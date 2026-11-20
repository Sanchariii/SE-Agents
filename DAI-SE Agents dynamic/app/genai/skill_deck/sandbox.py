"""Run the model's whole-deck program in its own folder, with no inherited secrets.

Before anything runs, the program goes through the SAME static guard the classic
per-slide designer uses (``app.genai.ppt.code_guard``): DeckBuilder only - no
``Presentation(...)``, no ``.shapes.add_*``, no hex colours - and no process, network or
file-deleting modules and calls. A rejected program never starts; the reason goes back
to the model in the normal repair loop.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.config.constants import SkillDeckConstants as C
from app.genai.ppt import code_guard
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def normalize(source: str, run_dir: Path) -> str:
    """Point hard-coded Linux sandbox paths at the run folder.

    Args:
        source: The generated program.
        run_dir: The only folder the program may write to.

    Returns:
        The program with known sandbox prefixes redirected.
    """
    target = f"{Path(run_dir).as_posix()}/"
    for prefix in C.SANDBOX_PATH_PREFIXES:
        if prefix in source:
            log.warning("program hard-coded %s; redirected to the run folder", prefix)
            source = source.replace(prefix, target)
    return source


def _env(run_dir: Path) -> dict:
    """A minimal environment: PATH and temp folders only, so no APIM/DB secrets leak."""
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "TMP": str(run_dir), "TEMP": str(run_dir), "TMPDIR": str(run_dir),
           "HOME": str(run_dir), "USERPROFILE": str(run_dir),
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    return {k: v for k, v in env.items() if v}


def unsafe_reason(source: str) -> str:
    """Why the program may not run (the classic guard's message), or ""."""
    return code_guard.check_message(code_guard.strip_code_fences(source)) or ""


def run_program(source: str, run_dir: Path) -> tuple[bool, str, Path | None]:
    """Check, save and execute the program, then find the deck it wrote.

    Args:
        source: Python source.
        run_dir: The staged run folder (``staging.stage``).

    Returns:
        Tuple of (success, error text, path of the deck or None).
    """
    rejection = unsafe_reason(source)
    if rejection:
        log.warning("program rejected before running: %s",
                    " | ".join(rejection.splitlines()[1:]))
        return False, rejection, None
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    code_path = run_dir / C.CODE_FILENAME
    code_path.write_text(normalize(code_guard.strip_code_fences(source), run_dir),
                         encoding="utf-8")
    deck = run_dir / C.RAW_DECK_FILENAME
    before = deck.stat().st_mtime if deck.exists() else None
    try:
        proc = subprocess.run([sys.executable, code_path.name], cwd=str(run_dir),
                              capture_output=True, text=True, env=_env(run_dir),
                              timeout=C.EXEC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"the program ran longer than {C.EXEC_TIMEOUT_SECONDS}s", None
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout)[-C.MAX_ERROR_CHARS:], None
    if not deck.exists() or (before is not None and deck.stat().st_mtime < before):
        return False, ("the program finished but saved no deck; end it with "
                       "deck.save(OUTPUT_PATH)"), None
    log.info("program wrote %s (%d bytes)", deck.name, deck.stat().st_size)
    return True, "", deck


def builder_warnings(run_dir: Path) -> list[str]:
    """DeckBuilder's own warnings from its report sidecar (empty when there is none)."""
    import json
    from app.genai.ppt.deck_builder import REPORT_FILENAME
    path = Path(run_dir) / REPORT_FILENAME
    try:
        warnings = json.loads(path.read_text(encoding="utf-8")).get("warnings") or []
    except (OSError, ValueError):
        return []
    return [str(w) for w in warnings
            if not any(part in str(w) for part in C.IGNORED_BUILDER_WARNINGS)
            ][:C.MAX_BUILDER_WARNINGS]
