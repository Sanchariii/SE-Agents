"""Run the deck workflow node-by-node and print saved state.

Run from repository root:
python -m scripts.manual_testing.test_workflow_image_nodes

Set SOURCE_FILE below to a PDF that contains a detected diagram/image.
Set PHASES to run only the stages you are investigating.

Flow:
1. setup       - save source document and register it in session state
2. ingestion   - Document Intelligence -> CkM -> Azure AI Search
3. triage      - classify the deck request
4. extraction  - verify all session documents are ready
5. planning    - create and save the deck plan
6. slides      - run the PPT agent for each slide
7. collect     - save DeckDraft and ContentBundle
8. publish     - render and save PPTX
9. inspect     - print image selection and deck artifacts
10. cleanup    - delete test session, stored files, and search documents
"""

import json
import mimetypes
from pathlib import Path
from pickle import TRUE

from app.core.handlers.session_store import session_store
from app.genai.ingestion.pipeline import process_document
from app.genai.ppt.template import catalog as catalog_mod
from app.genai.ppt.template import registry
from app.genai.retrieval import indexer
from app.genai.workflows import deck_workflow as workflow
from app.utilities import storage_util as storage
from app.core.models.deck_model import DeckPlan, SlidePlan, SlideTask

# --------------------------------------------------------------------------- #
# Manual configuration                                                        #
# --------------------------------------------------------------------------- #

SOURCE_FILE = Path(
    r"C:\projects\SE-Agents-project\dev\deployment\DAI-SE%20Agents\app\data\sample_docs\ROK AI Gateway Engineering Guide 7 1.pdf"
)

SESSION_ID = "manual-workflow-image-test"
MESSAGE = (
    "Create a 5-slide internal training deck from this document. "
    "Use the extracted architecture diagram or screenshot on the most relevant slide. "
    "Keep the content factual, concise, and professional." \
    "it should be for al the team in AI work , goal is for learning "
    
)

# MESSAGE = (
# "create a detailed deck on for training user audiance is IT engineer , "
# "and its for explaing how to use the gateway . "
# "tone should be formal only in english"
# )

FOLLOW_UP_TURNS = [
    (
        "Add more detail to slide 1 about the architecture components. "
        "Keep the extracted architecture diagram on the slide."
    ),
    (
        "Rewrite slide 2 for an engineering audience and use a picture layout "
        "if an extracted image is relevant."
    ),
    (
        "Remove slide 3."
    ),
]

# Change this list while debugging individual workflow nodes.
PHASES = [
    "setup",
    "ingestion",
    "triage",
    "extraction",
    "planning",
    "slides",
    "collect",
    "publish",
    "inspect",
]

# Set true only after examining output. Cleanup removes Redis state, local/blob
# source files, generated decks, and AI Search chunks for SESSION_ID.
CLEANUP_AT_END = True


# --------------------------------------------------------------------------- #
# Printing helpers                                                            #
# --------------------------------------------------------------------------- #

def heading(title: str) -> None:
    """Print a clear phase separator.

    Args:
        title: Name of the phase being run.

    Returns:
        None.
    """
    print(f"\n{'=' * 90}\n{title}\n{'=' * 90}")


def dump(label: str, value: object) -> None:
    """Pretty-print JSON-compatible state.

    Args:
        label: Description of the data being printed.
        value: Pydantic model, dict, list, or scalar to display.

    Returns:
        None.
    """
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")

    print(f"\n--- {label} ---")
    print(json.dumps(value, indent=2, default=str))


def session_snapshot() -> None:
    """Print all important Redis-backed state for the current test session.

    Returns:
        None.
    """
    state = session_store.get_session(SESSION_ID)
    plan, plan_status = session_store.get_plan(SESSION_ID)
    content = session_store.get_content(SESSION_ID)
    draft = session_store.get_draft(SESSION_ID)
    decks = session_store.list_decks(SESSION_ID)

    dump("SessionState", state)
    dump("Plan status", {"plan_status": plan_status})
    dump("Saved DeckPlan", plan)
    dump("Saved ContentBundle", content)
    dump("Saved DeckDraft", draft)
    dump("Saved deck artifacts", decks)


