"""The model call: deck + issues in, a JSON list of edit operations out.

Calls go through the ROK AI Gateway with the app's own client, like every other agent.
Any object with ``write_ops(system, user)`` can stand in for it (the tests use a fake).
"""
from __future__ import annotations

import json
import re
from typing import Protocol

from app.config.constants import DeckPolishConstants as C
from app.genai.skill_deck.llm import reply_text
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class OpsWriter(Protocol):
    """Anything that turns the polish prompt into operations."""

    def write_ops(self, system: str, user: str) -> list[dict]:
        """Return the operations."""


def parse_ops(text: str) -> list[dict]:
    """The operations in a reply: a fenced or bare JSON array (or {"operations": [...]}).

    Returns:
        The operations; an empty list when the reply holds none.
    """
    candidates = _FENCE.findall(text) + [text]
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate.strip())
        except ValueError:
            continue
        if isinstance(value, dict):
            value = value.get("operations")
        if isinstance(value, list):
            return [op for op in value if isinstance(op, dict)]
    log.warning("no operations in a %d-char reply; it began: %s", len(text), text[:200])
    return []


class GatewayOpsWriter:
    """Asks a Claude model over the ROK AI Gateway for the operations."""

    def __init__(self, model: str = C.MODEL):
        self.model = model

    def write_ops(self, system: str, user: str) -> list[dict]:
        from app.utilities.apim_llm_utils import get_apim_llm_client
        log.info("asking %s for polish operations (%d-char prompt)", self.model,
                 len(system) + len(user))
        response = get_apim_llm_client().foundry_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=self.model, max_tokens=C.MAX_TOKENS)
        return parse_ops(reply_text(response))
