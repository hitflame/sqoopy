#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Usage: sqoopy.py [--user=user] [--password=password] [--host=host] 
[--database=database] [--tables=tables] [--sqoop_options=sqoop_options] 

sqoopy: Generate sqoop custom import statements

Arguments:
	user			the MySQL username
	password		password belonging to user
	host			the host name of the MySQL database
	database		name of the database
	tables			comma separated list of tables that need to be inspected
	sqoop_options	Append verbatim sqoop command line options
	

'''

"""
sqoopy: Generate sqoop custom import statements
Copyright (C) 2012  Diederik van Liere, Wikimedia Foundation

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import subprocess
import re
import sys
import logging
import math

from docopt import docopt
from collections import OrderedDict

log = logging.getLogger()
log.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(formatter)
log.addHandler(ch)

column_size = re.compile('\(\d{1,5}\)')

class Column(object):
	def __init__(self, name, datatype, size, pk):
		self.name = name
		self.datatype = datatype
		self.size = size
		self.pk = pk
	
	def __str__(self):
		return '%s (%s)' % (self.name, self.datatype)

class Datatype(object):
	def __init__(self):
		self.hive_types = set(['smallint', 'int', 'bigint', 'boolean', 'float', 'double', 'string', 'binary', 'timestamp']) 
		'''
		Mysql to Mysql Casting
		'''
		self.mysql_to_mysql = {}
		self.mysql_to_mysql['varbinary'] = 'char'
		self.mysql_to_mysql['binary'] = 'char'
		self.mysql_to_mysql['blob'] = 'char'
		self.mysql_to_mysql['tinyblob'] = 'char'
		self.mysql_to_mysql['mediumblob'] = 'char'
		'''
		Mysql to Hive Casting
		'''
		self.mysql_to_hive = {}
		self.mysql_to_hive['varbinary'] = 'string'
		self.mysql_to_hive['binary'] = 'string'
		self.mysql_to_hive['blob'] = 'string'
		self.mysql_to_hive['timestamp'] = 'timestamp'
		self.mysql_to_hive['varchar'] = 'string'
		self.mysql_to_hive['char'] = 'string'
		self.mysql_to_hive['tinyint'] = 'smallint'
		self.mysql_to_hive['mediumblob'] = 'string'
		self.mysql_to_hive['enum'] = 'array'
		
		
		self.size = {}
		self.size['timestamp'] = 19
		
	def supports(self, mysql_datatype):
		return True if mysql_datatype in self.hive_types else False
	
	def requires_mysql_cast(self, mysql_datatype):
		if mysql_datatype in self.mysql_to_mysql.keys():
			return True
		else:
			return False
	
	def convert(self, mysql_datatype, destination):
		if destination == 'hive':
			if self.supports(mysql_datatype):
				return mysql_datatype
			else:
				if self.requires_mysql_cast(mysql_datatype):
					mysql_datatype = self.mysql_to_mysql.get(mysql_datatype)
				return self.mysql_to_hive.get(mysql_datatype, '%s has not yet a hive mapping ' % mysql_datatype)
		elif destination == 'mysql':
			return self.mysql_to_mysql.get(mysql_datatype)
		else:
			raise Exception('Destination %s is not supported' % destination)
			sys.exit(-1)

class Db(object):
	def __init__(self, user, password, host, database, tables=None, sqoop_options=None):
		self.user = user
		self.password = password
		self.host = host
		self.database = database
		self.tables = tables if tables else [] 
		self.sqoop_options = sqoop_options if sqoop_options != None else ''
		self.data = None
		self.row_count = 0
		self.blocksize = (1024 ** 3) * 256  # Hardcoded default for now
		self.schema = OrderedDict()
		self.verbose = True
		self.mysql_cmd = ['mysql', '-h', self.host, '-u%s' % self.user, '-p%s' % self.password, self.database]
		self.sqoop_cmd = 'sqoop import --username %s --password %s --connect jdbc:mysql://%s:3306/%s %s' % (self.user, self.password, self.host, self.database, self.sqoop_options)

	def __str__(self):
		return '%s@%s:%s' % (self.user, self.host, self.database)

	def get_pk(self, table):
		for name, column in self.schema.iteritems():
			if column.pk is True:
				return name
		raise Exception('Could not determine the primary key from table %s' % table)
		log.error('Could not determine the primary key for table %s' % table)
		sys.exit(-1)

	def launch(self, query):	
		p = subprocess.Popen(self.mysql_cmd, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
		stdoutdata, stderrdata = p.communicate(query)
		if stderrdata:
			raise Exception('The following error was encountered:\n %s' % stderrdata)
			log.error('Encountered error: %s' % stderrdata)
			sys.exit(-1)
		stdoutdata = stdoutdata.split('\n')
		return stdoutdata[1:-1]
	
	def get_row_count(self, table):
		query = "SELECT TABLE_ROWS FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA ='%s' AND TABLE_NAME ='%s';" % (self.database, table)
		self.row_count = float(self.launch(query)[0])

	def get_tables(self):
		self.tables = []
		tables = self.launch('SHOW TABLES')
		for table in tables:
			if self.verbose:
				log.info('Found table: %s' % table)
			self.tables.append(table)
	
	def inspect(self, table):
		self.data = self.launch('DESCRIBE %s' % table)
	
	def create_schema(self, table):
		mapping = Datatype()
		for data in self.data:
			data = data.split('\t')
			name = data[0]
			datatype = re.split(column_size, data[1])[0]
			datatype = datatype.lower()
			
			size = re.findall(column_size, data[1])
			if len(size) > 0:
				size = int(size[0][1:-1])
			else:
				size = mapping.size.get(datatype, 0)
			
			pk = True if data[3] == 'PRI' or data[3] == 'MUL' else False
			
			if self.verbose:
				log.info('Table: %s, found column: %s (%s)' % (table, name, datatype))

			column = Column(name, datatype, size, pk)
			self.schema.setdefault(name, column)
	
	def cast_columns(self):
		query = ''
		converter = Datatype()
		for name, column in self.schema.iteritems():
			if converter.requires_mysql_cast(column.datatype):
				part = 'CAST(%s AS %s CHARACTER SET utf8) AS %s' % (name, converter.mysql_to_mysql.get(column.datatype), name)
			else:
				part = name
			query = ', '.join([query, part])
		return query[1:]
	
	def number_of_mappers(self, table):
		self.get_row_count(table)
		row_size = sum([column.size for column in self.schema.itervalues()]) + len(self.schema.keys())
		num_mappers = int(math.ceil((self.row_count * row_size) / self.blocksize))
		if num_mappers < 5:
			return 4
		else:
			return num_mappers

	def generate_query(self, query_type, query, table):
		'''
		About importance of $CONDITIONS, see:
		https://groups.google.com/a/cloudera.org/forum/?fromgroups#!topic/sqoop-user/Z9Wa2ISpRvI
		
		Valid Sqoop import statement using custom SQL select query
		sqoop import --username <username> -P --target-dir /foo/bar 
			--connect jdbc:mysql://localhost:3306/db_name 
			--split-by rc_id 
			--query 'SELECT rc_id,CAST(column AS char(255) CHARACTER SET utf8) AS column FROM table_name WHERE $CONDITIONS'
		'''
		if query_type == 'select':			
			query = 'SELECT %s FROM %s WHERE $CONDITIONS' % (query, table)
			if self.verbose:
				log.info('Constructed query: %s' % query)
		else:
			raise Exception('Query type %s not yet supported' % query_type)
		return query
	
	def generate_sqoop_cmd(self, mappers, query, table):
		pk = self.get_pk(table)
		split_by = '--split-by %s' % pk
		query = "--query '%s'" % query
		mappers = '--num-mappers %s' % mappers
		target_dir = '--target-dir /tmp/' 
		sqoop_cmd = ' '.join([self.sqoop_cmd, split_by, mappers, target_dir, query])
		if self.verbose:
			log.info('Generated sqoop command: %s' % sqoop_cmd)
		return sqoop_cmd

def main(args):
	'''
	Given a mysql database name and an optional table, construct a select query 
	that takes care of casting (var)binary and blob fields to char fields.
	'''
	database = Db(args.get('--user'), args.get('--password'), args.get('--host'),
				args.get('--database'), args.get('--table'), args.get('--target_dir'))
	if not args.get('--table'):
		database.get_tables()
	else:
		database.tables = args.get('--tables').split(',')
		
	fh = open('sqoop.sh', 'w')
	log.info('Opening file handle...')
	for table in database.tables:
		database.inspect(table)
		database.create_schema(table)
		query = database.cast_columns()
		query = database.generate_query('select', query, table)
		mappers = database.number_of_mappers(table)
		sqoop_cmd = database.generate_sqoop_cmd(mappers, query, table)
		fh.write(sqoop_cmd)
		fh.write('\n\n')
	fh.close()
	log.info('Closing filehandle.')
	log.info('Exit successfully')
	
if __name__ == '__main__':
	args = docopt(__doc__)
	main(args)