def image_summary() -> None:
    """Print every extracted and selected image path.

    Returns:
        None.
    """
    heading("IMAGE DATA FLOW")

    for record in session_store.latest_docs(SESSION_ID):
        ckm = session_store.get_ckm(record.doc_id, record.version)
        if ckm is None:
            print(f"No CkM found for {record.filename}")
            continue

        figure_chunks = [
            chunk for chunk in ckm.chunks
            if chunk.kind in {"image_description", "frame_caption"}
        ]

        print(f"\nDocument: {record.filename}")
        print(f"Image-like CkM chunks: {len(figure_chunks)}")
        for chunk in figure_chunks:
            print(
                f"  chunk={chunk.chunk_id}\n"
                f"  kind={chunk.kind}\n"
                f"  frame_path={chunk.meta.get('frame_path')!r}\n"
                f"  text={chunk.text[:220]!r}\n"
            )

    draft = session_store.get_draft(SESSION_ID) or {}
    slides = draft.get("slides") or []

    print("\nFinal draft image blocks:")
    image_count = 0
    for slide in slides:
        image_blocks = [
            block for block in slide.get("blocks", [])
            if block.get("image_ref")
        ]

        print(
            f"  slide={slide.get('slide_number')} "
            f"layout={slide.get('layout')!r} "
            f"image_blocks={len(image_blocks)}"
        )

        for block in image_blocks:
            image_count += 1
            print(
                f"    slot={block.get('slot')!r} "
                f"image_ref={block.get('image_ref')!r}"
            )

    print(f"\nTotal selected images in DeckDraft: {image_count}")

    if image_count == 0:
        print(
            "\nFAILURE LOCATION: extraction may have found an image, but no final "
            "slide selected one. Check the per-slide retrieved chunks and whether "
            "writer._repair() changed the chosen layout to an image-capable layout."
        )


def template_summary() -> None:
    """Print actual image-capable layouts in the selected template.

    Returns:
        None.
    """
    heading("TEMPLATE IMAGE LAYOUTS")

    plan, _ = session_store.get_plan(SESSION_ID)
    classification = registry.normalize((plan or {}).get("classification"))
    catalog = catalog_mod.for_classification(classification)

    print(f"Classification: {classification}")
    print(f"Template: {registry.template_path()}")

    layouts = [
        layout for layout in catalog.content_layouts
        if layout.picture_slots
    ]

    print(f"Image-capable layouts: {len(layouts)}")
    for layout in layouts:
        picture_slots = ", ".join(slot.slot for slot in layout.picture_slots)
        text_slots = ", ".join(slot.slot for slot in layout.text_slots)
        print(f"  {layout.name}: pictures=[{picture_slots}], text=[{text_slots}]")


# --------------------------------------------------------------------------- #
# Workflow phases                                                             #
# --------------------------------------------------------------------------- #

def setup() -> None:
    """Save the hardcoded source and register a normal session document.

    Returns:
        None.

    Raises:
        RuntimeError: If source is missing or MIME type is unsupported.
    """
    heading("PHASE 1 - SETUP")

    source = SOURCE_FILE.resolve()
    if not source.is_file():
        raise RuntimeError(f"SOURCE_FILE does not exist: {source}")

    mime_type, _ = mimetypes.guess_type(source.name)
    if mime_type is None:
        raise RuntimeError(f"Could not determine MIME type for: {source.name}")

    data = source.read_bytes()
    storage_path = f"{SESSION_ID}/source/v1/{source.name}"

    storage.save_bytes(storage_path, data)
    record = session_store.register_upload(
        session_id=SESSION_ID,
        filename=source.name,
        mime_type=mime_type,
        sha256="manual-workflow-image-test",
        storage_path=storage_path,
    )

    print(f"Saved source bytes: {len(data)}")
    print(f"Storage path: {storage_path}")
    dump("Registered DocumentRecord", record)
    session_snapshot()


