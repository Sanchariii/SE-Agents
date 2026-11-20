"""Slide-copy helpers shared by the per-slide PPT agent, plus deck translation.

`repair_slide` is the safety layer between a model's slide JSON and the renderer: it
forces a real layout, real slots, and copy that fits the placeholder budgets. Formatting
is never set here — fonts and colours are inherited from the brand .potx.
"""

import re

from app.config.constants import ModelConstants

from app.config.constants import ModelConstants, TranslateConstants
from app.core.models.deck_model import (
    ContentBlock, DeckDraft, DeckMetaOutput, SlideDraft, TakeawaysOutput,
)
from app.utilities import events_util as events
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import (ContextThreadPoolExecutor, get_logger)

IMAGE_PATH = re.compile(r"\S*\.(?:png|jpe?g|gif|bmp|webp)\b", re.I)
EMOJI = re.compile(
    "[\\U0001F000-\\U0001FAFF\\U00002600-\\U000027BF\\U0000231A-\\U0000231B"
    "\\U000023E9-\\U000023F3\\U000025AA-\\U000025AB\\U000025FB-\\U000025FE"
    "\\U00002934-\\U00002935\\U00002B05-\\U00002B07\\U00002B1B-\\U00002B1C]"
)
log = get_logger(__name__)


def _extract_image_refs(slide, offered: list[str]) -> list[str]:
    """Pull any keyframe path the model wrote as visible copy back out of the text."""
    known = set(offered)
    found: list[str] = []

    def harvest(value: str) -> str:
        remaining = value
        for match in IMAGE_PATH.findall(value) + [p for p in known if p in value]:
            if match not in found:
                found.append(match)
            remaining = remaining.replace(match, "")
        return re.sub(r"\s{2,}", " ", remaining).strip(" :-–—")

    for block in slide.blocks:
        if block.image_ref:
            if block.image_ref not in found:
                found.append(block.image_ref)
            continue
        block.text = harvest(block.text)
        block.bullets = [cleaned for cleaned in (harvest(b) for b in block.bullets) if cleaned]
    slide.blocks = [b for b in slide.blocks if b.image_ref or b.text.strip() or b.bullets]
    return [path for path in found if path in known] or found


def _strip_emoji(text: str) -> str:
    """Remove emoji characters from model-produced visible or speaker-note text."""
    return EMOJI.sub("", text)


def _strip_slide_emoji(slide: SlideDraft) -> None:
    """Remove emoji from every text field that can be rendered into a slide."""
    slide.title = _strip_emoji(slide.title)
    slide.notes = _strip_emoji(slide.notes)
    for block in slide.blocks:
        block.text = _strip_emoji(block.text)
        block.bullets = [_strip_emoji(bullet) for bullet in block.bullets]


