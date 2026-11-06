"""Backfill `ppt_versions` from the Redis session store for one session.

Decks generated while the `ppt_versions` schema was broken were saved to Redis
and Blob but never recorded in Postgres, so they don't show up in
`GET /decks/{sid}/versions`. This re-records them from the session store.

Run:
    python -m scripts.developer_tools.backfill_deck_versions <session_id>
"""

import sys

from app.core.handlers.deck_version_handler import deck_version_handler
from app.core.handlers.session_store import session_store


def _language_from_path(path: str) -> str:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    lang = stem.rsplit("_", 1)[-1] if "_" in stem else "english"
    return lang.capitalize()


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m scripts.developer_tools.backfill_deck_versions <session_id>")
        return
    sid = sys.argv[1]

    decks = session_store.list_decks(sid)
    if not decks:
        print(f"[backfill] no decks in the session store for {sid}; nothing to do.")
        return

    draft = session_store.get_draft(sid) or {}
    theme = draft.get("theme")
    title = draft.get("title")

    recorded = 0
    for d in decks:
        language = _language_from_path(d.storage_path)
        plan_title = title or (d.plan_snapshot or {}).get("title")
        deck_version_handler.record_version(
            sid, d.deck_version, d.storage_path,
            theme=theme, plan_title=plan_title,
            files={language: d.storage_path})
        recorded += 1
        print(f"[backfill] recorded v{d.deck_version} ({language}) -> {d.storage_path}")

    back = deck_version_handler.list_versions(sid)
    print(f"[backfill] {recorded} version(s) written; list_versions now -> "
          f"{[v.deck_version for v in back]}")


if __name__ == "__main__":
    main()
