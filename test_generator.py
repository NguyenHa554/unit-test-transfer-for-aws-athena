"""Offline tests. Optional DuckDB executes the generated relational test SQL."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sqlglot
from sqlglot import exp

from generator import HEADERS, TestGenerator, columns_of, predicate, relation
from server import LocalHTTPServer, make_handler

try:
    import duckdb
except ImportError:
    duckdb = None

CONFIG = "{{ config(schema=var('env_km_staging'), alias='example', unique_key='id') }}\n"
SQL = CONFIG + """SELECT date'{{ var('etl_date') }}' AS SYM_RUN_DATE,
    s.id, s.amount
FROM {{ ref('brz_input') }} s
WHERE s.ds_dt = date'{{ var('etl_date') }}'"""


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.write('dbt/dbt_project.yml', '''name: fixture
version: '1.0'
vars:
  env_km_staging: "{{ target.name }}_km_stg"
  env_bronze: "{{ target.name }}_bronze"
  env_landing: "{{ target.name }}_lnd"
''')
        self.write('dbt/models/bronze/brz_input.sql', "{{ config(schema=var('env_bronze'), alias='src') }} SELECT 1 AS id")
        self.generator = TestGenerator(self.repo)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def generate(self, sql=SQL, **overrides):
        return self.generator.generate(dict(sql=sql, etl_date='2026-07-01', scope_confirmed=True, **overrides))

    def test_three_complete_queries(self):
        result = self.generate()
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['target'], 'dev_km_stg.example')
        self.assertEqual(result['source_tables'], ['dev_bronze.src'])
        self.assertEqual(result['columns'], ['SYM_RUN_DATE', 'id', 'amount'])
        for test in result['tests']:
            self.assertTrue(test['sql'].endswith(';\n'))
            sqlglot.parse_one(test['sql'], read='trino')
            self.assertEqual(list(test['metadata']), HEADERS)
            self.assertEqual(test['metadata']['Actual Result'], '')
            self.assertEqual(test['metadata']['Status'], 'Not Executed')
        self.assertEqual(result['tests'][2]['sql'].count('EXCEPT ALL'), 2)

    def test_requires_scope_confirmation(self):
        result = self.generator.generate(dict(sql=SQL, etl_date='2026-07-01'))
        self.assertTrue(all(not t['sql'] for t in result['tests']))
        self.assertIn('Confirm', ' '.join(result['input_issues']))

    def test_missing_key_blocks_only_duplicate(self):
        result = self.generate(SQL.replace(", unique_key='id'", ''))
        self.assertEqual([t['ready'] for t in result['tests']], [True, False, True])
        self.assertEqual(result['keys'], [])

    def test_key_is_not_guessed_from_name(self):
        result = self.generate("SELECT 1 AS INTERNAL_KEY, 2 AS DEBIT_NOTE_NO", target='dev_km_stg.x', scope_mode='all')
        self.assertEqual(result['keys'], [])

    def test_manual_composite_key(self):
        result = self.generate(keys='id, SYM_RUN_DATE')
        self.assertEqual(result['keys'], ['id', 'SYM_RUN_DATE'])
        self.assertIn('GROUP BY "id", "SYM_RUN_DATE"', result['tests'][1]['sql'])

    def test_invalid_key_column(self):
        self.assertFalse(self.generate(keys='not_in_output')['tests'][1]['ready'])
        self.assertFalse(self.generate(keys='id,id')['tests'][1]['ready'])

    def test_model_yaml_constraints(self):
        self.write('dbt/models/example.sql', SQL.replace(", unique_key='id'", ''))
        self.write('dbt/models/schema.yml', '''version: 2
models:
  - name: example
    constraints:
      - type: primary_key
        columns: [id, SYM_RUN_DATE]
''')
        result = self.generate(SQL.replace(", unique_key='id'", ''))
        self.assertEqual(result['keys'], ['id', 'SYM_RUN_DATE'])
        self.assertEqual(result['job'], 'example')

    def test_ambiguous_unique_tests_need_choice(self):
        text = SQL.replace(", unique_key='id'", '')
        self.write('dbt/models/example.sql', text)
        self.write('dbt/models/schema.yml', '''version: 2
models:
  - name: example
    columns:
      - name: id
        data_tests: [unique]
      - name: amount
        tests: [unique]
''')
        result = self.generate(text)
        self.assertEqual(len(result['key_candidates']), 2)
        self.assertFalse(result['tests'][1]['ready'])

    def test_combination_test(self):
        self.write('dbt/models/example.sql', SQL.replace(", unique_key='id'", ''))
        self.write('dbt/models/schema.yml', '''version: 2
models:
  - name: example
    data_tests:
      - dbt_utils.unique_combination_of_columns:
          arguments:
            combination_of_columns: [id, SYM_RUN_DATE]
''')
        self.assertEqual(self.generate(SQL.replace(", unique_key='id'", ''))['keys'], ['id', 'SYM_RUN_DATE'])

    def test_missing_reference_never_guessed(self):
        result = self.generate(SQL.replace('brz_input', 'brz_missing'))
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(all(not t['sql'] for t in result['tests']))

    def test_missing_source_never_guessed(self):
        result = self.generate(SQL.replace("ref('brz_input')", "source('bronze','missing')"))
        self.assertEqual(result['status'], 'blocked')

    def test_source_yaml_and_variable_override(self):
        self.write('dbt/models/sources.yml', '''version: 2
sources:
  - name: raw
    schema: "{{ var('env_bronze') }}"
    tables:
      - name: source_input
        identifier: src
''')
        result = self.generate(SQL.replace("ref('brz_input')", "source('raw', 'source_input')"), variables={'env_bronze':'dev_bronze'})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['source_tables'], ['dev_bronze.src'])

    def test_incremental_context_is_explicit(self):
        text = SQL + '\n{% if is_incremental() %} AND s.id > 5 {% endif %}'
        self.assertEqual(self.generate(text)['status'], 'blocked')
        self.assertIn('s.id > 5', self.generate(text, mode='incremental')['sql'])
        self.assertNotIn('s.id > 5', self.generate(text, mode='full_refresh')['sql'])

    def test_this_resolves_to_confirmed_target(self):
        text = SQL + '\n{% if is_incremental() %} AND s.id > (SELECT MAX(id) FROM {{ this }}) {% endif %}'
        result = self.generate(text, mode='incremental')
        self.assertEqual(result['status'], 'ready')
        self.assertIn('FROM dev_km_stg.example', result['sql'])

    def test_filtered_unique_is_not_a_global_key(self):
        text = SQL.replace(", unique_key='id'", '')
        self.write('dbt/models/example.sql', text)
        self.write('dbt/models/schema.yml', '''version: 2
models:
  - name: example
    columns:
      - name: id
        data_tests:
          - unique:
              config:
                where: id > 10
''')
        self.assertEqual(self.generate(text)['keys'], [])

    def test_macro_expansion_and_unknown_macro(self):
        self.write('dbt/macros/simple.sql', '{% macro twice(x) %} ({{ x }} * 2) {% endmacro %}')
        result = self.generate(SQL.replace('s.amount', "{{ twice('s.amount') }} AS amount"))
        self.assertEqual(result['status'], 'ready')
        self.assertIn('s.amount * 2', result['sql'])
        self.assertEqual(self.generate(SQL.replace('s.amount', "{{ unknown_macro() }} AS amount"))['status'], 'blocked')

    def test_hash_and_runtime_macro_blocked(self):
        self.assertEqual(self.generate(SQL.replace('s.amount', 'xxhash64(s.amount) AS amount'))['status'], 'blocked')
        self.write('dbt/macros/runtime.sql', "{% macro runtime() %} {{ run_query('SELECT 1') }} {% endmacro %}")
        self.assertEqual(self.generate(SQL.replace('s.amount', '{{ runtime() }} AS amount'))['status'], 'blocked')

    def test_first_split_element_conversion(self):
        result = self.generate(SQL.replace('s.amount', "element_at(split(s.amount, '-'), 1) AS amount"))
        self.assertEqual(result['status'], 'ready')
        self.assertIn('ELEMENT_AT(REGEXP_SPLIT', result['sql'])
        self.assertEqual(self.generate(SQL.replace('s.amount', "element_at(split(s.amount, '-'), 5) AS amount"))['status'], 'blocked')

    def test_cte_star_and_alias(self):
        sql = CONFIG + 'WITH c AS (SELECT 1 AS id, 2 AS amount) SELECT c.* FROM c'
        result = self.generate(sql, scope_mode='all')
        self.assertEqual(result['columns'], ['id','amount'])
        self.assertEqual(result['status'], 'ready')

    def test_unknown_star_requires_manual_columns(self):
        sql = CONFIG + "SELECT * FROM {{ ref('brz_input') }}"
        self.assertEqual(self.generate(sql, scope_mode='all')['columns'], [])
        result = self.generate(sql, scope_mode='all', output_columns='id, amount')
        self.assertEqual(result['columns'], ['id','amount'])
        self.assertEqual(result['status'], 'ready')

    def test_unnamed_expression_and_duplicate_columns_blocked(self):
        for sql in ['SELECT 1+1', 'SELECT 1 AS id, 2 AS id']:
            result = self.generate(sql, target='dev_km_stg.example', scope_mode='all')
            self.assertFalse(any(t['ready'] for t in result['tests']))

    def test_snapshot_not_invented(self):
        result = self.generate(CONFIG + 'SELECT 1 AS id')
        self.assertIn('No SYM_RUN_DATE', ' '.join(result['input_issues']))

    def test_target_required_without_metadata(self):
        result = self.generate('SELECT 1 AS id', scope_mode='all', keys='id')
        self.assertIn('Target table is unknown', ' '.join(result['input_issues']))

    def test_target_and_predicate_validation(self):
        for value in ['x', 'x.y JOIN q ON true', 'x.y WHERE true', 'x.y; DROP TABLE q', 'x.y AS q']:
            with self.assertRaises(ValueError, msg=value):
                relation(value)
        self.assertEqual(relation('"dev_km_stg"."a-b"'), '"dev_km_stg"."a-b"')
        for value in ['true; DROP TABLE q', 'true ORDER BY 1', 'id IN (SELECT id FROM q)']:
            with self.assertRaises(ValueError, msg=value):
                predicate(value)
        self.assertEqual(predicate('id > 0'), 'id > 0')

    def test_custom_scope_only_changes_target(self):
        result = self.generate(scope_mode='custom', target_filter='id > 2')
        self.assertEqual(result['scope'], 'id > 2')
        self.assertIn('FROM dev_km_stg.example WHERE id > 2', result['tests'][0]['sql'])
        self.assertNotIn('id > 2', result['sql'])

    def test_union_window_join_and_cte_scope_retained(self):
        sql = CONFIG + '''WITH expected_source AS (
SELECT s.id, s.amount, ROW_NUMBER() OVER(PARTITION BY s.id ORDER BY s.amount) AS rn
FROM {{ ref('brz_input') }} s LEFT JOIN {{ ref('brz_input') }} b ON s.id=b.id
)
SELECT id, amount, rn FROM expected_source
UNION ALL SELECT id, amount, rn FROM expected_source'''
        result = self.generate(sql, scope_mode='all')
        self.assertEqual(result['status'], 'ready')
        converted = sqlglot.parse_one(result['sql'], read='trino')
        self.assertEqual(len(list(converted.find_all(exp.Join))), 1)
        self.assertEqual(len(list(converted.find_all(exp.Window))), 1)
        self.assertFalse(converted.args['distinct'])
        self.assertEqual(result['tests'][0]['sql'].count('WITH expected_source AS'), 2)

    @unittest.skipUnless(duckdb, 'Install requirements-dev.txt for offline SQL execution tests')
    def test_execute_counts_duplicates_and_bidirectional_except_all(self):
        db = duckdb.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE SCHEMA dev_bronze; CREATE SCHEMA dev_km_stg')
        db.execute('CREATE TABLE dev_bronze.src (id INTEGER, amount INTEGER, ds_dt DATE)')
        db.execute('CREATE TABLE dev_km_stg.example (SYM_RUN_DATE DATE, id INTEGER, amount INTEGER)')
        result = self.generate()
        run = lambda index: db.execute(result['tests'][index]['sql']).fetchall()
        self.assertEqual(run(0)[0], (0,0,0,'EMPTY_SOURCE'))
        db.execute("INSERT INTO dev_bronze.src VALUES (1,10,'2026-07-01'),(1,10,'2026-07-01'),(2,20,'2026-07-01'),(9,99,'2025-01-01')")
        db.execute("INSERT INTO dev_km_stg.example SELECT ds_dt,id,amount FROM dev_bronze.src")
        self.assertEqual(run(0)[0], (3,3,0,'PASS'))
        self.assertEqual(run(1), [(1,2,'DUPLICATE_KEY')])
        self.assertEqual(run(2), [])
        # Equal count but different values/multiplicities: COUNT alone is insufficient.
        db.execute("DELETE FROM dev_km_stg.example WHERE id=1; INSERT INTO dev_km_stg.example VALUES ('2026-07-01',1,10),('2026-07-01',3,30)")
        self.assertEqual(run(0)[0][3], 'PASS')
        mismatch = run(2)
        self.assertEqual(len(mismatch), 2)
        self.assertEqual({r[0] for r in mismatch}, {'SOURCE_MINUS_TARGET','TARGET_MINUS_SOURCE'})
        db.execute("INSERT INTO dev_km_stg.example VALUES ('2026-07-01',NULL,30)")
        self.assertIn((None,1,'NULL_KEY'), run(1))
        self.assertEqual(run(0)[0][3], 'FAIL')

    @unittest.skipUnless(duckdb, 'Install requirements-dev.txt for offline SQL execution tests')
    def test_count_includes_join_multiplication(self):
        db = duckdb.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE SCHEMA dev_bronze; CREATE SCHEMA dev_km_stg')
        db.execute("CREATE TABLE dev_bronze.src AS SELECT 1 AS id, 10 AS amount, DATE '2026-07-01' AS ds_dt UNION ALL SELECT 1,20,DATE '2026-07-01'")
        sql = SQL.replace("WHERE s.ds_dt", "LEFT JOIN {{ ref('brz_input') }} b ON s.id=b.id WHERE s.ds_dt")
        result = self.generate(sql)
        db.execute('CREATE TABLE dev_km_stg.example AS ' + result['sql'].rstrip(';'))
        self.assertEqual(db.execute(result['tests'][0]['sql']).fetchone(), (4,4,0,'PASS'))

    def test_api_read_only_and_origin_checks(self):
        server = LocalHTTPServer(('127.0.0.1',0), make_handler(self.generator))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(url + '/api/health') as response:
            self.assertEqual(json.load(response)['status'], 'ok')
        payload = json.dumps(dict(sql=SQL,scope_confirmed=True)).encode()
        request = urllib.request.Request(url+'/api/generate', data=payload, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)['status'], 'ready')
        request.add_header('Origin','http://untrusted.invalid')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,403)


class RealRepositoryTests(unittest.TestCase):
    def test_cl_invoice_dev_columns_and_queries(self):
        repo = Path(__file__).resolve().parent.parent / 'km-etl-newcore'
        path = repo / 'dbt/models/km_staging/tdh_sds/lending/km_stg_tdh_sds_cl_invoice.sql'
        if not path.is_file():
            self.skipTest('Actual CL_INVOICE repo not available')
        result = TestGenerator(repo).generate(dict(sql=path.read_text(encoding='utf-8-sig'), etl_date='2026-07-01',keys='INTERNAL_KEY',scope_confirmed=True))
        self.assertEqual(result['status'],'ready',result['errors']+result['input_issues'])
        self.assertEqual(result['target'],'dev_km_stg.tdh_sds_cl_invoice')
        self.assertEqual(len(result['columns']),53)
        self.assertNotIn('DEBIT_NOTE_NO',result['keys'])
        self.assertTrue(all('uat_' not in table for table in result['source_tables']))
        self.assertIn('dev_lnd.ods_cl_invoice_mapping_key',result['source_tables'])
        for test in result['tests']:
            sqlglot.parse_one(test['sql'],read='trino')


if __name__ == '__main__':
    unittest.main(verbosity=2)
