"""One-off migration of a Postgres schema (DDL + data) from Dev to QA.

Standalone tool: it opens its own connections to two *different* databases (Dev
source, QA target) so it does not go through ``app.utilities.pgsql_util`` /
``get_settings()``, which only ever describe a single environment at a time.

Sequence (matches the request this script was built for):
  1. connect to Dev and list all tables in the target schema
  2. extract the DDL for each table (columns, primary key) from Dev
  3. create the corresponding tables in QA
  4. export the Dev data to JSON (one file per table)
  5. bulk-insert the exported data into the QA tables
  6. (after data load, for performance) add foreign keys and secondary indexes

Fill in the DEV_DB / QA_DB placeholders below (or set the matching environment
variables) before running. Nothing here reads `app.config.settings`; these are
throwaway/manual credentials for a one-time migration, not a service secret.

Run:
    python -m scripts.developer_tools.migrate_dev_to_qa --schema public
    python -m scripts.developer_tools.migrate_dev_to_qa --schema public --dry-run
    python -m scripts.developer_tools.migrate_dev_to_qa --schema public --tables users,orders
"""

import argparse
import base64
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.utilities.logger_util import get_logger, setup_logging

log = get_logger(__name__)

# --------------------------------------------------------------------------
# CONFIG — fill these in, or export the matching env vars, before running.
# Placeholders are wrapped in <...> so a forgotten one fails loudly at startup
# instead of silently trying to connect with garbage credentials.
# --------------------------------------------------------------------------
DEV_DB: dict[str, Any] = {
    "host": os.getenv("DEV_PG_HOST", "azr-pgsql-dai-sea-300.postgres.database.azure.com"),
    "port": int(os.getenv("DEV_PG_PORT", "5432")),
    "dbname": os.getenv("DEV_PG_DATABASE", "sed"),
    "user": os.getenv("DEV_PG_USERNAME", "sedev"),
    "password": os.getenv("DEV_PG_PASSWORD", "j7QbpeKmk57G"),
    "sslmode": os.getenv("DEV_PG_SSLMODE", "require"),
}

QA_DB: dict[str, Any] = {
    "host": os.getenv("QA_PG_HOST", "<QA_PG_HOST>"),
    "port": int(os.getenv("QA_PG_PORT", "5432")),
    "dbname": os.getenv("QA_PG_DATABASE", "<QA_PG_DATABASE>"),
    "user": os.getenv("QA_PG_USERNAME", "<QA_PG_USERNAME>"),
    "password": os.getenv("QA_PG_PASSWORD", "<QA_PG_PASSWORD>"),
    "sslmode": os.getenv("QA_PG_SSLMODE", "require"),
}

DEFAULT_SCHEMA = os.getenv("MIGRATE_SCHEMA", "public")
DEFAULT_EXPORT_DIR = Path(__file__).resolve().parent / "_migration_export"
BULK_INSERT_PAGE_SIZE = 1000

# Postgres internal array element names -> SQL type names, for reconstructing
# `element_type[]` column definitions (information_schema hides this detail).
_ARRAY_ELEMENT_TYPES = {
    "int2": "smallint", "int4": "integer", "int8": "bigint",
    "float4": "real", "float8": "double precision", "numeric": "numeric",
    "text": "text", "varchar": "character varying", "bpchar": "character",
    "bool": "boolean", "uuid": "uuid", "date": "date",
    "timestamp": "timestamp", "timestamptz": "timestamp with time zone",
    "jsonb": "jsonb", "json": "json",
}


def _require_config(cfg: dict[str, Any], label: str) -> None:
    """Fail fast with a clear message if a config placeholder was never filled in.

    Args:
        cfg: One of ``DEV_DB`` / ``QA_DB``.
        label: ``"DEV"`` or ``"QA"``, used in the error message.

    Raises:
        SystemExit: if any value still looks like a ``<PLACEHOLDER>``.
    """
    missing = [k for k, v in cfg.items() if isinstance(v, str) and v.startswith("<") and v.endswith(">")]
    if missing:
        raise SystemExit(
            f"[migrate] {label}_DB is not configured — fill in {missing} at the top of "
            f"this script (or set the corresponding {label}_PG_* environment variables) "
            f"before running."
        )


