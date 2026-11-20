"""The model call: send the skills, get back a DeckBuilder program for the whole deck.

Calls go through the ROK AI Gateway (APIM, Foundry route) with the app's own client,
so the same ``dev.env`` credentials and usage tracking apply. The program runs on this
machine against the official template (``deck_builder.py`` is staged beside it), so no
code-execution tool or container skill is sent: the model answers with the program as
text. Any object with a ``write_program(system, messages)`` method can stand in for it
(the tests use a fake).
"""
from __future__ import annotations

import ast
import re
from typing import Protocol

from app.utilities.logger_util import get_logger
from app.config.constants import SkillDeckConstants as C

log = get_logger(__name__)

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class ProgramWriter(Protocol):
    """Anything that turns a prompt into a python-pptx program."""

    def write_program(self, system: str, messages: list[dict]) -> str:
        """Return the source of the program."""


class NoProgramError(RuntimeError):
    """The model answered without a usable program."""


def reply_text(response: dict) -> str:
    """Assistant text from an OpenAI-style or Anthropic-style response.

    Args:
        response: Raw gateway JSON.

    Returns:
        The concatenated text (empty when there is none).
    """
    if "choices" in response:
        content = (response["choices"][0].get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        blocks = content or []
    else:
        blocks = response.get("content") or []
    return "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type", "text") == "text")


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False


def extract_program(response: dict) -> str:
    """Pull the generated program out of a gateway response.

    Looks first for the code-execution tool block (``server_tool_use`` with a
    ``file_text`` input), then for the longest fenced block that parses as Python,
    then for a bare reply that parses.

    Args:
        response: Raw gateway JSON.

    Returns:
        Python source.

    Raises:
        NoProgramError: When nothing in the reply is a parseable program.
    """
    blocks = response.get("content")
    if not isinstance(blocks, list):
        message = (response.get("choices") or [{}])[0].get("message") or {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
    for block in blocks or []:
        if (isinstance(block, dict) and block.get("type") == C.CODE_BLOCK_TYPE
                and block.get("name") in C.CODE_BLOCK_NAMES):
            source = (block.get("input") or {}).get(C.CODE_BLOCK_TEXT_KEY)
            if source:
                log.debug("program read from a %s block", block.get("name"))
                return source
    text = reply_text(response)
    for candidate in sorted(_FENCE.findall(text), key=len, reverse=True):
        if _is_program(candidate) and _parses(candidate):
            return candidate.strip() + "\n"
    if _is_program(text) and _parses(text):
        return text.strip() + "\n"
    log.error("no program in a %d-char reply; it began: %s", len(text), text[:300])
    raise NoProgramError("the model returned no DeckBuilder program")


def _is_program(source: str) -> bool:
    return any(marker in source for marker in C.PROGRAM_MARKERS)


class GatewayProgramWriter:
    """Writes programs with a Claude model over the ROK AI Gateway.

    Args:
        model: Gateway model name.
    """

    def __init__(self, model: str = C.MODEL):
        self.model = model

    def write_program(self, system: str, messages: list[dict]) -> str:
        """Ask the model for the program.

        Args:
            system: System prompt carrying the skills.
            messages: User/assistant turns so far.

        Returns:
            Python source.
        """
        from app.utilities.apim_llm_utils import get_apim_llm_client
        log.info("asking %s for the deck program (%d turn(s))", self.model, len(messages))
        response = get_apim_llm_client().foundry_chat(
            [{"role": "system", "content": system}, *messages], model=self.model,
            max_tokens=C.MAX_TOKENS)
        return extract_program(response)
