"""Tools the PPT Agent uses to gather and write the CONTENT of ONE slide.

Every tool is bound to a single `SlideWorkspace`, so the agent's context stays one
slide wide no matter how large the deck is. The workspace is also where the round
budgets live: a tool refuses politely once its budget is spent, which is what stops
the loop without relying on the model to stop itself.

These tools stop at grounded content. Layout, colour and composition are decided
later by `PptCreatorAgent`, which writes a python-pptx program for the slide — so
there is deliberately no layout tool and no slot budget to fit copy into here.

Retrieval is independent per slide — no chunk is withheld because a neighbouring
slide already used it (see docs/slide-agent-pipeline.md section 3.5).
"""

from dataclasses import dataclass, field

from langchain_core.tools import tool
from pydantic import ValidationError

from app.config.constants import QuizConstants, SlideAgentConstants, SlideTypes
from app.core.models.deck_model import (
    ContentBlock, DeckContext, FaqItem, ImageCandidate, QuizContent, SlideDraft, SlidePlan,
    SlideTask,
)
from app.core.models.document_model import RetrievedChunk
from app.genai.retrieval.retriever import retrieve
from app.genai.guardrails import GuardrailCheckError, GuardrailViolation
from app.utilities import events_util as events
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_IMAGE_CHUNK_KINDS = ("frame_caption", "image_description")

@dataclass
class SlideWorkspace:
    """Mutable per-slide state shared by the content tools of one agent run."""

    session_id: str
    task: SlideTask
    plan: SlidePlan
    context: DeckContext
    chunks: list[RetrievedChunk] = field(default_factory=list)
    candidate: SlideDraft | None = None
    coverage: str = "missing"
    research_rounds: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def seen(self) -> set[str]:
        return {c.chunk_id for c in self.chunks}

    # def keyframes(self) -> list[str]:
    #     """Storage paths this slide may legitimately place in a picture slot."""
    #     paths = [c.meta.get("frame_path") for c in self.chunks
    #              if c.kind in _IMAGE_CHUNK_KINDS and (c.meta or {}).get("frame_path")]
    #     return list(dict.fromkeys(p for p in paths if p))[:3]

    def image_assets(self) -> list[tuple[str, str]]:
        """Return extracted images the writer may choose this slide's candidates from.

        Returns:
            Unique pairs of storage path and source description, best search score first,
            at most `MAX_IMAGE_ASSETS_OFFERED`. Empty for a slide that may carry no image.
        """
        if not self.plan.images_allowed:
            return []
        assets: dict[str, str] = {}
        image_chunks = sorted((c for c in self.chunks if c.kind in _IMAGE_CHUNK_KINDS),
                              key=lambda c: c.score or 0.0, reverse=True)
        for chunk in image_chunks:
            image_path = str((chunk.meta or {}).get("frame_path") or "").strip()
            if not image_path:
                continue
            description = " ".join(chunk.text.split())[
                :SlideAgentConstants.IMAGE_DESCRIPTION_CHARS]
            assets.setdefault(image_path, description or chunk.section)

        return list(assets.items())[:SlideAgentConstants.MAX_IMAGE_ASSETS_OFFERED]

    def keyframes(self) -> list[str]:
        """Return valid storage paths for image-capable slide slots.

        Returns:
            The offered image storage paths.
        """
        return [image_path for image_path, _ in self.image_assets()]

    def image_asset_block(self) -> str:
        """Format image assets so the PPT agent can choose this slide's candidates.

        Returns:
            Prompt text containing exact image_ref values, or "" when the slide may
            carry no image, so a quiz/FAQ/role slide's prompt is unchanged.
        """
        if not self.plan.images_allowed:
            return ""
        assets = self.image_assets()
        if not assets:
            return "Available image assets: none."

        limit = SlideAgentConstants.MAX_IMAGE_CANDIDATES
        lines = [
            f"Available image assets. Pass up to {limit} whose description fits this "
            "slide's content as write_content's image_candidates, most relevant first, or "
            "none when none fits. They are optional assets for the designer, who decides "
            "whether to use them. Never put an image_ref in slide text."
        ]
        if self.plan.images_requested:
            lines.append("The user asked for images on this slide: include the best "
                         "matches unless they are clearly unrelated.")
        lines.extend(
            f"  - image_ref={image_path!r}; description={description!r}"
            for image_path, description in assets
        )
        return "\n".join(lines)

    def prefetch_images(self) -> int:
        """Retrieve image chunks for this slide without the model, before a rewrite.

        A rewrite withholds `research_slide`, so a follow-up such as "add an image to
        this slide" would otherwise never see an image. Runs only for a slide that may
        carry images and holds no image chunk yet.

        Returns:
            How many new image chunks were added.
        """
        if not self.plan.images_allowed:
            return 0
        if any(c.kind in _IMAGE_CHUNK_KINDS for c in self.chunks):
            log.info("slide %d: image chunks already gathered; no prefetch",
                     self.plan.slide_number, extra={"session_id": self.session_id})
            return 0
        queries = [q for q in [self.plan.title, *self.plan.content_needs] if q and q.strip()]
        added = 0
        for query in queries:
            try:
                for chunk in retrieve(self.session_id, query.strip(),
                                      top_k=SlideAgentConstants.TOP_K_PER_QUERY,
                                      kinds=list(_IMAGE_CHUNK_KINDS)):
                    if chunk.chunk_id not in self.seen:
                        self.chunks.append(chunk)
                        added += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("image prefetch failed for %r on slide %d: %s", query,
                            self.plan.slide_number, exc,
                            extra={"session_id": self.session_id})
        log.info("slide %d: image prefetch ran %d query(ies), added %d image chunk(s)",
                 self.plan.slide_number, len(queries), added,
                 extra={"session_id": self.session_id})
        return added

    def excerpt_block(self, limit: int | None = None) -> str:
        """The slide's excerpts, formatted for a prompt."""
        limit = limit or SlideAgentConstants.MAX_EXCERPTS_PER_SLIDE
        rows = [f"  [{i}] ({c.filename} / {c.section}) "
                f"{c.text[:SlideAgentConstants.MAX_EXCERPT_CHARS]}"
                for i, c in enumerate(self.chunks[:limit], start=1)]
        return "\n".join(rows) or "  (nothing retrieved for this slide)"

    def step(self, message: str, **data) -> None:
        events.publish(self.session_id, "agent_step", agent="slide", message=message,
                       slide_number=self.plan.slide_number,
                       total=self.context.total, **data)


