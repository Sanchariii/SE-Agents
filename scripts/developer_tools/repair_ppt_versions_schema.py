"""One-off, idempotent repair for the `ppt_versions` table schema drift.

The live table was created by an older definition where `files` is a Postgres
`text[]` array and the uniqueness was two single-column constraints. The current
code writes `files` as a JSON string and relies on a composite
`UNIQUE (session_id, deck_version)` for its upsert. This aligns the live schema
with `app/core/handlers/deck_version_handler.py`.

Safe to run repeatedly. Run:
    python -m scripts.developer_tools.repair_ppt_versions_schema
"""

from app.config.constants import PgTables
from app.utilities.pgsql_util import PgsqlUtil

TBL = PgTables.DECK_VERSIONS


def main() -> None:
    pg = PgsqlUtil()

    before = pg.query(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = 'files'", {"t": TBL})
    rows = pg.query(f"SELECT count(*) AS c FROM {TBL}")[0]["c"]
    print(f"[repair] {TBL}: {rows} row(s); files type before = "
          f"{before[0]['data_type'] if before else 'MISSING'}")
    if rows:
        print("[repair] ABORT: table is not empty; refusing to change column types "
              "without a data migration.")
        return

    # 1) files: text[] -> text (json string), matching the code's json.dumps/json.loads.
    pg.execute(f"ALTER TABLE {TBL} ALTER COLUMN files DROP DEFAULT")
    pg.execute(f"ALTER TABLE {TBL} ALTER COLUMN files TYPE text USING files::text")
    pg.execute(f"ALTER TABLE {TBL} ALTER COLUMN files SET DEFAULT '{{}}'")

    # 2) Replace every UNIQUE constraint with the composite the upsert needs.
    pg.execute(
        f"""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = '{TBL}'::regclass AND contype = 'u'
            LOOP
                EXECUTE format('ALTER TABLE {TBL} DROP CONSTRAINT %I', r.conname);
            END LOOP;
        END $$;
        """)
    pg.execute(
        f"ALTER TABLE {TBL} "
        f"ADD CONSTRAINT {TBL}_session_id_deck_version_key UNIQUE (session_id, deck_version)")

    after_type = pg.query(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = 'files'", {"t": TBL})[0]["data_type"]
    uniques = pg.query(
        "SELECT tc.constraint_name, string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) cols "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        "WHERE tc.table_name = :t AND tc.constraint_type = 'UNIQUE' "
        "GROUP BY tc.constraint_name", {"t": TBL})
    print(f"[repair] files type after = {after_type}")
    print(f"[repair] UNIQUE constraints after = "
          f"{[(u['constraint_name'], u['cols']) for u in uniques]}")
    print("[repair] done.")


if __name__ == "__main__":
    main()
