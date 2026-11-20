"""Manual test of the planning node in deck_workflow — real ingested documents, no triage.
Run from repo root:
    python -m scripts.manual_testing.test_workflow_planning
Requires Azure OpenAI configured for summarization; Azure AI Search must also be
configured for search_source_documents to return real hits (analyze_source_documents
works either way once ingestion reaches 'extracted').
"""

from pathlib import Path

from app.core.handlers import document_handler
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.agents.planner_agent import run_planner
from app.genai.agents.work_order import derive_work_order
from app.genai.ingestion.pipeline import process_document
from app.genai.workflows.deck_workflow import _planning, _after_planning
from app.utilities.files_util import detect_mime, sha256_bytes

SESSION_ID_NEW = "manual-planning-real-docs-new"
SESSION_ID_EXISTING = "manual-planning-real-docs-existing"

SAMPLE_DOCS_DIR = Path("app/data/sample_docs")
SAMPLE_DOCS = ["01_smt_ppm_kpivs.txt", "02_reflow_aoi.txt"]  # small, text — fast to ingest

MESSAGE_NEW = "Build a training deck on SMT PPM KPIs and reflow/AOI quality for process engineers"
MESSAGE_EDIT = "Add more depth to slide 2 using the AOI defect data, and remove slide 4"


def seed_real_documents(session_id: str, filenames: list[str] = SAMPLE_DOCS):
    """Runs the same upload -> extract -> chunk -> summarize -> index pipeline the API
    uses, synchronously, so run_planner's document_tools see real content instead of an
    empty session."""
    print(f"Ingesting {len(filenames)} real document(s) for session {session_id!r}\n")
    for filename in filenames:
        data = (SAMPLE_DOCS_DIR / filename).read_bytes()
        mime = document_handler.validate_upload(filename, data)
        record = document_handler.register_upload(session_id, filename, mime, data)
        record = process_document(record)  # sync — no need for asyncio.gather here
        print(f"  {filename}: status={record.status.value} error={record.error!r}")

    
def seed_conversation(session_id: str):
    print(f"Seeding conversation for session {session_id!r}\n")
    session_store.add_message(session_id, "user", "Build a deck on SMT PPM KPIs")
    session_store.add_message(session_id, "assistant", "Here's your deck v1.")


def seed_plan(session_id: str) -> DeckPlan:
    """A confirmed 5-slide plan so run_planner sees an existing deck to revise/remove
    against, and derive_work_order has a real 'before' snapshot to diff."""
    plan = DeckPlan(
        title="SMT PPM KPIs", audience="engineers", objective="review KPIs",
        slides=[SlidePlan(slide_number=n, title=f"Slide {n}",
                          key_points=[f"point {n}.1", f"point {n}.2"],
                          content_needs=[f"topic for slide {n}"])
                for n in range(1, 6)])
    session_store.set_plan(session_id, plan.model_dump(), status="confirmed")
    print(f"Seeded a 5-slide confirmed plan for session {session_id!r}")
    return plan


def step1_run_planner_agent(session_id: str, message: str, hints: str = "",
                            enrich_slides: list[int] | None = None):
    """Calls planner_agent.run_planner directly — real LLM + tool-calling round-trip."""
    print(f"STEP1 : run_planner(message={message!r}, hints={hints!r})\n")
    result = run_planner(session_id, message, hints=hints, enrich_slides=enrich_slides or [])
    print("run_planner output:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def step2_derive_work_order(before: DeckPlan | None, session_id: str, message: str):
    """Same diff derive_work_order performs inside run_planner — shown standalone so you
    can inspect the before/after op assignment without re-running the agent."""
    after_dict, _ = session_store.get_plan(session_id)
    after = DeckPlan(**after_dict) if after_dict else None
    work_order = derive_work_order(before, after, instruction=message)
    print("derive_work_order output:")
    for task in work_order:
        print(f"  slide {task.slide_number}: op={task.op} reuse_chunks={task.reuse_chunks} "
              f"layout_hint={task.layout_hint!r}")
    return work_order


def step3_run_node(session_id: str, message: str, hints: str = "",
                   enrich_slides: list[int] | None = None):
    """Calls the actual LangGraph node function used in the real workflow."""
    print(f"STEP3 : Running planning node for message={message!r}\n")
    state = {"session_id": session_id, "message": message, "hints": hints,
            "enrich_slides": enrich_slides or []}
    result = _planning(state)
    print("node output:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def step4_routing(session_id: str, node_result: dict):
    next_node = _after_planning({**node_result, "session_id": session_id})
    print(f"_after_planning routes to -> {next_node!r}")
    return next_node


def run_scenario(session_id: str, label: str, message: str, seed_existing: bool):
    print(f"\n{'=' * 70}\nSCENARIO: {label}\n{'=' * 70}")
    seed_conversation(session_id)
    before = seed_plan(session_id) if seed_existing else None

    node_result = step3_run_node(session_id, message)
    step4_routing(session_id, node_result)
    step2_derive_work_order(before, session_id, message)

def describe_planner_usage() -> str:
    """Static call-map: which plan_tools tool wraps each app.genai.agents.planner
    function, so a trace/log doesn't need to walk plan_tools.py by hand."""
    return "\n".join([
        "planner.build_plan          <- plan_tools.create_deck_plan (tool)",
        "planner.revise_slides       <- plan_tools.revise_deck_slides (tool)",
        "planner.draft_new_slides    <- plan_tools.add_deck_slides (tool)",
        "planner.apply_classification<- plan_tools.set_deck_classification (tool)",
        "                             <- deck_workflow._triage (direct call, no LLM)",
        "planner.document_summaries  <- document_tools tool + planner.build_plan/"
        "draft_new_slides internally",
    ])

if __name__ == "__main__":
    # describe = describe_planner_usage()
    # print(f"\nPlanner usage:\n{describe}\n")
    # A brand-new deck: no prior plan, so run_planner should call create_deck_plan and
    # every resulting slide should come back as op=create
    # .
    seed_real_documents(SESSION_ID_NEW)
    step1_run_planner_agent(SESSION_ID_NEW,MESSAGE_NEW)
    

    # run_scenario(SESSION_ID_NEW, "brand-new deck (create_deck_plan)",
    #             message=MESSAGE_NEW, seed_existing=False)
    # Bundled edit+remove+classification against an existing 5-slide deck: expect
    # revise_deck_slides + remove_deck_slides + set_deck_classification all in one run,
    # and _after_planning to route to "slides" (not the remove-only shortcut, since an
    # edit is also present).
    # run_scenario(SESSION_ID_EXISTING, "bundled edit + remove + classification",
    #             message=MESSAGE_EDIT, seed_existing=True)

    # A vague instruction against a fresh session with no deck: expect no tool calls,
    # action="clarify", and _after_planning routing to END.
    # run_scenario("manual-planning-clarify", "vague instruction -> clarifying question",
    #             message=MESSAGE_CLARIFY, seed_existing=False)