def _as_block(item) -> ContentBlock:
    """Accept a dict or a pydantic object from the tool call and normalise it."""
    if isinstance(item, ContentBlock):
        return item
    if hasattr(item, "model_dump"):
        item = item.model_dump()
    return ContentBlock(slot=str(item.get("slot") or "body1"),
                        text=str(item.get("text") or ""),
                        bullets=[str(b) for b in (item.get("bullets") or []) if str(b).strip()],
                        image_ref=str(item.get("image_ref") or ""))


def _unique_images(blocks: list[ContentBlock], slide_number: int,
                   session_id: str = "") -> list[ContentBlock]:
    """Each image at most once on a slide, and at most `MAX_IMAGES_PER_SLIDE` of them.

    Args:
        blocks: The blocks the writer sent.
        slide_number: For the log line.
        session_id: For the log line.

    Returns:
        The blocks with repeated or surplus image refs cleared; a block left with
        neither text nor an image is dropped.
    """
    seen: set[str] = set()
    out: list[ContentBlock] = []
    for block in blocks:
        ref = (block.image_ref or "").strip()
        if ref and (ref in seen or len(seen) >= SlideAgentConstants.MAX_IMAGES_PER_SLIDE):
            log.info("slide %d: dropped image %s (%s)", slide_number, ref,
                     "repeated on the slide" if ref in seen else
                     f"over the {SlideAgentConstants.MAX_IMAGES_PER_SLIDE}-image limit",
                     extra={"session_id": session_id})
            block = block.model_copy(update={"image_ref": ""})
            if not block.text.strip() and not block.bullets:
                continue
        elif ref:
            seen.add(ref)
        out.append(block)
    return out


def _image_candidates_of(ws: SlideWorkspace, chosen: list,
                         blocks: list[ContentBlock]) -> list[ImageCandidate]:
    """Validate the writer's image candidates for this slide.

    A stray block `image_ref` (the older way of attaching an image) counts as a choice
    too, after the explicit ones.

    Args:
        ws: The slide's workspace, for the offered assets and the plan.
        chosen: The refs the writer passed, most relevant first.
        blocks: The written blocks, which may still carry an image_ref.

    Returns:
        Up to `MAX_IMAGE_CANDIDATES` offered images with their descriptions; empty on a
        slide that may carry no image.
    """
    refs = [str(r or "").strip() for r in chosen]
    refs += [(b.image_ref or "").strip() for b in blocks]
    refs = list(dict.fromkeys(r for r in refs if r))
    if not refs:
        return []
    if not ws.plan.images_allowed:
        log.info("slide %d: dropped %d image candidate(s); images are not used on a "
                 "%s/%s slide unless the user asks", ws.plan.slide_number, len(refs),
                 ws.plan.kind, ws.plan.slide_type, extra={"session_id": ws.session_id})
        return []
    offered = dict(ws.image_assets())
    unknown = [r for r in refs if r not in offered]
    if unknown:
        log.info("slide %d: dropped %d image candidate(s) that were never offered",
                 ws.plan.slide_number, len(unknown), extra={"session_id": ws.session_id})
    kept = [r for r in refs if r in offered][:SlideAgentConstants.MAX_IMAGE_CANDIDATES]
    log.info("slide %d: writer chose %d image candidate(s) of %d offered%s",
             ws.plan.slide_number, len(kept), len(offered),
             " (user asked for images)" if ws.plan.images_requested else "",
             extra={"session_id": ws.session_id})
    return [ImageCandidate(ref=r, description=offered[r]) for r in kept]


