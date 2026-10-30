"""Generate read-only Athena Engine 3 test SQL. No database connections."""
from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import sqlglot
from jinja2 import nodes
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from converter import Converter, environment

HEADERS = ['Testcase ID', 'Test case type', 'Job name', 'Source Table',
           'Source Column', 'Target Table', 'Target Column', 'Test Parameter',
           'Expected Results', 'Actual Result', 'Status', 'Time-frame']


def identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def names(value):
    if isinstance(value, str):
        return [x.strip() for x in value.split(',') if x.strip()]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return [x.strip() for x in value if x.strip()]
    if value in (None, ''):
        return []
    raise ValueError('Column names must be a comma-separated string or a list of strings.')


def relation(value):
    """Only a qualified relation, never SQL pasted into an identifier slot."""
    try:
        statements = sqlglot.parse('SELECT * FROM ' + value, read='trino')
        if len(statements) != 1:
            raise ValueError()
        parsed = statements[0]
        table = parsed.args['from'].this
        if (not isinstance(table, exp.Table) or not table.db or table.alias
                or set(parsed.args) - {'kind', 'hint', 'distinct', 'expressions', 'limit', 'operation_modifiers', 'from'}
                or parsed.args.get('limit') or parsed.sql('trino').count(';')):
            raise ValueError()
        if any(not isinstance(x, exp.Identifier) for x in table.parts):
            raise ValueError()
        return table.sql('trino')
    except (sqlglot.errors.SqlglotError, KeyError, AttributeError, ValueError):
        raise ValueError('Target must be schema.table (or catalog.schema.table), without alias or SQL clauses.') from None


