"""Deck polish inside the app: one call after the deck is built, before it is published.

    DECK_POLISH=off      nothing changes (the default)
    DECK_POLISH=on       polish at the creativity level the user picked (Low/Medium/High)
    DECK_POLISH=low|medium|high   always this level (handy for testing)

It works on the finished file, so it runs after either engine (classic or skill_deck).
A failure never costs the deck: the built deck is published as it was.
"""
from __future__ import annotations

from pathlib import Path

from app.config.constants import CreativityLevels, DeckPolishConstants as C
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _setting() -> str:
    return (getattr(get_settings(), "deck_polish", "") or C.OFF).strip().lower()


def enabled() -> bool:
    """True when this deployment polishes decks."""
    value = _setting()
    if value in (C.OFF, "false", "0", "no", ""):
        return False
    if value != C.FOLLOW and value not in C.SETTING_LEVELS:
        log.warning("unknown DECK_POLISH %r; polish stays off", value)
        return False
    return True


def level_for(creativity: str) -> str:
    """The polish level: fixed by the setting, or the deck's creativity level."""
    value = _setting()
    if value in C.SETTING_LEVELS:
        return C.SETTING_LEVELS[value]
    creativity = (creativity or "").upper()
    creativity = CreativityLevels.ALIASES.get(creativity, creativity)
    return creativity if creativity in C.LEVEL_SKILLS else CreativityLevels.DEFAULT


def polish_deck(deck: Path, workdir: Path, level: str, style_guidance: str = "",
                writer=None, on_step=None) -> tuple[Path, list[str]]:
    """Polish a finished deck.

    Args:
        deck: The deck the engine built.
        workdir: The turn's work directory; versions are kept in a sub-folder.
        level: LOW, MED or HIGH.
        style_guidance: The session's active style skill ("" when none).
        writer: Operations writer for MEDIUM/HIGH; defaults to the gateway model.
        on_step: Callback for progress lines shown in the chat.

    Returns:
        (deck to publish, notes for the user: the summary and anything a person must fix).
    """
    from app.genai.deck_polish.graph import build_graph
    from app.genai.deck_polish.llm import GatewayOpsWriter

    if level not in C.LEVEL_SKILLS:
        raise ValueError(f"unknown polish level {level!r}")
    if writer is None and C.OPS_BY_LEVEL[level]:
        writer = GatewayOpsWriter()
    work = Path(workdir) / C.WORK_DIRNAME
    state = build_graph(writer, on_step).invoke({
        "source": str(deck), "level": level, "work_dir": str(work),
        "output_path": str(work / C.OUTPUT_FILENAME),
        "style_guidance": style_guidance})
    return Path(state["final_deck"]), [state["summary"], *state.get("reports", [])]