def ingestion() -> None:
    """Run the real Document Intelligence ingestion node synchronously.

    Returns:
        None.

    Raises:
        RuntimeError: If no registered document exists or ingestion fails.
    """
    heading("PHASE 2 - INGESTION")

    docs = session_store.latest_docs(SESSION_ID)
    if not docs:
        raise RuntimeError("No document registered. Run setup first.")

    for record in docs:
        result = process_document(record)
        dump("Processed DocumentRecord", result)

        if result.status.value == "failed":
            raise RuntimeError(
                f"Ingestion failed for {result.filename}: {result.error}"
            )

        ckm = session_store.get_ckm(result.doc_id, result.version)
        if ckm is None:
            raise RuntimeError("Ingestion completed but did not save a CkM.")

        dump("Saved CkM", ckm)

        figures = [
            chunk for chunk in ckm.chunks
            if chunk.kind == "image_description"
        ]
        print(f"\nExtracted Document Intelligence figures: {len(figures)}")
        for figure in figures:
            figure_path = str((figure.meta or {}).get("frame_path") or "")
            print(f"  path={figure_path!r}")
            print(f"  caption={figure.text[:240]!r}")
            if figure_path:
                print(f"  stored_bytes={len(storage.read_file(figure_path))}")

    session_snapshot()


def triage(state: dict) -> dict:
    """Run the real workflow triage node and merge its output.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state.
    """
    heading("PHASE 3 - TRIAGE")

    output = workflow._triage(state)
    dump("Triage node output", output)

    state.update(output)
    dump("Workflow state after triage", state)
    session_snapshot()
    return state


def extraction(state: dict) -> dict:
    """Run the workflow extraction gate and merge its output.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state.
    """
    heading("PHASE 4 - EXTRACTION GATE")

    output = workflow._extraction(state)
    dump("Extraction node output", output)

    state.update(output)
    session_snapshot()
    return state


def planning(state: dict) -> dict:
    """Run the real planning agent and print the stored plan/work order.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state.

    Raises:
        RuntimeError: If the planner did not create a plan.
    """
    heading("PHASE 5 - PLANNING")

    output = workflow._planning(state)
    dump("Planning node output", output)

    state.update(output)
    plan, plan_status = session_store.get_plan(SESSION_ID)

    if not plan:
        raise RuntimeError(
            "Planner did not save a deck plan. Inspect the planner reply above."
        )

    dump("Persisted DeckPlan", plan)
    dump("Work order", state.get("work_order", []))
    print(f"Plan status: {plan_status}")
    template_summary()
    session_snapshot()
    return state


def slides(state: dict) -> dict:
    """Run each PPT agent through the workflow slides node.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state.

    Raises:
        RuntimeError: If a plan is missing.
    """
    heading("PHASE 6 - SLIDES")

    plan, _ = session_store.get_plan(SESSION_ID)
    if not plan:
        raise RuntimeError("No plan exists. Run planning first.")

    output = workflow._slides(state)
    dump("Slides node output", output)

    state.update(output)

    print("\nPer-slide selected layout and image references:")
    for result in state.get("slide_results", []):
        slide = result["slide_draft"]
        images = [
            block["image_ref"]
            for block in slide.get("blocks", [])
            if block.get("image_ref")
        ]
        print(
            f"  slide={slide['slide_number']} "
            f"layout={slide['layout']!r} "
            f"images={images}"
        )

    return state


def collect(state: dict) -> dict:
    """Assemble and persist draft/content state using the real collect node.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state.
    """
    heading("PHASE 7 - COLLECT")

    output = workflow._collect(state)
    dump("Collect node output", output)

    state.update(output)
    session_snapshot()
    image_summary()
    return state


def publish(state: dict) -> dict:
    """Render and persist the real PPTX through the workflow publish node.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state.
    """
    heading("PHASE 8 - PUBLISH")

    output = workflow._publish(state)
    dump("Publish node output", output)

    state.update(output)
    session_snapshot()
    return state


def inspect() -> None:
    """Print final image path, chosen layout, and generated deck storage path.

    Returns:
        None.
    """
    heading("PHASE 9 - FINAL INSPECTION")
    template_summary()
    image_summary()

    decks = session_store.list_decks(SESSION_ID)
    if not decks:
        print("\nNo published deck exists. Run publish first.")
        return

    latest = decks[-1]
    print("\nLatest generated deck:")
    print(f"  version={latest.deck_version}")
    print(f"  storage_path={latest.storage_path}")

    pptx_bytes = storage.read_file(latest.storage_path, container="decks")
    local_pptx = Path("artifacts") / f"workflow-image-test-v{latest.deck_version}.pptx"
    local_pptx.parent.mkdir(exist_ok=True)
    local_pptx.write_bytes(pptx_bytes)
    print(f"  downloaded_copy={local_pptx.resolve()}")


