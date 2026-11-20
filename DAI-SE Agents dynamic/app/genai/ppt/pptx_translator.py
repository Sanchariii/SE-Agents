"""Translate a finished .pptx in place, run by run.

The deck is no longer rendered from structured JSON, so translation can no longer happen
before rendering: the design lives in the generated file. This module therefore walks the
built deck, translates only the human-readable strings, and writes them back into the
*same* runs — which is what preserves every font, size, colour and position the designer
chose.

Fault tolerance is per slide: a slide whose translation call fails keeps its English text
rather than failing the language, because a partly-English deck is still a usable deck.
"""

from pathlib import Path
from pydantic import BaseModel

from app.config.constants import AgentNames, ModelConstants, TranslateConstants
from app.utilities import events_util as events
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import (ContextThreadPoolExecutor, get_logger)

log = get_logger(__name__)

TRANSLATE_PROMPT = """You translate the text of ONE presentation slide into {language}.

You are given a JSON object mapping numeric ids to strings taken from the slide, in
reading order. Translate every value into natural, professional {language} as used in
business training material.

Rules:
- Return the SAME ids. Never add, drop, merge or reorder an entry.
- Keep each translation about as short as the original — it must fit the same shape.
- Do NOT translate: product names, company names, standards, acronyms, code, URLs,
  file names, email addresses and numbers. Leave them exactly as they are.
- Preserve leading/trailing spaces, bullet characters and punctuation style.
- A value that is only digits, punctuation or a name is returned unchanged.

Respond ONLY with JSON: {{"texts": {{"0": "...", "1": "..."}}}}"""


class _Translations(BaseModel):
    """The model's reply: original index -> translated string."""
    texts: dict[str, str] = {}


def _iter_shapes(shapes):
    """Yield every shape on a slide, descending into groups.

    Args:
        shapes: A python-pptx shape collection.

    Yields:
        Each shape, with grouped shapes flattened out.
    """
    for shape in shapes:
        # 6 == MSO_SHAPE_TYPE.GROUP; compared numerically to avoid an enum import here.
        if shape.shape_type == 6:
            yield from _iter_shapes(shape.shapes)
        else:
            yield shape


def _collect_runs(slide) -> list:
    """Collect every translatable text run on one slide, in reading order.

    Args:
        slide: A python-pptx slide.

    Returns:
        The runs of shape text, table cells and speaker notes, as python-pptx run objects.
    """
    runs = []
    for shape in _iter_shapes(slide.shapes):
        if shape.has_text_frame:
            runs += [run for paragraph in shape.text_frame.paragraphs
                     for run in paragraph.runs]
        if getattr(shape, "has_table", False):
            runs += [run for row in shape.table.rows for cell in row.cells
                     for paragraph in cell.text_frame.paragraphs
                     for run in paragraph.runs]
    if slide.has_notes_slide:
        runs += [run for paragraph in slide.notes_slide.notes_text_frame.paragraphs
                 for run in paragraph.runs]
    return runs


def _translate_slide(session_id: str, slide, index: int, language: str) -> None:
    """Translate one slide's runs in place. Best-effort: never raises.

    Args:
        session_id: Owning session, so progress can be streamed to the client.
        slide: The python-pptx slide to translate.
        index: 1-based slide number, for events and logs.
        language: Target language name.

    Returns:
        None. The slide's runs are rewritten in place when translation succeeds.
    """
    runs = [run for run in _collect_runs(slide) if run.text and run.text.strip()]
    if not runs:
        return

    # One call per slide keeps each payload small; a huge slide is clipped rather than
    # risking a truncated reply that would lose text.
    runs = runs[:TranslateConstants.MAX_TEXTS_PER_SLIDE]
    payload = {str(i): run.text for i, run in enumerate(runs)}

    try:
        translated = get_apim_llm_client().chat_json(
            TRANSLATE_PROMPT.format(language=language),
            [{"role": "user", "content": _Translations(texts=payload).model_dump_json()}],
            _Translations, max_tokens=TranslateConstants.MAX_TOKENS,
            model=ModelConstants.TRANSLATOR,
            tracking={"x-rok-agent-name": AgentNames.TRANSLATOR})
    except Exception as exc:                                 # noqa: BLE001
        log.warning("slide %d translation to %s failed, keeping English: %s",
                    index, language, exc, extra={"session_id": session_id})
        events.publish(session_id, "agent_step", agent="translate",
                       message=f"Slide {index} translation to {language} failed; "
                               f"kept English text")
        return

    for position, run in enumerate(runs):
        text = translated.texts.get(str(position))
        if text:
            run.text = text

    events.publish(session_id, "agent_step", agent="translate",
                   message=f"Translated slide {index} into {language}")


def translate_pptx(session_id: str, src: Path, dst: Path, language: str) -> Path:
    """Write a translated copy of a built deck, preserving its design.

    Args:
        session_id: Owning session, so per-slide progress can be streamed.
        src: The finished English deck.
        dst: Where the translated deck is written.
        language: Target language name (e.g. "Spanish").

    Returns:
        The path of the translated deck. On a total failure the English deck is copied
        through unchanged, so the language is still published.
    """
    # Imported lazily: python-pptx is heavy and only needed when a deck is published.
    from pptx import Presentation

    log.info("translating deck %s into %s", src.name, language,
             extra={"session_id": session_id})
    try:
        presentation = Presentation(str(src))
    except Exception as exc:                                 # noqa: BLE001
        log.error("deck %s could not be opened for translation: %s", src, exc,
                  exc_info=True, extra={"session_id": session_id})
        dst.write_bytes(src.read_bytes())
        return dst

    slides = list(presentation.slides)
    events.publish(session_id, "agent_step", agent="translate",
                   message=f"Translating {len(slides)} slide(s) into {language}…")

    workers = min(TranslateConstants.MAX_WORKERS, len(slides) or 1)
    if workers > 1:
        # Slides are independent objects in one package, so they translate concurrently.
        with ContextThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda item: _translate_slide(session_id, item[1], item[0],
                                                        language),
                          list(enumerate(slides, start=1))))
    else:
        for index, slide in enumerate(slides, start=1):
            _translate_slide(session_id, slide, index, language)

    _translate_core_properties(presentation, language)
    presentation.save(str(dst))
    return dst


def _translate_core_properties(presentation, language: str) -> None:
    """Tag the translated copy with its language, leaving the original untouched.

    Args:
        presentation: The deck about to be saved.
        language: Target language name.

    Returns:
        None.
    """
    try:
        presentation.core_properties.language = language
    except Exception as exc:                                 # noqa: BLE001
        log.warning("could not set deck language metadata: %s", exc)


__all__ = ["translate_pptx", "copy_deck"]


def copy_deck(src: Path, dst: Path) -> Path:
    """Copy a deck file without touching its contents.

    Args:
        src: Source deck.
        dst: Destination path.

    Returns:
        The destination path.
    """
    dst.write_bytes(src.read_bytes())
    return dst