_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _clip(text: str, limit: int) -> str:
    """Trim to at most `limit` chars on a sentence or word boundary — never mid-word,
    so a bullet always reads as a complete phrase instead of a cut-off fragment."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    window = text[:limit + 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(window)]
    if ends and ends[-1] >= limit * 0.6:
        return window[:ends[-1]].strip()
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window[:limit]).rstrip()


def _fit_bullets(bullets: list[str], slot) -> list[str]:
    """Keep whole bullets, clip an over-long one on a word boundary rather than mid-word,
    and stop adding bullets once the slot's lines are full — so nothing lands truncated."""
    per_line = max(20, slot.capacity // max(slot.lines, 1))
    lines_left = slot.lines
    fitted: list[str] = []
    for bullet in bullets:
        if lines_left <= 0:
            break
        text = _clip(bullet, per_line * min(lines_left, 2))   # a bullet wraps to <=2 lines
        if not text:
            continue
        fitted.append(text)
        lines_left -= max(1, -(-len(text) // per_line))       # ceil: lines this bullet uses
    return fitted


def _repair(
    draft: DeckDraft,
    catalog,
    keyframes_by_slide: dict[int, list[str]],
) -> DeckDraft:
    """Make model slide choices compatible with the selected template.

    Args:
        draft: Generated deck draft to normalize.
        catalog: Template layout catalog.
        keyframes_by_slide: Valid extracted image paths grouped by slide number.

    Returns:
        The repaired deck with valid layouts, slots, fitted text, and images.
    """
    for slide in draft.slides:
        _strip_slide_emoji(slide)
        spec = catalog.resolve(slide.layout)

        # The document extractor has already verified these source image paths.
        offered_images = list(dict.fromkeys(
            image_path.strip()
            for image_path in keyframes_by_slide.get(slide.slide_number, [])
            if image_path and image_path.strip()
        ))

        # Preserve explicit valid choices, but use an extracted image when the
        # agent omitted image_ref. This prevents a retrieved DI figure from
        # disappearing before the PPT renderer runs.
        requested_images = _extract_image_refs(slide, offered_images)
        images = [
            image_path
            for image_path in requested_images
            if image_path in offered_images
        ]
        # if not images:
        #     images = offered_images

        # Move to an image-capable layout when source imagery exists.
        if images and not spec.picture_slots:
            image_layout = catalog.best_with_picture(
                len([block for block in slide.blocks if not block.image_ref])
            )
            if image_layout is not None:
                log.info(
                    "slide %d: extracted image selects layout %r instead of %r",
                    slide.slide_number,
                    image_layout.name,
                    spec.name,
                )
                spec = image_layout

        if images and not spec.picture_slots:
            log.warning(
                "slide %d has %d extracted image(s), but layout %r has no picture slot",
                slide.slide_number,
                len(images),
                spec.name,
            )
            images = []

        slide.layout = spec.name
        text_slots = [placeholder.slot for placeholder in spec.text_slots]
        picture_slots = [placeholder.slot for placeholder in spec.picture_slots]
        used: set[str] = set()
        kept: list[ContentBlock] = []

        # Add source images before text so picture slots cannot be consumed by copy.
        for image_path, picture_slot in zip(images, picture_slots):
            kept.append(ContentBlock(slot=picture_slot, image_ref=image_path))
            used.add(picture_slot)

        for block in slide.blocks:
            # Image blocks have been normalized above from trusted extracted paths.
            if block.image_ref:
                continue

            target = spec.slot(block.slot)
            if target is not None and target.kind in ("title", "picture"):
                target = None

            if target is None or target.slot in used:
                free_text_slots = [
                    slot_name for slot_name in text_slots if slot_name not in used
                ]
                if not free_text_slots:
                    continue
                target = spec.slot(free_text_slots[0])

            if not (block.text.strip() or any(item.strip() for item in block.bullets)):
                continue

            if block.bullets:
                block.bullets = _fit_bullets(
                    [item for item in block.bullets if item.strip()],
                    target,
                )

            block.text = _clip(block.text, target.capacity)
            block.slot = target.slot
            used.add(target.slot)
            kept.append(block)

        slide.blocks = kept
        if not slide.title.strip():
            slide.title = f"Slide {slide.slide_number}"

    return draft


def repair_slide(slide: SlideDraft, catalog, keyframes: list[str] | None = None) -> SlideDraft:
    """Public single-slide entry to the same layout/slot repair the deck writer uses.

    Args:
        slide: A candidate slide, possibly naming a layout or slots that do not exist.
        catalog: The Catalog for the deck's classification.
        keyframes: Storage paths this slide is allowed to place in a picture slot.

    Returns:
        The repaired slide: a real layout, real slots, copy clipped to slot budgets,
        and any image path pulled out of visible copy into an image_ref.
    """
    draft = DeckDraft(title=slide.title or "slide", slides=[slide])
    return _repair(draft, catalog, {slide.slide_number: list(keyframes or [])}).slides[0]


TAKEAWAYS_PROMPT = """You write the closing takeaways of a training deck. Given the deck
title and its slide titles with their main points, write 2-4 short takeaways the audience
should leave with. Ground them in the slides given — never introduce a new fact.

Respond ONLY with JSON: {"takeaways": ["...", "..."]}"""


def write_takeaways(draft: DeckDraft, audience: str = "") -> list[str]:
    """Write closing takeaways from finished slides. Best-effort: never raises.

    Args:
        draft: The assembled deck, whose slides are already written.
        audience: Who the deck is for.

    Returns:
        2-4 takeaways, or an empty list when the model is unavailable.
    """
    outline = "\n".join(
        f"Slide {s.slide_number}: {s.title}\n"
        + "\n".join(f"  - {item}" for b in s.blocks
                    for item in (b.bullets or ([b.text] if b.text else [])))[:400]
        for s in draft.slides)
    try:
        output = get_apim_llm_client().chat_json(
            TAKEAWAYS_PROMPT,
            [{"role": "user", "content": f"DECK: {draft.title}\nAudience: {audience}\n\n"
                                         f"{outline}"}],
            TakeawaysOutput, max_tokens=500, model=ModelConstants.WRITER)
        return [t for t in output.takeaways if t.strip()][:4]
    except Exception as exc:                             # noqa: BLE001
        log.warning("takeaways unavailable: %s", exc)
        return []


SLIDE_TRANSLATE_PROMPT = """Translate every human-readable text field of this single
training-deck slide into {language}: title, notes, and each block's "text" and "bullets".
Do NOT change the JSON shape and do NOT translate or alter slide_number, layout, slot, or
image_ref. Keep each translation roughly the same length as the original so it still fits
its placeholder. Respond ONLY with the translated JSON in the identical schema."""

DECK_META_TRANSLATE_PROMPT = """Translate these training-deck fields into {language}:
title, subtitle, and each takeaway. Keep each translation roughly the same length as the
original. Respond ONLY with JSON in the identical schema: {{"title": "...",
"subtitle": "...", "takeaways": ["...", "..."]}}"""



def _translate_slide(session_id: str, slide: SlideDraft, language: str) -> SlideDraft:
    """Translate one slide in isolation, keeping the prompt payload light.

    Args:
        session_id: Owning session, so progress can be streamed to the client.
        slide: The slide to translate.
        language: Target language name (e.g. "Spanish").

    Returns:
        The translated slide, or the original slide if translation fails
        (a bad translation must never drop a slide from the deck).
    """
    try:
        translated = get_apim_llm_client().chat_json(
            SLIDE_TRANSLATE_PROMPT.format(language=language),
            [{"role": "user", "content": slide.model_dump_json()}],
            SlideDraft, max_tokens=2000, model=ModelConstants.TRANSLATOR)
        events.publish(session_id, "agent_step", agent="translate",
                       message=f"Translated slide {slide.slide_number} into {language}")
        return translated
    except Exception as exc:                             # noqa: BLE001
        log.warning("slide %d translation to %s failed, keeping English: %s",
                    slide.slide_number, language, exc)
        events.publish(session_id, "agent_step", agent="translate",
                       message=f"Slide {slide.slide_number} translation to {language} "
                               f"failed; kept English text")
        return slide


def _translate_meta(draft: DeckDraft, language: str) -> DeckMetaOutput:
    """Translate the deck-level title/subtitle/takeaways as one small call."""
    try:
        return get_apim_llm_client().chat_json(
            DECK_META_TRANSLATE_PROMPT.format(language=language),
            [{"role": "user", "content": DeckMetaOutput(
                title=draft.title, subtitle=draft.subtitle,
                takeaways=draft.takeaways).model_dump_json()}],
            DeckMetaOutput, max_tokens=1000, model=ModelConstants.TRANSLATOR)
    except Exception as exc:                             # noqa: BLE001
        log.warning("deck meta translation to %s failed, keeping English: %s",
                    language, exc)
        return DeckMetaOutput(title=draft.title, subtitle=draft.subtitle,
                              takeaways=draft.takeaways)


def translate_deck(session_id: str, draft: DeckDraft, language: str) -> DeckDraft:
    """Translate a deck slide-by-slide plus its title/subtitle/takeaways.

    Args:
        session_id: Owning session, so per-slide progress can be streamed to the client.
        draft: The finished, English deck draft.
        language: Target language name.

    Returns:
        A new DeckDraft with every human-readable field translated; slide_number,
        layout, slot, and image_ref are left untouched on every slide.
    """
    log.info("translating deck '%s' into %s (%d slides)",
              draft.title, language, len(draft.slides))
    events.publish(session_id, "agent_step", agent="translate",
                   message=f"Translating {len(draft.slides)} slide(s) into {language}…")

    workers = min(TranslateConstants.MAX_WORKERS, len(draft.slides) or 1)
    if workers > 1:
        # slides are independent, so translate them concurrently
        with ContextThreadPoolExecutor(max_workers=workers) as pool:
            translated_slides = list(pool.map(
                lambda slide: _translate_slide(session_id, slide, language), draft.slides))
    else:
        translated_slides = [_translate_slide(session_id, s, language)
                             for s in draft.slides]

    meta = _translate_meta(draft, language)

    return draft.model_copy(update={
        "title": meta.title,
        "subtitle": meta.subtitle,
        "takeaways": meta.takeaways,
        "slides": translated_slides,
    })