def _without_image_refs(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Clear block image refs; images travel as candidates now.

    Args:
        blocks: The written blocks.

    Returns:
        The blocks with image_ref cleared; a block left with no copy is dropped.
    """
    out: list[ContentBlock] = []
    for block in blocks:
        if (block.image_ref or "").strip():
            block = block.model_copy(update={"image_ref": ""})
            if not block.text.strip() and not block.bullets:
                continue
        out.append(block)
    return out


def build_slide_tools(ws: SlideWorkspace) -> list:
    """The two content tools, closed over one slide's workspace."""

    @tool
    def research_slide(queries: list[str], top_k: int = SlideAgentConstants.TOP_K_PER_QUERY) -> str:
        """Search this session's uploaded source documents for material to write THIS
        slide from. Pass 2-4 short, concrete queries drawn from the slide's content
        needs and key points. Call this before writing. If the excerpts come back thin,
        you may call it once more with different wording."""
        if ws.research_rounds >= SlideAgentConstants.MAX_RESEARCH_ROUNDS:
            return ("Research budget spent for this slide. Write the best grounded slide "
                    "the excerpts you already have will support.")
        ws.research_rounds += 1
        ws.step(f"Slide {ws.plan.slide_number}/{ws.context.total}: researching",
                phase="research", round=ws.research_rounds)

        added = 0
        # Quiz, FAQ and role slides carry no image unless the user asked for one, so
        # they skip the image search entirely.
        with_images = ws.plan.images_allowed
        for query in [q for q in queries if q and q.strip()]:
            try:
                text_chunks = retrieve(ws.session_id, query.strip(), top_k=top_k)
                image_chunks = retrieve(
                    ws.session_id,
                    query.strip(),
                    top_k=top_k,
                    kinds=list(_IMAGE_CHUNK_KINDS),
                ) if with_images else []

                for chunk in [*text_chunks, *image_chunks]:
                    if chunk.chunk_id not in ws.seen:
                        ws.chunks.append(chunk)
                        added += 1
            except (GuardrailViolation, GuardrailCheckError):
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "retrieval failed for %r on slide %d: %s",
                    query,
                    ws.plan.slide_number,
                    exc,
                )
                ws.notes.append(f"retrieval failed for {query!r}")

        ws.coverage = ("good" if len(ws.chunks) >= 4
                       else "partial" if ws.chunks else "missing")
        ws.step(f"Slide {ws.plan.slide_number}/{ws.context.total}: "
                f"{len(ws.chunks)} excerpt(s)", phase="research",
                chunks=len(ws.chunks), coverage=ws.coverage)
        log.info("slide %d: research round %d offers %d image asset(s) (images %s)",
                 ws.plan.slide_number, ws.research_rounds, len(ws.image_assets()),
                 "allowed" if with_images else "not allowed on this slide",
                 extra={"session_id": ws.session_id})
        # return (f"{added} new excerpt(s); {len(ws.chunks)} total for this slide.\n"
        #         f"{ws.excerpt_block()}")
        image_block = ws.image_asset_block()
        return (
            f"{added} new excerpt(s); {len(ws.chunks)} total for this slide.\n"
            f"{ws.excerpt_block()}"
            + (f"\n\n{image_block}" if image_block else "")
        )

    @tool
    def write_content(title: str, blocks: list, tables: list | None = None,
                      visual: str = "", notes: str = "", quiz: dict | None = None,
                      faq: list | None = None,
                      image_candidates: list[str] | None = None) -> str:
        """Store this slide's final, grounded CONTENT. This is the last tool you call.

        `blocks` is a list of {"slot": "body1", "text": "", "bullets": ["..."]} — one
        entry per distinct block of copy. Slot ids are free-form grouping labels (body1,
        body2, …): they group related copy, they are NOT template placeholders, so write
        as many as the content genuinely needs.
        `image_candidates` is up to 3 image_ref values from 'Available image assets'
        whose description fits this slide, most relevant first; pass none when none
        fits. The designer decides whether to use them. Never put an image_ref in text.
        `tables` is optional structured data, `visual` is a one-line description of the
        visual treatment the designer should use (e.g. "5 column cards", "3-stage flow"),
        and `notes` is 2-4 sentences of speaker notes explaining the why/how.

        QUIZ slides must pass `quiz` = {"question": "...", "options": ["...", "..."],
        "correct_index": 0, "explanation": "..."} (2-6 options, correct_index 0-based)
        and leave the question and options out of `blocks`. FAQ slides must pass
        `faq` = [{"question": "...", "answer": "..."}, ...]. Other slides pass neither.

        Returns a confirmation of what was stored, or what still needs fixing.
        """
        problems: list[str] = []
        quiz_content = _quiz_of(quiz, problems) if quiz else None
        if ws.plan.is_quiz_question and quiz_content is None and not problems:
            problems.append("this is a QUIZ slide, so `quiz` is required")
        faq_items = _faq_of(faq, problems) if faq else []
        if ws.plan.slide_type == SlideTypes.FAQ and not faq_items and not problems:
            problems.append("this is an FAQ slide, so `faq` is required")

        notes = notes.strip()
        if quiz_content is not None:
            # The answer is ALWAYS in the speaker notes, whatever the deck's placement.
            line = quiz_content.answer_line()
            if line not in notes:
                notes = f"{notes}\n{line}".strip()

        # No slot budgets and no layout repair here: composition is decided later by the
        # code-generating designer, so clipping copy at this stage would lose material.
        written = [_as_block(b) for b in (blocks or [])]
        candidates = _image_candidates_of(ws, list(image_candidates or []), written)
        ws.candidate = SlideDraft(
            slide_number=ws.plan.slide_number, kind=ws.plan.kind,
            title=title.strip() or ws.plan.title,
            blocks=_without_image_refs(written),
            tables=tables or [],
            layout=visual.strip() or ws.task.layout_hint or ws.plan.layout_hint,
            notes=notes, quiz=quiz_content, faq=faq_items,
            image_candidates=candidates)

        ws.step(f"Slide {ws.plan.slide_number}/{ws.context.total}: content written",
                phase="write", blocks=len(ws.candidate.blocks))

        filled = "\n".join(
            f"  [{b.slot}] " + (f"(image {b.image_ref})" if b.image_ref
                                else " / ".join(b.bullets) or b.text)
            for b in ws.candidate.blocks) or (
                "  (structured quiz/FAQ content)" if quiz_content or faq_items
                else "  (nothing landed — send real content)")
        if candidates:
            filled += f"\n  image candidates: {len(candidates)}"
        if problems:
            log.warning("slide %d: write_content needs a correction: %s",
                        ws.plan.slide_number, "; ".join(problems),
                        extra={"session_id": ws.session_id})
            return (f"Stored a draft of slide {ws.plan.slide_number}, but fix this and "
                    f"call write_content again: {'; '.join(problems)}.")
        return (f"Stored the content of slide {ws.plan.slide_number}.\n{filled}\n"
                "Now give your one-sentence summary and call no more tools.")

    return [research_slide, write_content]