def _connect(cfg: dict[str, Any], label: str):
    """Open a psycopg2 connection to Dev or QA and log the outcome.

    Args:
        cfg: Connection kwargs (host/port/dbname/user/password/sslmode).
        label: ``"DEV"`` or ``"QA"``, used for logging only.

    Returns:
        An open ``psycopg2`` connection.
    """
    import psycopg2
    conn = psycopg2.connect(**cfg)
    conn.autocommit = False
    log.info("connected to %s postgres (host=%s, db=%s)", label, cfg["host"], cfg["dbname"])
    return conn


def list_tables(conn, schema: str) -> list[str]:
    """List base tables in ``schema``, ordered alphabetically.

    Args:
        conn: Dev connection.
        schema: Target schema name.

    Returns:
        Table names (no schema prefix).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
            (schema,),
        )
        tables = [r[0] for r in cur.fetchall()]
    log.info("found %d table(s) in %s.*: %s", len(tables), schema, tables)
    return tables


def _foreign_key_deps(conn, schema: str, tables: list[str]) -> dict[str, set[str]]:
    """Map each table to the (in-schema) tables its foreign keys point at.

    Used only to order table creation / data load so parents load before children;
    self-references and cross-schema references are ignored for ordering purposes.
    """
    deps: dict[str, set[str]] = {t: set() for t in tables}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.table_name, ccu.table_name AS foreign_table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
            """,
            (schema,),
        )
        for table_name, foreign_table_name in cur.fetchall():
            if table_name in deps and foreign_table_name in deps and foreign_table_name != table_name:
                deps[table_name].add(foreign_table_name)
    return deps


def topological_order(conn, schema: str, tables: list[str]) -> list[str]:
    """Order tables so a table always comes after every table it FKs to.

    Falls back to the original (alphabetical) order — logging a warning — if a
    dependency cycle is found, since a strict topological order doesn't exist then.
    """
    deps = _foreign_key_deps(conn, schema, tables)
    ordered: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(t for t in remaining if deps[t] <= (set(tables) - remaining))
        if not ready:
            log.warning("foreign-key cycle detected among %s; falling back to alphabetical order", sorted(remaining))
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        remaining -= set(ready)
    return ordered


def _column_type_sql(row: dict[str, Any]) -> str:
    """Render a single ``information_schema.columns`` row as a SQL type expression."""
    data_type = row["data_type"]
    if data_type == "ARRAY":
        elem = row["udt_name"].lstrip("_")
        return f"{_ARRAY_ELEMENT_TYPES.get(elem, elem)}[]"
    if data_type == "USER-DEFINED":
        return row["udt_name"]
    if data_type in ("character varying", "character") and row["character_maximum_length"]:
        return f"{data_type}({row['character_maximum_length']})"
    if data_type == "numeric" and row["numeric_precision"] is not None:
        scale = row["numeric_scale"] or 0
        return f"numeric({row['numeric_precision']},{scale})"
    return data_type


