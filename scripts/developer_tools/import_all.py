"""Import every module in the app package to catch broken references.

    python scripts/developer_tools/import_all.py

Useful after a refactor: unit tests only import what they touch, so a module that
nothing imports yet can stay broken until runtime.
"""

import importlib
import os
import pkgutil
import sys
from pathlib import Path

os.environ.setdefault("APP_TYPE", "local")
os.environ.setdefault("APP_ENV", "dev")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    import app

    failures: list[tuple[str, str]] = []
    count = 0
    for module in pkgutil.walk_packages(app.__path__, "app."):
        count += 1
        try:
            importlib.import_module(module.name)
        except Exception as exc:                    # noqa: BLE001
            failures.append((module.name, f"{type(exc).__name__}: {exc}"))

    for name, error in failures:
        print(f"FAIL {name}\n     {error}")
    print(f"\n{count - len(failures)}/{count} modules imported cleanly")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