def cleanup() -> None:
    """Remove all test records from Search, storage, and Redis.

    Returns:
        None.
    """
    heading("CLEANUP")

    # Delete indexed chunks first while document/session metadata still exists.
    if indexer.search_configured():
        indexer.purge_session(SESSION_ID)
        print("Deleted Azure AI Search chunks.")
    else:
        print("Azure AI Search is not configured; no index cleanup needed.")

    # delete the pptx file from local storage and blob storage



    # A session prefix contains uploaded source, DI figures, and generated decks.
    upload_count = storage.delete_prefix(SESSION_ID, container="uploads")
    deck_count = storage.delete_prefix(SESSION_ID, container="decks")
    print(f"Deleted uploads/files: {upload_count}")
    print(f"Deleted deck files: {deck_count}")


    deleted_state = session_store.delete_session(SESSION_ID)
    dump("Deleted Redis SessionState", deleted_state)


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #

def run_follow_up_turn(message: str) -> None:
    """Run one complete real workflow turn against the existing session.

    Args:
        message: User follow-up instruction for the persisted deck.

    Returns:
        None.
    """
    heading(f"MULTI-TURN REQUEST: {message}")

    state = {
        "session_id": SESSION_ID,
        "message": message,
        "refresh_content": False,
    }

    # The API route normally persists every user message before it starts the graph.
    session_store.add_message(SESSION_ID, "user", message)

    triage_output = workflow._triage(state)
    state.update(triage_output)
    dump("Triage output", triage_output)

    route_after_triage = workflow._after_triage(state)
    print(f"\nRoute after triage: {route_after_triage}")

    if route_after_triage == "chat":
        print("The agent treated this turn as chat.")
        return

    if route_after_triage == "extraction":
        extraction_output = workflow._extraction(state)
        state.update(extraction_output)
        dump("Extraction output", extraction_output)

        planning_output = workflow._planning(state)
        state.update(planning_output)
        dump("Planning output", planning_output)

        route_after_planning = workflow._after_planning(state)
        print(f"\nRoute after planning: {route_after_planning}")

        if route_after_planning == "clarify":
            print(f"Planner clarification: {state.get('reply', '')}")
            return

        if route_after_planning == "done":
            return

    elif route_after_triage == "slides":
        print("Classification-only turn: re-rendering existing draft.")

    slides_output = workflow._slides(state)
    state.update(slides_output)
    dump("Slides output", slides_output)

    collect_output = workflow._collect(state)
    state.update(collect_output)
    dump("Collect output", collect_output)

    if workflow._after_collect(state) == "publish":
        publish_output = workflow._publish(state)
        state.update(publish_output)
        dump("Publish output", publish_output)

    image_summary()
    session_snapshot()

def main() -> None:
    """Run selected workflow phases in order.

    Returns:
        None.
    """
    state = {
        "session_id": SESSION_ID,
        "message": MESSAGE,
        "refresh_content": False,
    }

    phase_handlers = {
        "setup": lambda: setup(),
        "ingestion": lambda: ingestion(),
        "triage": lambda: triage(state),
        "extraction": lambda: extraction(state),
        "planning": lambda: planning(state),
        "slides": lambda: slides(state),
        "collect": lambda: collect(state),
        "publish": lambda: publish(state),
        "inspect": lambda: inspect(),
    }

    try:
        for phase in PHASES:
            handler = phase_handlers.get(phase)
            if handler is None:
                raise RuntimeError(f"Unknown phase: {phase}")
            handler()
        # for message in FOLLOW_UP_TURNS:
        #     run_follow_up_turn(message)

        inspect()

    finally:
        if CLEANUP_AT_END:
            cleanup()
        else:
            print(
                "\nTest data is retained. Set CLEANUP_AT_END = True, or run "
                "`cleanup()` manually after inspecting the data."
            )


if __name__ == "__main__":
    main()