def get_columns(conn, schema: str, table: str) -> list[dict[str, Any]]:
    """Fetch ordered column metadata for a table from ``information_schema``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, udt_name, is_nullable, column_default,
                   character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        cols = [dict(zip((d[0] for d in cur.description), r)) for r in cur.fetchall()]
    return cols


def get_primary_key_columns(conn, schema: str, table: str) -> list[str]:
    """Return the ordered primary-key column names for a table (empty if none)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """,
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]


def build_create_table_sql(conn, schema: str, table: str) -> tuple[str, dict[str, str]]:
    """Build the ``CREATE TABLE`` statement for one table, from Dev's metadata.

    Serial-style defaults (``nextval(...)``) are rewritten to
    ``GENERATED BY DEFAULT AS IDENTITY`` instead of being copied verbatim, since
    the underlying sequence only exists in Dev.

    Returns:
        A tuple of (the CREATE TABLE SQL, a map of column_name -> pg data_type,
        used later to adapt values on insert).
    """
    columns = get_columns(conn, schema, table)
    pk_cols = get_primary_key_columns(conn, schema, table)
    type_map = {c["column_name"]: c["data_type"] for c in columns}

    lines = []
    for c in columns:
        type_sql = _column_type_sql(c)
        parts = [f'"{c["column_name"]}"', type_sql]
        default = c["column_default"]
        if default and "nextval(" in default:
            parts.append("GENERATED BY DEFAULT AS IDENTITY")
        elif default is not None:
            parts.append(f"DEFAULT {default}")
        if c["is_nullable"] == "NO":
            parts.append("NOT NULL")
        lines.append("  " + " ".join(parts))
    if pk_cols:
        pk_list = ", ".join(f'"{c}"' for c in pk_cols)
        lines.append(f"  PRIMARY KEY ({pk_list})")

    ddl = f'CREATE TABLE "{schema}"."{table}" (\n' + ",\n".join(lines) + "\n);"
    return ddl, type_map


def get_foreign_key_statements(conn, schema: str, table: str) -> list[str]:
    """Build ``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`` statements for a table.

    Applied after data load so bulk inserts aren't slowed down / blocked by FK checks
    while parent tables are still being populated.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.constraint_name, kcu.column_name, ccu.table_schema AS foreign_schema,
                   ccu.table_name AS foreign_table, ccu.column_name AS foreign_column,
                   rc.update_rule, rc.delete_rule, kcu.ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
            WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'FOREIGN KEY'
            ORDER BY tc.constraint_name, kcu.ordinal_position
            """,
            (schema, table),
        )
        rows = cur.fetchall()

    by_constraint: dict[str, dict[str, Any]] = defaultdict(lambda: {"cols": [], "fcols": []})
    for name, col, fschema, ftable, fcol, update_rule, delete_rule, _ in rows:
        entry = by_constraint[name]
        entry.update(foreign_schema=fschema, foreign_table=ftable, update_rule=update_rule, delete_rule=delete_rule)
        entry["cols"].append(col)
        entry["fcols"].append(fcol)

    statements = []
    for name, e in by_constraint.items():
        cols = ", ".join(f'"{c}"' for c in e["cols"])
        fcols = ", ".join(f'"{c}"' for c in e["fcols"])
        statements.append(
            f'ALTER TABLE "{schema}"."{table}" ADD CONSTRAINT "{name}" '
            f'FOREIGN KEY ({cols}) REFERENCES "{e["foreign_schema"]}"."{e["foreign_table"]}" ({fcols}) '
            f'ON UPDATE {e["update_rule"]} ON DELETE {e["delete_rule"]};'
        )
    return statements


def get_index_statements(conn, schema: str, table: str) -> list[str]:
    """Return ``CREATE INDEX`` statements for indexes not already implied by the primary key."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname = %s AND tablename = %s
            """,
            (schema, table),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND table_name = %s AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')",
            (schema, table),
        )
        constraint_names = {r[0] for r in cur.fetchall()}
    return [indexdef + ";" for indexname, indexdef in rows if indexname not in constraint_names]


def _json_default(obj: Any) -> Any:
    """``json.dumps(default=...)`` hook for the Postgres value types JSON can't hold natively."""
    import datetime
    import uuid
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (bytes, memoryview)):
        return {"__bytea__": base64.b64encode(bytes(obj)).decode("ascii")}
    raise TypeError(f"cannot JSON-serialize {type(obj)!r}")


def export_table_json(conn, schema: str, table: str, type_map: dict[str, str], export_dir: Path) -> Path:
    """Export every row of a table to a JSON file (columns + pg types + rows).

    Args:
        conn: Dev connection.
        schema: Schema name.
        table: Table name.
        type_map: column_name -> pg data_type, stashed alongside the rows so the
            insert step can re-adapt jsonb/bytea values correctly.
        export_dir: Directory to write ``<table>.json`` into (created if missing).

    Returns:
        Path to the written JSON file.
    """
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{schema}"."{table}"')
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]

    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{table}.json"
    payload = {"schema": schema, "table": table, "columns": columns, "types": type_map, "rows": rows}
    path.write_text(json.dumps(payload, default=_json_default), encoding="utf-8")
    log.info("exported %d row(s) from %s.%s -> %s", len(rows), schema, table, path)
    return path


def _adapt_value(value: Any, pg_type: str):
    """Convert a JSON-round-tripped value back to what psycopg2 needs for ``pg_type``."""
    import psycopg2.extras
    if value is None:
        return None
    if isinstance(value, dict) and set(value.keys()) == {"__bytea__"}:
        return psycopg2.Binary(base64.b64decode(value["__bytea__"]))
    if pg_type in ("json", "jsonb") and isinstance(value, (dict, list)):
        return psycopg2.extras.Json(value)
    return value


