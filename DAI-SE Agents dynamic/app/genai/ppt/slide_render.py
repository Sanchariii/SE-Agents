"""Render one single-slide .pptx to a PNG with headless LibreOffice (max mode).

Max mode needs a picture of what a generated program actually drew, and nothing in
python-pptx can draw. LibreOffice can: `soffice --headless --convert-to png` writes the
first (only) slide of a file as an image.

Three things make that safe to do from worker threads:

* every render holds one slot of a small pool, and each slot has its own LibreOffice
  profile, so two renders never share a profile (soffice refuses to) and the first-run
  profile cost is paid once per slot, not once per slide;
* the process runs in its own process group with a minimal environment (no secrets),
  and a timeout or a stop kills the whole tree, not just the wrapper;
* consecutive failures open a circuit breaker, so a broken install turns max mode off
  for the life of the process instead of costing every slide a timeout.

This module is never imported by `deck_builder.py`, which is copied into the sandbox.
"""

import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

from app.config.constants import SlideRenderConstants as RC
from app.config.exceptions import SlideRenderError
from app.utilities import run_control
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_slots: "queue.Queue[int] | None" = None
_slots_lock = threading.Lock()
_failures = 0
_failures_lock = threading.Lock()
_renders = 0


@lru_cache(maxsize=1)
def find_soffice() -> str | None:
    """The LibreOffice binary to run, or None when none is installed.

    Order: the `soffice_path` setting, then PATH, then the standard Windows folders.

    Returns:
        An absolute path, or None.
    """
    try:
        from app.config.settings import get_settings
        configured = (get_settings().soffice_path or "").strip()
    except Exception as exc:                                  # noqa: BLE001
        log.warning("settings unavailable while locating LibreOffice (%s); "
                    "autodetecting", exc)
        configured = ""
    if configured:
        if Path(configured).is_file():
            log.info("LibreOffice from settings: %s", configured)
            return _console_binary(configured)
        log.error("soffice_path %r does not exist; autodetecting instead", configured)
    for name in RC.SOFFICE_NAMES:
        found = shutil.which(name)
        if found:
            log.info("LibreOffice on PATH: %s", found)
            return _console_binary(found)
    if sys.platform.startswith("win"):
        for candidate in RC.WINDOWS_DEFAULT_PATHS:
            if Path(candidate).is_file():
                log.info("LibreOffice in its Windows install folder: %s", candidate)
                return candidate
    log.warning("LibreOffice not found; max mode is unavailable in this process")
    return None


def _console_binary(path: str) -> str:
    """On Windows, `soffice.com` (which waits for the conversion) over `soffice.exe`."""
    candidate = Path(path)
    if candidate.suffix.lower() == ".exe":
        console = candidate.with_suffix(".com")
        if console.is_file():
            log.debug("using the console launcher %s", console)
            return str(console)
    return path


def available() -> bool:
    """Whether a slide can be rendered right now.

    Not cached as a whole: the circuit breaker can open at any time.

    Returns:
        True when a binary exists and the breaker is closed.
    """
    if find_soffice() is None:
        return False
    with _failures_lock:
        broken = _failures >= RC.FAILURES_TO_DISABLE
    if broken:
        log.debug("renderer disabled after %d consecutive failure(s)", _failures)
    return not broken


def _record(ok: bool) -> None:
    """Move the circuit breaker after one render."""
    global _failures
    with _failures_lock:
        if ok:
            _failures = 0
            return
        _failures += 1
        count = _failures
    if count >= RC.FAILURES_TO_DISABLE:
        log.error("slide renderer failed %d time(s) in a row; max mode is OFF for the "
                  "rest of this process", count)


def _slot_pool() -> "queue.Queue[int]":
    """The pool of profile slots, created on first use."""
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = queue.Queue()
            for index in range(max(1, RC.MAX_CONCURRENCY)):
                _slots.put(index)
            log.debug("render slot pool created with %d slot(s)", RC.MAX_CONCURRENCY)
        return _slots


def _profile_dir(slot: int) -> Path:
    """This slot's private LibreOffice profile directory."""
    return Path(tempfile.gettempdir()) / RC.PROFILE_DIRNAME / f"slot_{slot}_{os.getpid()}"


def _acquire_slot() -> int:
    """Wait for a free slot, noticing a stopped run while waiting."""
    pool = _slot_pool()
    while True:
        run_control.checkpoint()
        try:
            return pool.get(timeout=RC.SLOT_POLL_SECONDS)
        except queue.Empty:
            log.debug("all %d render slot(s) busy; waiting", RC.MAX_CONCURRENCY)


def _env(profile: Path) -> dict:
    """A minimal environment for soffice: no service secrets are inherited.

    On Windows soffice crashes (0xC0000409) without the standard system variables, so
    those - and only those - are passed through; its per-user folders point at the
    slot's own profile.
    """
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(profile),
           "TMP": str(profile), "TEMP": str(profile), "TMPDIR": str(profile),
           "USERPROFILE": str(profile), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    if sys.platform.startswith("win"):
        for name in RC.WINDOWS_SYSTEM_VARS:
            env[name] = os.environ.get(name, "")
        env["APPDATA"] = env["LOCALAPPDATA"] = str(profile)
    return {k: v for k, v in env.items() if v}