def predicate(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Enter a target WHERE predicate, without the WHERE keyword.')
    statements = sqlglot.parse('SELECT 1 FROM target_scope WHERE ' + value, read='trino')
    if len(statements) != 1:
        raise ValueError('Target scope must be one WHERE expression.')
    query = statements[0]
    if (not isinstance(query, exp.Select) or set(query.args) -
            {'kind', 'hint', 'distinct', 'expressions', 'limit', 'operation_modifiers', 'from', 'where'}
            or query.args.get('limit') or not query.args.get('where')):
        raise ValueError('Only a WHERE predicate is allowed in target scope.')
    condition = query.args['where'].this
    if condition.find(exp.Query):
        raise ValueError('Subqueries in target scope are not supported; use a direct target-column predicate.')
    return condition.sql('trino')


def columns_of(tree, supplied=None):
    """Expand CTE/subquery stars only when column names are actually known."""
    def visit(scope):
        if isinstance(scope.expression, exp.SetOperation):
            return visit(scope.union_scopes[0])
        result = []
        for projection in scope.expression.selects:
            if projection.is_star:
                qualifier = projection.table if isinstance(projection, exp.Column) else ''
                selected = [(alias, pair[1]) for alias, pair in scope.selected_sources.items()
                            if not qualifier or alias.lower() == qualifier.lower()]
                if not selected or any(not isinstance(src, Scope) for _, src in selected):
                    raise ValueError('Final SELECT * needs an explicit output-column list or known CTE columns.')
                if any(projection.find(exp.Star).args.values()):
                    raise ValueError('Star EXCEPT/REPLACE needs explicit output columns in the model.')
                for _, source in selected:
                    result.extend(source.outer_columns or visit(source))
            elif isinstance(projection, (exp.Alias, exp.Column)):
                result.append(projection.alias_or_name)
            else:
                raise ValueError('Every calculated final output must have an explicit AS column alias.')
        return result

    scope = build_scope(tree)
    try:
        result = visit(scope)
    except ValueError:
        manual = names(supplied)
        # Manual columns can resolve only stars, not unnamed computed expressions.
        if not manual or not any(p.is_star for p in tree.selects):
            raise
        if any(not p.is_star and not isinstance(p, (exp.Alias, exp.Column)) for p in tree.selects):
            raise
        result = manual
    if not result or any(not x for x in result):
        raise ValueError('Could not identify the output columns.')
    if len({x.lower() for x in result}) != len(result):
        raise ValueError('Duplicate output column names: add unique aliases before generating tests.')
    return result


class TestGenerator:
    __test__ = False

    def __init__(self, repo):
        self.converter = Converter(repo)

    def config(self, text, request, inherited=None):
        cfg = dict(inherited or {})
        env = environment()
        variables = {**self.converter.metadata.project.get('vars', {}), **request.get('variables', {})}
        variables['etl_date'] = request.get('etl_date') or '2026-07-01'
        resolving = set()

        def var(name, *default):
            if name in resolving:
                raise ValueError('Circular variable: ' + name)
            if name not in variables and not default:
                raise ValueError('Missing variable: ' + name)
            value = variables.get(name, default[0] if default else None)
            if isinstance(value, str) and '{{' in value:
                resolving.add(name)
                try:
                    return env.from_string(value).render()
                finally:
                    resolving.remove(name)
            return value

        env.globals.update(var=var, target=SimpleNamespace(name='dev'))
        for call in env.parse(text).find_all(nodes.Call):
            if not isinstance(call.node, nodes.Name) or call.node.name != 'config':
                continue
            pairs = [(x.key, x.value) for x in call.kwargs]
            for arg in call.args:
                if isinstance(arg, nodes.Dict):
                    pairs.extend((p.key.value, p.value) for p in arg.items if isinstance(p.key, nodes.Const))
            for key, value in pairs:
                if key not in {'alias', 'schema', 'unique_key', 'materialized', 'partition_by'}:
                    continue
                rendered = env.from_string(nodes.Template([nodes.Output([value])])).render()
                try:
                    cfg[key] = ast.literal_eval(rendered)
                except (ValueError, SyntaxError):
                    cfg[key] = rendered
        for key, value in list(cfg.items()):
            if isinstance(value, str) and '{{' in value:
                cfg[key] = env.from_string(value).render()
        return cfg

    def model_info(self, text, request):
        meta = self.converter.metadata
        chosen = request.get('model_name', '').strip()
        path = None
        if chosen:
            matches = meta.models.get(chosen, [])
            if len(matches) != 1:
                raise ValueError('Model name is missing or ambiguous in the selected repo.')
            path = matches[0]
        else:
            # Exact saved source match avoids inferring a job from a ref() dependency.
            matches = [p for paths in meta.models.values() for p in paths
                       if p.read_text(encoding='utf-8-sig').strip() == text.strip()]
            if len(matches) == 1:
                path = matches[0]
                chosen = path.stem
        inherited = meta.inherited(path) if path else {}
        cfg = self.config(text, request, inherited)
        inferred = relation(f'{cfg["schema"]}.{cfg.get("alias", chosen)}') if cfg.get('schema') and (cfg.get('alias') or chosen) else ''
        candidates = []

        def add(cols, evidence):
            cols = names(cols)
            if cols and all(re.fullmatch(r'[A-Za-z_]\w*', x) for x in cols):
                existing = next((c for c in candidates if [x.lower() for x in c['columns']] == [x.lower() for x in cols]), None)
                if existing:
                    existing['evidence'] += '; ' + evidence
                else:
                    candidates.append({'columns': cols, 'evidence': evidence})

        add(cfg.get('unique_key'), 'dbt unique_key config')
        for doc in meta.model_docs.get(chosen, []):
            for constraint in doc.get('constraints', []):
                if constraint.get('type') in ('primary_key', 'unique'):
                    add(constraint.get('columns'), 'model ' + constraint['type'] + ' constraint')
            primary = []
            for column in doc.get('columns', []):
                for constraint in column.get('constraints', []):
                    if constraint.get('type') == 'primary_key':
                        primary.append(column['name'])
                    elif constraint.get('type') == 'unique':
                        add([column['name']], 'column unique constraint')
                for test in column.get('data_tests', column.get('tests', [])):
                    if test == 'unique' or isinstance(test, dict) and 'unique' in test and not (test['unique'] or {}).get('config', {}).get('where'):
                        add([column['name']], 'column unique test')
            add(primary, 'column primary_key constraints')
            for test in doc.get('data_tests', doc.get('tests', [])):
                if isinstance(test, dict):
                    for name, value in test.items():
                        if name.endswith('unique_combination_of_columns') and isinstance(value, dict) and not value.get('config', {}).get('where'):
                            add(value.get('arguments', value).get('combination_of_columns'), 'unique combination test')
        return chosen or cfg.get('alias') or 'pasted_model', inferred, candidates

    def generate(self, request):
        if not isinstance(request, dict):
            raise ValueError('Expected a JSON object.')
        for field in ('sql', 'etl_date', 'mode', 'model_name', 'target', 'scope_mode', 'target_filter'):
            if field in request and not isinstance(request[field], str):
                raise ValueError(f'{field} must be a string.')
        if 'scope_confirmed' in request and not isinstance(request['scope_confirmed'], bool):
            raise ValueError('scope_confirmed must be true or false.')
        conversion_request = dict(request)
        info = None
        info_error = None
        self.converter.metadata.refresh()
        if isinstance(request.get('sql'), str) and isinstance(request.get('variables', {}), dict):
            try:
                info = self.model_info(request['sql'], request)
                this = relation(request['target'].strip()) if request.get('target', '').strip() else info[1]
                if this:
                    conversion_request['this_relation'] = this
            except Exception as exc:
                # Conversion can still supply useful diagnostics, but no test
                # may be emitted using unresolved model/target configuration.
                info_error = str(exc)
        converted = self.converter.convert(conversion_request)
        result = {**converted, 'status': 'needs_input', 'tests': [], 'columns': [],
                  'target': '', 'key_candidates': [], 'keys': [], 'scope': '', 'job': ''}
        problems = []
        if converted['status'] != 'converted' or converted.get('output_kind') == 'relation':
            problems.append('Paste a complete SELECT/WITH model and resolve conversion errors first.')
        else:
            try:
                tree = sqlglot.parse_one(converted['sql'], read='trino')
                result['columns'] = columns_of(tree, request.get('output_columns'))
                if info_error:
                    raise ValueError(info_error)
                result['job'], inferred, candidates = info
                result['key_candidates'] = candidates
                result['target'] = relation(request['target'].strip()) if request.get('target', '').strip() else inferred
                if not result['target']:
                    problems.append('Target table is unknown. Enter the physical schema.table in Advanced settings.')
                known = {c.lower(): c for c in result['columns']}
                manual = names(request.get('keys'))
                selected = manual or (candidates[0]['columns'] if len(candidates) == 1 else [])
                if any(k.lower() not in known for k in selected):
                    result['key_problem'] = 'Selected key columns are not present in the model output.'
                elif len(set(k.lower() for k in selected)) != len(selected):
                    result['key_problem'] = 'Key columns must not repeat.'
                else:
                    result['keys'] = [known[k.lower()] for k in selected]
                if not result['keys']:
                    result.setdefault('key_problem', 'Choose/enter a unique business key; no key is assumed from column names.')
                scope_mode = request.get('scope_mode', 'snapshot')
                etl = date.fromisoformat(str(request.get('etl_date') or '2026-07-01')).isoformat()
                if scope_mode == 'snapshot':
                    if 'sym_run_date' not in known:
                        problems.append('No SYM_RUN_DATE output. Choose a custom target predicate or explicitly select all target rows.')
                    else:
                        result['scope'] = f'{identifier(known["sym_run_date"])} = DATE \'{etl}\''
                elif scope_mode == 'custom':
                    result['scope'] = predicate(request.get('target_filter', ''))
                elif scope_mode == 'all':
                    result['scope'] = 'TRUE'
                else:
                    problems.append('Unknown target scope mode.')
                if request.get('scope_confirmed') is not True:
                    problems.append('Confirm the displayed target scope before copying generated tests.')
                root = build_scope(tree)
                tables = set()
                for source_scope in root.traverse():
                    for _, src in source_scope.selected_sources.values():
                        if isinstance(src, exp.Table):
                            table = src.copy()
                            table.set('alias', None)
                            tables.add(table.sql('trino'))
                result['source_tables'] = sorted(tables)
                if names(request.get('output_columns')):
                    result['warnings'].append('Manual output columns must contain the complete, ordered model output; no catalog is available to verify the list.')
            except (ValueError, TypeError, sqlglot.errors.SqlglotError, AttributeError) as exc:
                problems.append(str(exc))
        result['input_issues'] = problems
        for number, kind in enumerate(['Record Count Validation', 'Duplicate Validation', 'Source vs Target MINUS Validation'], 1):
            issues = list(problems)
            if number == 2 and result.get('key_problem'):
                issues.append(result['key_problem'])
            sql = self.query(number, result) if not issues else ''
            expected = ['Source/target counts match; empty source is reported separately.',
                        '0 duplicate groups and 0 null-key groups.',
                        '0 rows in both directions (duplicate multiplicity included).'][number - 1]
            values = [f'UT{number:03}', kind, result['job'], ', '.join(result.get('source_tables', [])),
                      ', '.join(result['columns']), result['target'],
                      ', '.join(result['keys'] if number == 2 else result['columns']),
                      f'DEV; target WHERE {result["scope"]}; keys={result["keys"]}; mode={request.get("mode") or "not used"}; variables={converted.get("variables", [])}',
                      expected, '', 'Not Executed', str(request.get('etl_date') or '2026-07-01')]
            result['tests'].append({'id': values[0], 'type': kind, 'sql': sql, 'ready': not issues,
                                    'issues': issues, 'expected': expected, 'metadata': dict(zip(HEADERS, values))})
        if all(t['ready'] for t in result['tests']):
            result['status'] = 'ready'
        elif converted['status'] == 'blocked':
            result['status'] = 'blocked'
        return result

    @staticmethod
    def query(number, result):
        # Nest the original query, including all its CTEs, without name collisions
        # with expected_source/target_data generated outside its lexical scope.
        source = result['sql'].rstrip().rstrip(';')
        cols = ', '.join(identifier(c) for c in result['columns'])
        target = result['target']
        scope = result['scope']
        if number == 2:
            keys = ', '.join(identifier(c) for c in result['keys'])
            nulls = ' OR '.join(f'{identifier(k)} IS NULL' for k in result['keys'])
            body = f'''WITH target_data AS (
    SELECT {keys} FROM {target} WHERE {scope}
)
SELECT {keys}, COUNT(*) AS row_count,
       CASE WHEN {nulls} THEN 'NULL_KEY' ELSE 'DUPLICATE_KEY' END AS issue_type
FROM target_data
GROUP BY {keys}
HAVING COUNT(*) > 1 OR {nulls}'''
        else:
            common = f'''WITH expected_source AS (
{source}
), target_data AS (
    SELECT {cols} FROM {target} WHERE {scope}
)'''
            if number == 1:
                body = common + '''
SELECT source_count, target_count, source_count - target_count AS count_difference,
       CASE WHEN source_count = 0 THEN 'EMPTY_SOURCE'
            WHEN source_count = target_count THEN 'PASS' ELSE 'FAIL' END AS status
FROM (SELECT COUNT(*) AS source_count FROM expected_source) s
CROSS JOIN (SELECT COUNT(*) AS target_count FROM target_data) t'''
            else:
                direction = 'ut_diff_direction'
                while direction.lower() in {c.lower() for c in result['columns']}:
                    direction = '_' + direction
                body = common + f''', source_minus_target AS (
    SELECT {cols} FROM expected_source
    EXCEPT ALL
    SELECT {cols} FROM target_data
), target_minus_source AS (
    SELECT {cols} FROM target_data
    EXCEPT ALL
    SELECT {cols} FROM expected_source
)
SELECT 'SOURCE_MINUS_TARGET' AS {direction}, {cols} FROM source_minus_target
UNION ALL
SELECT 'TARGET_MINUS_SOURCE' AS {direction}, {cols} FROM target_minus_source'''
        # Parse the complete artifact too, not just the pasted SQL.
        sqlglot.parse_one(body, read='trino', error_level=sqlglot.ErrorLevel.RAISE)
        return f'-- UT{number:03} | Athena Engine 3 | Not Executed\n' + body + ';\n'