def bulk_insert(conn, schema: str, table: str, payload: dict[str, Any]) -> int:
    """Bulk-insert previously exported rows into the QA table.

    Args:
        conn: QA connection.
        schema: Schema name.
        table: Table name.
        payload: The dict produced by ``export_table_json`` (columns/types/rows).

    Returns:
        Number of rows inserted.
    """
    from psycopg2.extras import execute_values

    columns: list[str] = payload["columns"]
    types: dict[str, str] = payload["types"]
    rows: list[dict[str, Any]] = payload["rows"]
    if not rows:
        log.info("no rows to insert for %s.%s", schema, table)
        return 0

    col_list = ", ".join(f'"{c}"' for c in columns)
    insert_sql = f'INSERT INTO "{schema}"."{table}" ({col_list}) VALUES %s'
    values = [tuple(_adapt_value(row.get(c), types.get(c, "")) for c in columns) for row in rows]

    with conn.cursor() as cur:
        execute_values(cur, insert_sql, values, page_size=BULK_INSERT_PAGE_SIZE)
    log.info("inserted %d row(s) into %s.%s", len(values), schema, table)
    return len(values)


def main() -> None:
    """Run the Dev -> QA migration end to end (see module docstring for the sequence)."""
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="Schema to migrate (default: %(default)s)")
    parser.add_argument("--tables", default=None, help="Comma-separated subset of tables (default: all base tables)")
    parser.add_argument("--export-dir", default=str(DEFAULT_EXPORT_DIR), help="Where to write the per-table JSON export")
    parser.add_argument("--drop-existing", action="store_true", help="DROP TABLE IF EXISTS ... CASCADE in QA before creating")
    parser.add_argument("--skip-export", action="store_true", help="Reuse JSON files already on disk instead of re-exporting from Dev")
    parser.add_argument("--dry-run", action="store_true", help="Print planned DDL/row counts; don't touch QA")
    args = parser.parse_args()

    _require_config(DEV_DB, "DEV")
    _require_config(QA_DB, "QA")

    export_dir = Path(args.export_dir)
    dev_conn = _connect(DEV_DB, "DEV")
    qa_conn = None if args.dry_run else _connect(QA_DB, "QA")

    try:
        # step 1: list tables in the target schema
        all_tables = list_tables(dev_conn, args.schema)
        tables = [t.strip() for t in args.tables.split(",")] if args.tables else all_tables
        order = topological_order(dev_conn, args.schema, tables)

        ddls: dict[str, str] = {}
        type_maps: dict[str, dict[str, str]] = {}
        for table in order:
            # step 2: extract DDL for each table
            ddl, type_map = build_create_table_sql(dev_conn, args.schema, table)
            ddls[table] = ddl
            type_maps[table] = type_map
            log.info("DDL for %s.%s:\n%s", args.schema, table, ddl)

            # step 3: create the table in QA
            if args.dry_run:
                continue
            with qa_conn.cursor() as cur:
                if args.drop_existing:
                    cur.execute(f'DROP TABLE IF EXISTS "{args.schema}"."{table}" CASCADE')
                cur.execute(ddl)
            qa_conn.commit()
            log.info("created %s.%s in QA", args.schema, table)

        payloads: dict[str, dict[str, Any]] = {}
        for table in order:
            # step 4: export Dev data to JSON
            path = export_dir / f"{table}.json"
            if args.skip_export and path.exists():
                payloads[table] = json.loads(path.read_text(encoding="utf-8"))
                log.info("reusing existing export for %s (%d rows)", table, len(payloads[table]["rows"]))
            else:
                path = export_table_json(dev_conn, args.schema, table, type_maps[table], export_dir)
                payloads[table] = json.loads(path.read_text(encoding="utf-8"))

            # step 5: bulk-insert into QA
            if args.dry_run:
                log.info("[dry-run] would insert %d row(s) into %s.%s", len(payloads[table]["rows"]), args.schema, table)
                continue
            bulk_insert(qa_conn, args.schema, table, payloads[table])
            qa_conn.commit()

        if not args.dry_run:
            # applied last, after every table has data, so FK checks don't block load order
            for table in order:
                fk_statements = get_foreign_key_statements(dev_conn, args.schema, table)
                index_statements = get_index_statements(dev_conn, args.schema, table)
                with qa_conn.cursor() as cur:
                    for stmt in fk_statements + index_statements:
                        cur.execute(stmt)
                qa_conn.commit()
                if fk_statements or index_statements:
                    log.info("applied %d FK(s) and %d index(es) on %s.%s",
                             len(fk_statements), len(index_statements), args.schema, table)

        log.info("migration complete: %d table(s) processed (%s)", len(order), ", ".join(order))
    except Exception:
        if qa_conn is not None:
            qa_conn.rollback()
        log.exception("migration failed; QA changes for the in-flight table were rolled back")
        raise
    finally:
        dev_conn.close()
        if qa_conn is not None:
            qa_conn.close()


if __name__ == "__main__":
    sys.exit(main())
