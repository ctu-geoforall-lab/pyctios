###############################################################################
# Name:         library CTI_OS
# Purpose:      Sends a request to CTI_OS service based on given posident array
#               and stores response to SQLITE DB
# Date:         May 2019
# Copyright:    (C) 2019 Linda Kladivova
# Email:        l.kladivova@seznam.cz
###############################################################################

import requests
import xml.etree.ElementTree as et
import sqlite3
import math
import logging
from datetime import datetime
import re
import os
from pathlib import Path
import configparser

from ctios.exceptions import CtiOsError

from ctios.templates import CtiOsTemplate
from ctios.csv import CtiOsCsv


class CtiOs:
    """
        CTIOS class contains methods for requesting CTIOS service, processing the response and saving it to db
    """

    def __init__(self, username, password, config_file=None):
        """
        Constructor of CTIOS class

        Args:
            username (str): Username for CTIOS service
            password (str): Password for CTIOS service
            config_file (str): Configuration file (not mandatory, if not specified, default config file is selected)
        """
        # CTI_OS authentication
        self._username = username
        self._password = password

        config = configparser.ConfigParser()

        # Read configuration
        if config_file is None:
            config.read(os.path.join(os.path.dirname(__file__), 'settings.ini'))
        else:
            config.read(config_file)

        # Directories and path loaded from ini file
        self._base_dir = config['paths']['base_dir']
        self._template_dir = config['paths']['templates_dir']
        self._csv_dir = config['paths']['csv_dir']
        self._attribute_map_file = config['paths']['attribute_map_file']  # Mapping xml and database attributes

        # CTIOS service parameters loaded from ini file
        self._content_type = config['service headers']['Content-Type']
        self._accept_encoding = config['service headers']['Accept-Encoding']
        self._SOAP_action = config['service headers']['SOAPAction']
        self._connection = config['service headers']['Connection']
        self._endpoint = config['service headers']['Endpoint']
        self._max_num = int(config['service headers']['Max_num'])

        # CTIOS headers for service requesting
        self._headers = {"Content-Type": self._content_type, "Accept-Encoding": self._accept_encoding,
                         "SOAPAction": self._SOAP_action, "Connection": self._connection}

    @staticmethod
    def set_db(db_path):
        """
        Control path to db

        Args:
            db_path (str): Path to vfk db

        Raises:
            CtiOsError: FileNotFoundError
        """

        my_file = Path(db_path)

        try:
            # Control if database file exists
            my_file.resolve(strict=True)
            return db_path

        except FileNotFoundError as e:
            raise CtiOsError(e)

    def _create_connection(self, db_path):
        """
        Create a database connection to the SQLite database specified by the db_file

        Args:
            db_path (str): path to vfk db

        Raises:
            CtiOsError(SQLite error)

        Returns:
            conn (Connection object)
        """

        try:
            conn = sqlite3.connect(db_path)
            return conn
        except sqlite3.Error as e:
            if self.log_dir:
                self.logging.fatal('SQLITE3 ERROR!' + db_path)
            raise CtiOsError("SQLITE3 ERROR {}".format(e))

    def _get_ids_from_db(self, db_path, sql):
        """
        Get ids from db

        Args:
            db_path (str): path to vfk db
            sql (str): SQL select statement for id selection

        Raises:
            CtiOsError: "Database error"

        Returns:
            ids (list): pseudo ids from db
        """
        try:
            with self._create_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(sql)
                ids = cur.fetchall()
                cur.close()
        except sqlite3.Error as e:
            raise CtiOsError("Database error: {}".format(e))

        return list(set(ids))

    def set_ids_from_db(self, db_path, sql=None):
        """
        Set ids from db

        Args:
            db_path (str): path to db
            sql (str): SQL select statement for filtering or None (if not specified, all ids from db are selected)

        Raises:
            CtiOsError: Query has an empty result! (Raises when the response is empty)

        Returns:
            ids (list): pseudo ids from db
        """

        if sql is None:
            sql = "SELECT ID FROM OPSUB"
        ids = self._get_ids_from_db(db_path, sql)

        # Control if not empty
        if len(ids) <= 1:
            raise CtiOsError("Query has an empty result!")

        return ids

    def _draw_up_xml_request(self, ids):
        """
        Draw up xml request using ids array and function for rendering from CtiOsTemplate class

        Args:
            ids (list): pseudo ids from db

        Returns:
            request_xml (str): xml request for CtiOS service
        """

        ids_array = []

        for i in ids:
            row = "<v2:pOSIdent>{}</v2:pOSIdent>".format(i[0])
            ids_array.append(row)  # Add all tags to one list

        # Render XML request using request xml template
        request_xml = CtiOsTemplate(self._template_dir).render(
            'request.xml', username=self._username, password=self._password,
            posidents=''.join(ids_array)
        )
        return request_xml

    def _call_service(self, request_xml):
        """
        Send a request in the XML form to CTI_OS service

        Args:
            request_xml (str): xml for requesting CTIOS service

        Raises:
            CtiOsError(Service error)

        Returns:
            response_xml (str): xml response from CtiOS service
        """
        try:
            response_xml = requests.post(self._endpoint, data=request_xml, headers=self._headers)
            response_xml = response_xml.text

        except requests.exceptions.RequestException as e:
            raise CtiOsError("Service error: {}".format(e))

        return response_xml

    @staticmethod
    def _transform_names(xml_name):
        """
        Convert names in XML name to name in database (eg. StavDat to stav_dat)

        Args:
            xml_name (str): tag of xml attribute in xml response

        Returns:
            database_name (str): column names in database
        """

        database_name = re.sub('([A-Z]{1})', r'_\1', xml_name).upper()
        return database_name

    def _transform_names_dict(self, xml_name):
        """
            Convert names in XML name to name in database based on special dictionary

            Args:
                xml_name (str): tag of xml attribute in xml response

            Raises:
                CtiOsError(XML ATTRIBUTE NAME CANNOT BE CONVERTED TO DATABASE COLUMN NAME)

            Returns:
                database_name (str): column names in database
        """
        try:
            # Load dictionary with names of XML tags and their relevant database names
            dictionary = CtiOsCsv(self._csv_dir).read_csv_as_dictionary(
                self._attribute_map_file
            )
            database_name = dictionary[xml_name]
            return database_name

        except Exception as e:
            if self.log_dir:
                self.logging.exception('XML ATTRIBUTE NAME CANNOT BE CONVERTED TO DATABASE COLUMN NAME')
            raise CtiOsError("XML ATTRIBUTE NAME CANNOT BE CONVERTED TO DATABASE COLUMN NAME: {}".format(e))

    def _save_attributes_to_db(self, response_xml, db_path):
        """
         1. Parses XML returned by CTI_OS service into desired parts which will represent database table attributes
         2. Connects to db
         3. Alters table by adding OS_ID column if not exists
         4. Updates attributes for all pseudo ids in SQLITE3 table rows

         Args:
             response_xml (str): tag of xml attribute in xml response
             db_path (str): path to vfk db
        """
        root = et.fromstring(response_xml)

        for os in root.findall('.//{http://katastr.cuzk.cz/ctios/types/v2.8}os'):

            # Check errors of given ids, if error occurs continue back to the function beginning
            if os.find('{http://katastr.cuzk.cz/ctios/types/v2.8}chybaPOSIdent') is not None:
                posident = os.find('{http://katastr.cuzk.cz/ctios/types/v2.8}pOSIdent').text

                if os.find('{http://katastr.cuzk.cz/ctios/types/v2.8}chybaPOSIdent').text == "NEPLATNY_IDENTIFIKATOR":
                    self.state_vector['neplatny_identifikator'] += 1
                    if self.log_dir:
                        self.logging.error('POSIDENT {} NEPLATNY IDENTIFIKATOR'.format(posident))
                    continue
                if os.find('{http://katastr.cuzk.cz/ctios/types/v2.8}chybaPOSIdent').text == "EXPIROVANY_IDENTIFIKATOR":
                    self.state_vector['expirovany_identifikator'] += 1
                    if self.log_dir:
                        self.logging.error('POSIDENT {}: EXPIROVANY IDENTIFIKATOR'.format(posident))
                    continue
                if os.find(
                        '{http://katastr.cuzk.cz/ctios/types/v2.8}chybaPOSIdent').text == "OPRAVNENY_SUBJEKT_NEEXISTUJE":
                    self.state_vector['opravneny_subjekt_neexistuje'] += 1
                    if self.log_dir:
                        self.logging.error('POSIDENT {}: OPRAVNENY SUBJEKT NEEXISTUJE'.format(posident))
                    continue
            else:
                self.state_vector['uspesne_stazeno'] += 1

            # Create the dictionary with XML child attribute names and particular texts
            for child in os:
                name = child.tag
                if name == '{http://katastr.cuzk.cz/ctios/types/v2.8}pOSIdent':
                    pos = os.find(name)
                    posident = pos.text
                if name == '{http://katastr.cuzk.cz/ctios/types/v2.8}osId':
                    o = os.find(name)
                    osid = o.text

            xml_attributes = {}
            for child in os.find('.//{http://katastr.cuzk.cz/ctios/types/v2.8}osDetail'):
                name2 = child.tag
                xml_attributes[child.tag[child.tag.index('}') + 1:]] = os.find('.//{}'.format(name2)).text

            # Find out the names of columns in database and if column os_id doesnt exist, add it
            try:
                conn = self._create_connection(db_path)
                cur = conn.cursor()
                cur.execute("PRAGMA read_committed = true;")
                cur.execute('select * from OPSUB')
                col_names = list(map(lambda x: x[0], cur.description))
                if 'OS_ID' not in col_names:
                    cur.execute('ALTER TABLE OPSUB ADD COLUMN OS_ID TEXT')
                cur.close()
            except conn.Error:
                cur.close()
                conn.close()
                if self.log_dir:
                    self.logging.exception('Pripojeni k databazi selhalo')
                raise Exception("CONNECTION TO DATABASE FAILED")

            #  Transform xml_names to database_names
            database_attributes = {}
            for xml_name, xml_value in xml_attributes.items():
                database_name = self._transform_names(xml_name)
                if database_name not in col_names:
                    database_name = self._transform_names_dict(xml_name)
                database_attributes.update({database_name: xml_value})

            #  Update table OPSUB by database_attributes items
            try:
                cur = conn.cursor()
                cur.execute("BEGIN TRANSACTION")
                for dat_name, dat_value in database_attributes.items():
                    cur.execute("""UPDATE OPSUB SET {0} = ? WHERE id = ?""".format(dat_name), (dat_value, posident))
                cur.execute("""UPDATE OPSUB SET OS_ID = ? WHERE id = ?""", (osid, posident))
                cur.execute("COMMIT TRANSACTION")
                cur.close()
                if self.log_dir:
                    self.logging.info('Radky v databazi u POSIdentu {} aktualizovany'.format(posident))
            except conn.Error:
                print("failed!")
                cur.execute("ROLLBACK TRANSACTION")
                cur.close()
                conn.close()
            finally:
                if conn:
                    conn.close()
        if self.log_dir:
            return self.state_vector

    def _query_service(self, ids, db_path):
        """
        Function which draws up xml request, call service and save attributes into db using other partial functions

        Args:
            ids (list): list of pseudo ids
            db_path (str): path to vfk db
        """
        request_xml = self._draw_up_xml_request(ids)  # Putting XML request together
        response_xml = self._call_service(request_xml)  # CTI_OS request with upper parameters
        self._save_attributes_to_db(response_xml, db_path)  # Save attributes to db

    def query_requests(self, ids, db_path):
        """
        Main function which divides requests into groups and process them

        Args:
            ids (list): list of pseudo ids
            db_path (str): path to vfk db
        """
        if self.log_dir:
            self.logging.info('Pocet jedinecnych ID v seznamu: {}'.format(len(ids)))

        if len(ids) <= self._max_num:
            self._query_service(ids, db_path)  # Query and save response to db
            if self.log_dir:
                self.logging.info('Zpracovano v ramci 1 pozadavku.')
        else:
            full_arrays = math.floor(len(ids) / self._max_num)  # Floor to number of full posidents arrays
            rest = len(ids) % self._max_num  # Left posidents
            for i in range(0, full_arrays):
                start = i * self._max_num
                end = i * self._max_num + self._max_num
                whole_ids = ids[start: end]
                self._query_service(whole_ids, db_path)  # Query and save response to db

            # make a request from the rest of posidents
            ids_rest = ids[len(ids) - rest: len(ids)]
            if ids_rest:
                self._query_service(ids_rest, db_path)  # Query and save response to db
                divided_into = full_arrays + 1
            else:
                divided_into = full_arrays

            if self.log_dir:
                self.logging.info('Rozdeleno do {} pozadavku'.format(divided_into))

        if self.log_dir:
            self.logging.info('Pocet uspesne stazenych posidentu: {} '.format(
                self.state_vector['uspesne_stazeno']))
            self.logging.info('Neplatny identifikator: {}x.'.format(
                self.state_vector['neplatny_identifikator']))
            self.logging.info('Expirovany identifikator: {}x.'.format(
                self.state_vector['expirovany_identifikator']))
            self.logging.info('Opravneny subjekt neexistuje: {}x.'.format(
                self.state_vector['opravneny_subjekt_neexistuje']))

    def set_log_file(self, log_dir):
        """
        Create pyctios log file

        Args:
            log_dir (str): path to log directory

        Returns:
            logging (Logging object)
        """
        logger = logging.getLogger('pyctios')
        self.log_dir = log_dir
        log_filename = datetime.now().strftime('%H_%M_%S_%d_%m_%Y.log')

        # set up logging to file - see previous section for more details
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s %(levelname)-8s %(message)s',
                            datefmt='%m-%d %H:%M',
                            filename=log_dir + '/' + log_filename,
                            filemode='w')
        # define a Handler which writes INFO messages or higher to the sys.stderr
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)

        # Create formatters and add it to handlers
        formatter = logging.Formatter('%(name)-12s - %(levelname)-8s - %(message)s')
        console.setFormatter(formatter)

        # Add handlers to the logger
        logger.addHandler(console)
        self.logging = logger

        # Initialization of state vector
        self.state_vector = {
            'neplatny_identifikator': 0,
            'expirovany_identifikator': 0,
            'opravneny_subjekt_neexistuje': 0,
            'uspesne_stazeno': 0}