def _command(binary: str, source: Path, out_dir: Path, profile: Path) -> list[str]:
    """The soffice argv for one PNG export at the configured size."""
    flt = ('png:impress_png_Export:{"PixelWidth":{"type":"long","value":"%d"},'
           '"PixelHeight":{"type":"long","value":"%d"}}' % (RC.WIDTH_PX, RC.HEIGHT_PX))
    return [binary, f"-env:UserInstallation={profile.as_uri()}", "--headless",
            "--norestore", "--nologo", "--nodefault", "--nolockcheck",
            "--convert-to", flt, "--outdir", str(out_dir), str(source)]


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill soffice and every child it started (soffice.bin outlives its wrapper)."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("could not kill the soffice process tree %d (%s); killing the "
                    "wrapper only", process.pid, exc)
        try:
            process.kill()
        except Exception:                                     # noqa: BLE001
            pass


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk, or (0, 0) when it is not a PNG."""
    with open(path, "rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != _PNG_MAGIC:
        return 0, 0
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def render_png(pptx_path: Path, out_dir: Path) -> Path:
    """Render a single-slide .pptx to a PNG.

    The file is copied into its own round directory first, so LibreOffice's lock and
    output files never land in the slide's sandbox.

    Args:
        pptx_path: The slide to render.
        out_dir: Where round directories and PNGs go (e.g. ``slide_3/review``).

    Returns:
        The PNG's path.

    Raises:
        SlideRenderError: soffice is missing, failed, timed out or wrote no valid PNG.
    """
    global _renders
    binary = find_soffice()
    if binary is None:
        raise SlideRenderError("LibreOffice is not installed")
    with _slots_lock:
        _renders += 1
        round_no = _renders
    round_dir = Path(out_dir) / f"round_{round_no}"
    round_dir.mkdir(parents=True, exist_ok=True)
    source = round_dir / Path(pptx_path).name
    shutil.copy2(pptx_path, source)

    slot = _acquire_slot()
    profile = _profile_dir(slot)
    profile.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = None
    try:
        argv = _command(binary, source, round_dir, profile)
        log.info("rendering %s on slot %d (%dx%d px, timeout %ss)", source.name, slot,
                 RC.WIDTH_PX, RC.HEIGHT_PX, RC.TIMEOUT_SECONDS)
        kwargs: dict = {"cwd": str(round_dir), "env": _env(profile),
                        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(argv, **kwargs)
        stdout = stderr = b""
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                # A stop raises out of here; the finally kills the tree.
                run_control.checkpoint()
                if time.monotonic() - started > RC.TIMEOUT_SECONDS:
                    _kill_tree(process)
                    shutil.rmtree(profile, ignore_errors=True)
                    _record(False)
                    raise SlideRenderError(
                        f"LibreOffice did not finish within {RC.TIMEOUT_SECONDS}s")
        process = None
        tail = (stderr or stdout or b"").decode("utf-8", "replace")[-RC.MAX_STDERR_CHARS:]
        png = round_dir / f"{source.stem}.png"
        if not png.is_file():
            _record(False)
            raise SlideRenderError(f"LibreOffice wrote no PNG: {tail.strip() or '-'}")
        size = png.stat().st_size
        width, height = _png_size(png)
        if size < RC.MIN_PNG_BYTES or not width:
            _record(False)
            raise SlideRenderError(f"LibreOffice wrote an unusable PNG ({size} bytes, "
                                   f"{width}x{height})")
        _record(True)
        log.info("rendered %s -> %s (%dx%d px, %d bytes) in %.1fs", source.name,
                 png.name, width, height, size, time.monotonic() - started)
        return png
    finally:
        if process is not None:
            _kill_tree(process)
            shutil.rmtree(profile, ignore_errors=True)
        _slot_pool().put(slot)


def self_test() -> dict:
    """Build one slide with DeckBuilder and render it, for `check_config.py`.

    Returns:
        {"ok": bool, "detail": str, "seconds": float}
    """
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="render_selftest_"))
    try:
        from app.genai.ppt.deck_builder import DeckBuilder
        from app.genai.ppt.template import catalog as catalog_mod, registry

        catalog = catalog_mod.load()
        deck = DeckBuilder(registry.template_path(), catalog.to_dict(), frame=None)
        layout = catalog.content_layouts[0] if catalog.content_layouts else catalog.layouts[0]
        slide = deck.add_slide(layout.key)
        try:
            slide.set_title("Renderer self-test")
        except Exception:                                     # noqa: BLE001
            pass
        pptx = deck.save(work / "selftest.pptx")
        png = render_png(Path(pptx), work / "review")
        width, height = _png_size(png)
        return {"ok": True, "detail": f"{width}x{height} px",
                "seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:                                  # noqa: BLE001
        log.warning("renderer self-test failed: %s", exc, exc_info=True)
        return {"ok": False, "detail": str(exc)[:300],
                "seconds": round(time.monotonic() - started, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def missing_fonts(families: tuple[str, ...] = ("Barlow", "Carlito", "Liberation Sans")
                  ) -> list[str]:
    """Font families fontconfig cannot resolve exactly (best effort, POSIX only).

    Args:
        families: The families the renders depend on.

    Returns:
        The missing ones; [] when every one resolves or `fc-match` is unavailable.
    """
    fc_match = shutil.which("fc-match")
    if fc_match is None:
        return []
    missing = []
    for family in families:
        try:
            out = subprocess.run([fc_match, family], capture_output=True, text=True,
                                 timeout=10).stdout
        except Exception:                                     # noqa: BLE001
            continue
        if family.split()[0].lower() not in out.lower():
            missing.append(family)
    return missing
