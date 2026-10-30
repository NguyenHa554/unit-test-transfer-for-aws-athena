"""Read-only dbt metadata resolution and conservative Spark → Trino conversion."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import sqlglot
import yaml
from jinja2 import StrictUndefined, TemplateError, nodes
from jinja2.sandbox import SandboxedEnvironment
from jinja2.visitor import NodeTransformer
from sqlglot import exp
from sqlglot.dialects.trino import Trino
from sqlglot.dialects.spark import Spark

SCHEMAS = {'bronze': 'dev_bronze', 'brz': 'dev_bronze', 'landing': 'dev_lnd', 'lnd': 'dev_lnd', 'km_staging': 'dev_km_stg', 'staging': 'dev_km_stg', 'stg': 'dev_km_stg'}
DEFAULT_ETL_DATE = '2026-07-01'
CONFIG = re.compile(r'\{\{\s*config\s*\(.*?\)\s*\}\}', re.S)
MACRO = re.compile(r'\{%[-+]?\s*macro\s+([A-Za-z_]\w*)\s*\(.*?\{%[-+]?\s*endmacro\s*[-+]?%\}', re.S)

# Query expressions use Trino type names, not Hive CREATE TABLE declarations.
# Spark LONG is normalized to BIGINT by the parser; Spark FLOAT is 32-bit.
ATHENA_QUERY_TYPES = {
    exp.DataType.Type.TEXT: 'VARCHAR',
    exp.DataType.Type.INT: 'INTEGER',
    exp.DataType.Type.BIGINT: 'BIGINT',
    exp.DataType.Type.FLOAT: 'REAL',
}


class AthenaQuery(Trino):
    class Generator(Trino.Generator):
        TYPE_MAPPING = {**Trino.Generator.TYPE_MAPPING, **ATHENA_QUERY_TYPES}


class AthenaTypeDraft(Spark):
    """Normalize query types while preserving unconverted Spark functions in a draft."""
    class Generator(Spark.Generator):
        TYPE_MAPPING = {**Spark.Generator.TYPE_MAPPING, **ATHENA_QUERY_TYPES}


def environment():
    return SandboxedEnvironment(undefined=StrictUndefined, extensions=['jinja2.ext.do'], autoescape=False, keep_trailing_newline=True)


class RemoveConfig(NodeTransformer):
    def visit_Call(self, node, *args, **kwargs):
        if isinstance(node.node, nodes.Name) and node.node.name == 'config':
            return nodes.Const('').set_lineno(node.lineno)
        return self.generic_visit(node, *args, **kwargs)


class MacroReturn(Exception):
    def __init__(self, value):
        self.value = value


def dbt_return(value):
    raise MacroReturn(value)


class Metadata:
    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self.dbt = self.repo / 'dbt'
        self.fingerprint = None
        self.models = {}
        self.sources = {}
        self.macros = {}
        self.project = {}
        self.manifest = {}
        self.notices = []

    def refresh(self):
        files = [self.dbt / 'dbt_project.yml', self.dbt / 'target' / 'manifest.json']
        for folder in ('models', 'macros'):
            files.extend(p for p in (self.dbt / folder).rglob('*') if p.suffix.lower() in ('.sql', '.yml', '.yaml'))
        fingerprint = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files if p.is_file())
        if fingerprint == self.fingerprint:
            return
        self.fingerprint = fingerprint
        self.models, self.sources, self.macros = {}, {}, {}
        self.notices = []
        project_path = self.dbt / 'dbt_project.yml'
        self.project = yaml.safe_load(project_path.read_text(encoding='utf-8-sig')) if project_path.exists() else {}
        self.project = self.project or {}
        manifest_path = self.dbt / 'target' / 'manifest.json'
        self.manifest = {}
        if manifest_path.exists():
            try:
                self.manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                self.notices.append('Manifest could not be read; using current source definitions.')
        self.model_yml = {}
        self.model_docs = {}
        for p in files:
            if p.suffix.lower() not in ('.yml', '.yaml') or 'models' not in p.parts:
                continue
            try:
                doc = yaml.safe_load(p.read_text(encoding='utf-8-sig')) or {}
                for model in doc.get('models', []):
                    self.model_yml.setdefault(model['name'], {}).update(model.get('config', {}))
                    self.model_docs.setdefault(model['name'], []).append(model)
                for source in doc.get('sources', []):
                    for table in source.get('tables', []):
                        key = (source['name'], table['name'])
                        self.sources.setdefault(key, []).append((source, table, str(p)))
            except (ValueError, TypeError, KeyError, yaml.YAMLError):
                self.notices.append(f'Could not index YAML: {p.name}')
        for p in files:
            if p.suffix.lower() != '.sql':
                continue
            if 'models' in p.parts:
                self.models.setdefault(p.stem, []).append(p)
            elif 'macros' in p.parts:
                for match in MACRO.finditer(p.read_text(encoding='utf-8-sig')):
                    self.macros.setdefault(match[1], []).append((match[0], str(p)))

    def inherited(self, path):
        config = {}
        node = self.project.get('models', {})
        # dbt accepts project-scoped configs; walk root then folder scopes.
        def take(scope):
            if isinstance(scope, dict):
                config.update({k.lstrip('+'): v for k, v in scope.items() if k.startswith('+')})
        take(node)
        package = self.project.get('name')
        if isinstance(node.get(package), dict):
            node = node[package]
            take(node)
        for part in path.relative_to(self.dbt / 'models').parts[:-1]:
            node = node.get(part, {}) if isinstance(node, dict) else {}
            take(node)
        config.update(self.model_yml.get(path.stem, {}))
        return config


class Converter:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        if not (self.repo / 'dbt' / 'dbt_project.yml').is_file():
            raise ValueError(f'dbt project not found: {self.repo / "dbt"}')
        self.metadata = Metadata(self.repo)
        self.metadata.refresh()

    def summary(self):
        return {'models': len(self.metadata.models), 'sources': len(self.metadata.sources), 'macros': len(self.metadata.macros), 'catalog_verified': False}

    def convert(self, request):
        sql = request.get('sql', '')
        variables = request.get('variables', {})
        mode = request.get('mode', '')
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError('Paste a dbt SELECT query first.')
        if not isinstance(variables, dict):
            raise ValueError('Additional variables must be a JSON object.')
        if mode not in ('', 'full_refresh', 'incremental'):
            raise ValueError('Unknown incremental mode.')
        variables = dict(variables)
        if request.get('etl_date'):
            variables['etl_date'] = request['etl_date']
        for name in ('etl_date', 'ds_dt'):
            value = variables.get(name)
            if value is None or value == '':
                variables.pop(name, None)
                continue
            from datetime import date
            try:
                if not isinstance(value, str) or not re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', value):
                    raise ValueError('Unsupported date format')
                variables[name] = date.fromisoformat(value).isoformat()
            except (TypeError, ValueError):
                raise ValueError(f'{name} must be a valid date in YYYYMMDD or YYYY-MM-DD format.') from None
        date_defaults = {
            'etl_date': (DEFAULT_ETL_DATE, 'Application default (20260701)'),
            'ds_dt': (variables.get('etl_date', DEFAULT_ETL_DATE), 'ETL date' if 'etl_date' in variables else 'Application default (20260701)'),
        }
        self.metadata.refresh()
        meta = self.metadata
        errors, warnings, mappings, notes = [], list(meta.notices), [], []
        used = {}
        env = environment()
        target = SimpleNamespace(name='dev')
        resolving = set()
        missing = object()

        def var(name, default=missing):
            if name in resolving:
                raise ValueError(f'Circular variable definition: {name}')
            if name in variables:
                value, origin = variables[name], 'User input'
            elif name in date_defaults:
                value, origin = date_defaults[name]
            elif default is not missing:
                value, origin = default, 'var() default'
            elif name in meta.project.get('vars', {}):
                value, origin = meta.project['vars'][name], 'dbt_project.yml'
            else:
                errors.append(f'Unknown variable: {name}. Supply it in Additional variables.')
                value, origin = f'__UNKNOWN_VAR_{name}__', 'Unknown'
            if isinstance(value, str) and ('{{' in value or '{%' in value):
                resolving.add(name)
                try:
                    value = env.from_string(value).render()
                finally:
                    resolving.remove(name)
            used[name] = {'name': name, 'value': value, 'origin': origin}
            return value

        def value_render(value):
            if isinstance(value, str) and ('{{' in value or '{%' in value):
                return env.from_string(value).render()
            return value

        def relation(schema, identifier):
            if not schema or not identifier:
                raise ValueError('Missing schema or table identifier.')
            schema, identifier = str(schema), str(identifier)
            if '.' in schema:
                raise ValueError(f'Schema {schema!r} is not a confirmed Athena schema (may be a JDBC database).')
            quote = lambda s: s if re.fullmatch(r'[a-z_][a-z0-9_]*', s) else '"' + s.replace('"', '""') + '"'
            return f'{quote(schema)}.{quote(identifier)}'

        def record(original, resolved, status, evidence):
            row = {'reference': original, 'table': resolved, 'status': status, 'evidence': evidence}
            if row not in mappings:
                mappings.append(row)
            return resolved

        def unknown(original, reason):
            errors.append(f'{original}: {reason}')
            return record(original, f'__unresolved_table_{len(mappings) + 1}__', 'Unknown', reason)

        def ref(*args, **kwargs):
            if not args:
                raise ValueError('ref() requires a model name.')
            name = str(args[-1])
            original = 'ref(' + ', '.join(repr(a) for a in args) + ')'
            if kwargs or len(args) > 1:
                return unknown(original, 'Package/version-specific references need explicit resolution.')
            paths = meta.models.get(name, [])
            if len(paths) > 1:
                return unknown(original, 'Multiple current models have this name.')
            if paths:
                path = paths[0]
                text = path.read_text(encoding='utf-8-sig')
                cfg = meta.inherited(path)
                config_env = environment()
                config_env.globals.update(var=var, target=target)
                try:
                    ast = config_env.parse(text)
                    for call in ast.find_all(nodes.Call):
                        if not isinstance(call.node, nodes.Name) or call.node.name != 'config':
                            continue
                        for kw in call.kwargs:
                            if kw.key in ('alias', 'schema', 'materialized'):
                                template = nodes.Template([nodes.Output([kw.value])])
                                cfg[kw.key] = config_env.from_string(template).render()
                        for arg in call.args:
                            if isinstance(arg, nodes.Dict):
                                for pair in arg.items:
                                    if isinstance(pair.key, nodes.Const) and pair.key.value in ('alias', 'schema', 'materialized'):
                                        cfg[pair.key.value] = config_env.from_string(nodes.Template([nodes.Output([pair.value])])).render()
                    cfg = {k: value_render(v) for k, v in cfg.items() if k in ('alias', 'schema', 'materialized')}
                    if cfg.get('materialized') == 'ephemeral':
                        return unknown(original, 'Ephemeral model requires inlining; no physical table exists.')
                    layer = path.relative_to(meta.dbt / 'models').parts[0]
                    schema = cfg.get('schema')
                    alias = cfg.get('alias', name)
                    status = 'Verified (dbt)' if cfg.get('alias') and cfg.get('schema') else 'Rule-based'
                    result = relation(schema, alias)
                    return record(original, result, status, f'Current source: {path.relative_to(self.repo)}; Athena catalog not checked')
                except (TemplateError, ValueError, TypeError) as exc:
                    return unknown(original, f'Cannot resolve current config: {exc}')
            # Manifest is a fallback only when no current model exists.
            candidates = [n for n in meta.manifest.get('nodes', {}).values() if n.get('resource_type') == 'model' and n.get('name') == name]
            if len(candidates) == 1:
                node = candidates[0]
                source_path = self.repo / 'dbt' / node.get('original_file_path', '')
                if not source_path.is_file():
                    return unknown(original, 'Manifest model has no current source file; metadata may be stale.')
            return unknown(original, 'Current model definition not found. No physical name is guessed.')

        def source(source_name, table_name):
            original = f'source({source_name!r}, {table_name!r})'
            candidates = meta.sources.get((source_name, table_name), [])
            if len(candidates) == 1:
                src, table, path = candidates[0]
                try:
                    schema = value_render(src.get('schema', source_name))
                    identifier = value_render(table.get('identifier', table_name))
                    return record(original, relation(schema, identifier), 'Verified (dbt)', f'Current YAML: {Path(path).relative_to(self.repo)}; Athena catalog not checked')
                except (ValueError, TemplateError) as exc:
                    return unknown(original, str(exc))
            if len(candidates) > 1:
                return unknown(original, 'Ambiguous source definitions.')
            return unknown(original, 'Current source YAML missing; no physical name is guessed.')

        def incremental():
            if not mode:
                raise ValueError('Choose Full refresh or Incremental: this query calls is_incremental().')
            notes.append(f'is_incremental() evaluated as {mode == "incremental"}.')
            return mode == 'incremental'

        def fail(message):
            raise ValueError(str(message))

        env.globals.update(var=var, ref=ref, source=source, target=target, config=lambda *a, **k: '', is_incremental=incremental, execute=False, **{'return': dbt_return}, exceptions=SimpleNamespace(raise_compiler_error=fail))
        if request.get('this_relation'):
            env.globals['this'] = request['this_relation']
        # Add a name-only lookup without changing the existing SQL/ref pipeline.
        bare_name = re.fullmatch(r'([A-Za-z_]\w*)\s*;?', sql.strip())
        if bare_name and ('_' in bare_name[1] or bare_name[1] in meta.models):
            name = bare_name[1]
            resolved = ref(name)
            for mapping in mappings:
                if mapping['reference'] == f'ref({name!r})':
                    mapping['reference'] = name
            notes.append('Model-name lookup uses the same alias/schema resolver as ref(); the result is a relation name, not a SELECT query.')
            warnings.append('Athena/Glue table existence and column types have not been verified.')
            if any(mapping['status'] == 'Rule-based' for mapping in mappings):
                warnings.append('Rule-based relation names require confirmation against your Athena catalog.')
            return {'status': 'blocked' if errors else 'converted', 'output_kind': 'relation', 'sql': resolved, 'mappings': mappings, 'variables': list(used.values()), 'notes': notes, 'warnings': warnings, 'errors': list(dict.fromkeys(errors)), 'metadata': self.summary()}
        # Lazy macro wrappers resolve only macros actually invoked by this query.
        def macro_wrapper(name):
            def call(*args, **kwargs):
                definitions = meta.macros[name]
                if len(definitions) != 1:
                    raise ValueError(f'Ambiguous macro: {name}')
                body, path = definitions[0]
                if re.search(r'\b(run_query|statement|adapter|load_result|modules|graph)\b', body):
                    raise ValueError(f'Macro {name} needs dbt runtime/database context ({Path(path).name}).')
                try:
                    module = env.from_string(body).make_module()
                    result = getattr(module, name)(*args, **kwargs)
                except MacroReturn as returned:
                    result = returned.value
                notes.append(f'Expanded macro {name} from current repo definition.')
                return result
            return call
        for name in meta.macros:
            env.globals.setdefault(name, macro_wrapper(name))
        rendered = CONFIG.sub('', sql)
        try:
            ast = RemoveConfig().visit(env.parse(sql))
            rendered = env.from_string(ast).render()
        except (TemplateError, ValueError, TypeError, RecursionError) as exc:
            errors.append(f'Jinja rendering blocked: {exc}')
        output = rendered.strip()
        if re.search(r'\{[{%#]', rendered):
            errors.append('Unresolved Jinja remains. This output is a draft, not executable Athena SQL.')
        else:
            try:
                trees = sqlglot.parse(rendered, read='spark', error_level=sqlglot.ErrorLevel.RAISE)
                if len(trees) != 1 or not isinstance(trees[0], exp.Query):
                    errors.append('Only one SELECT/WITH query is supported; DDL and write statements are not converted.')
                else:
                    tree = trees[0]
                    type_changes = []
                    for dtype in tree.find_all(exp.DataType):
                        if dtype.this in ATHENA_QUERY_TYPES:
                            source_type = dtype.sql(dialect='spark')
                            target_type = dtype.sql(dialect=AthenaQuery)
                            if source_type != target_type:
                                type_changes.append(f'{source_type} → {target_type}')
                    if type_changes:
                        notes.append('Athena query data types: ' + '; '.join(dict.fromkeys(type_changes)) + '. FLOAT → REAL retains 32-bit precision; DOUBLE and DECIMAL precision/scale are unchanged.')
                    # A blocked macro/function must not prevent independent type
                    # normalization. This remains a Spark draft until the full
                    # Trino conversion succeeds; no unsupported function is guessed.
                    output = tree.sql(dialect=AthenaTypeDraft, pretty=True) + ';'
                    for hint in list(tree.find_all(exp.Hint)):
                        hint.pop()
                        notes.append('Removed Spark execution hints (for example BROADCAST), unsupported by Athena; relational logic retained.')
                    self._audit(tree, errors)
                    if not errors:
                        tree = self._compatible_functions(tree, notes)
                        output = tree.sql(dialect=AthenaQuery, pretty=True, unsupported_level=sqlglot.ErrorLevel.RAISE) + ';'
                        sqlglot.parse_one(output, read='trino', error_level=sqlglot.ErrorLevel.RAISE)
                        if tree.find(exp.Cast):
                            warnings.append('CAST behavior depends on source types and Spark ANSI settings; validate invalid inputs, numeric rounding/overflow and timestamp conversions against the source runtime.')
                        notes.append('Spark → Trino conversion parsed successfully; no SQL optimizer was run.')
                        notes.append('Joins, filters, CASE, UNION and window expressions retained by syntax-tree conversion.')
            except (sqlglot.errors.SqlglotError, ValueError, TypeError) as exc:
                errors.append(f'SQL conversion blocked: {exc}')
        warnings.append('Athena/Glue tables, column types and query execution have not been verified.')
        if errors:
            warnings.append('SQL draft only: full Athena conversion did not complete. Types are normalized when parsing succeeds; unsupported Spark functions may remain. Do not execute this draft in Athena.')
        if any(row['status'] == 'Rule-based' for row in mappings):
            warnings.append('Rule-based relation names require confirmation against your Athena catalog.')
        return {'status': 'blocked' if errors else 'converted', 'sql': output, 'mappings': mappings, 'variables': list(used.values()), 'notes': list(dict.fromkeys(notes)), 'warnings': list(dict.fromkeys(warnings)), 'errors': list(dict.fromkeys(errors)), 'metadata': self.summary()}

    @staticmethod
    def _compatible_functions(tree, notes):
        def rewrite(node):
            if isinstance(node, exp.AddMonths):
                # Spark ADD_MONTHS returns DATE, even for timestamp input.
                # Trino DATE_ADD retains its input type, so normalize to DATE.
                notes.append("ADD_MONTHS(start, months) → DATE_ADD('month', months, CAST(start AS DATE)); retain DATE output and calendar-month arithmetic.")
                return exp.DateAdd(
                    this=exp.Cast(this=node.this.copy(), to=exp.DataType.build('DATE')),
                    expression=node.expression.copy(),
                    unit=exp.Var(this='MONTH'),
                )
            if isinstance(node, exp.Cast) and node.to.this in {exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT}:
                operand = node.this
                numeric = isinstance(operand, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Ceil, exp.Floor, exp.Round)) or (isinstance(operand, exp.Literal) and operand.is_number)
                if numeric:
                    # Trino rounds decimal-to-integer; Spark truncates toward zero.
                    node.set('this', exp.Anonymous(this='TRUNCATE', expressions=[operand.copy()]))
                    notes.append('Numeric expression → integer casts truncate toward zero before casting, matching Spark.')
                return node
            if isinstance(node, exp.RegexpExtract):
                notes.append('REGEXP_EXTRACT: preserve null arguments and Spark empty string on no match.')
                inputs = [value for value in node.args.values() if isinstance(value, exp.Expression)]
                checks = [exp.Is(this=value.copy(), expression=exp.Null()) for value in inputs]
                condition = checks[0]
                for check in checks[1:]:
                    condition = exp.Or(this=condition, expression=check)
                coalesced = exp.Coalesce(this=node.copy(), expressions=[exp.Literal.string('')])
                return exp.Case(ifs=[exp.If(this=condition, true=exp.Null())], default=coalesced)
            if isinstance(node, exp.TsOrDsToDate) and node.args.get('safe'):
                notes.append('TO_DATE: use TRY around date parsing to retain null for invalid dates.')
                return exp.Anonymous(this='TRY', expressions=[node.copy()])
            if isinstance(node, (exp.Least, exp.Greatest)):
                # Spark skips null operands; Trino LEAST/GREATEST propagate null.
                args = [node.this, *node.expressions]
                array = exp.Array(expressions=[arg.copy() for arg in args])
                variable = exp.Identifier(this='__athena_nonnull')
                predicate = exp.Not(this=exp.Is(this=variable.copy(), expression=exp.Null()))
                filtered = exp.ArrayFilter(this=array, expression=exp.Lambda(this=predicate, expressions=[variable]))
                name = 'ARRAY_MIN' if isinstance(node, exp.Least) else 'ARRAY_MAX'
                notes.append('LEAST/GREATEST: preserve Spark null-skipping behavior using array_min/array_max over non-null operands.')
                return exp.Anonymous(this=name, expressions=[filtered])
            return node
        for node in reversed(list(tree.walk())):
            replacement = rewrite(node)
            if replacement is not node:
                if node.parent is None:
                    tree = replacement
                else:
                    node.replace(replacement)
        return tree

    @staticmethod
    def _audit(tree, errors):
        for node in tree.walk():
            name = node.sql_name().lower() if isinstance(node, exp.Func) else ''
            if isinstance(node, exp.Anonymous):
                name = node.name.lower()
            if name in {'xxhash64', 'hash', 'crc32', 'conv', 'unix_timestamp', 'from_unixtime', 'to_utc_timestamp', 'from_utc_timestamp', 'monotonically_increasing_id', 'get_json_object', 'collect_set', 'collect_list', 'explode', 'posexplode', 'stack'}:
                errors.append(f'{name}: exact Spark/Athena semantics are not established; manual conversion required.')
            if isinstance(node, exp.Anonymous):
                # Repository CL_INVOICE uses element_at(split(RECID, '-'), 1).
                # A non-null split has at least one element: index 1 cannot be
                # out of bounds, so Spark ANSI error behavior is irrelevant.
                args = node.expressions
                first_split = (name == 'element_at' and len(args) == 2
                               and isinstance(args[0], exp.RegexpSplit)
                               and isinstance(args[1], exp.Literal) and not args[1].is_string
                               and args[1].this == '1'
                               and isinstance(args[0].expression, exp.Literal)
                               and args[0].expression.is_string
                               and re.fullmatch(r'[A-Za-z0-9_ ,:/-]+', args[0].expression.this))
                if not first_split:
                    errors.append(f'Function {node.name} is not recognized by the Spark parser; no verified conversion is available.')
            # Spark array subscripts start at zero; Trino arrays start at one.
            # Let SQLGlot perform this rewrite rather than changing text manually.
        if tree.find(exp.CurrentTimestamp):
            errors.append('CURRENT_TIMESTAMP depends on session timezone; supply an explicit timestamp or verify the conversion manually.')