def _quiz_of(raw, problems: list[str]) -> QuizContent | None:
    """Validate the model's `quiz` argument.

    Args:
        raw: A dict (or pydantic object) from the tool call.
        problems: Collects a readable reason when the quiz is unusable.

    Returns:
        The quiz, or None when it is invalid.
    """
    data = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw or {})
    try:
        return QuizContent(**data)
    except (ValidationError, TypeError) as exc:
        problems.append(f"`quiz` is invalid ({_first_error(exc)}); it needs a question, "
                        f"{QuizConstants.MIN_OPTIONS}-{QuizConstants.MAX_OPTIONS} options "
                        f"and a 0-based correct_index")
        return None


def _faq_of(raw, problems: list[str]) -> list[FaqItem]:
    """Validate the model's `faq` argument.

    Args:
        raw: A list of {"question", "answer"} dicts.
        problems: Collects a readable reason when an item is unusable.

    Returns:
        The usable items (possibly empty).
    """
    items: list[FaqItem] = []
    for entry in raw or []:
        data = entry.model_dump() if hasattr(entry, "model_dump") else entry
        try:
            item = FaqItem(**dict(data or {}))
        except (ValidationError, TypeError) as exc:
            problems.append(f"an `faq` item is invalid ({_first_error(exc)})")
            continue
        if item.question.strip() and item.answer.strip():
            items.append(item)
    return items


def _first_error(exc: Exception) -> str:
    """A short reason from a validation error, for the model's correction."""
    if isinstance(exc, ValidationError) and exc.errors():
        return str(exc.errors()[0].get("msg", exc))
    return str(exc)
