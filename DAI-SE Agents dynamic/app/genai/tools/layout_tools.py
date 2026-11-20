"""Tools that expose the company brand-template layout catalog to sub-agents, so a plan
or a rewrite can be built with an eye to what layouts actually exist and what shape of
content each one holds — instead of the writer discovering a mismatch after the fact."""

from langchain_core.tools import tool

from app.core.handlers.session_store import session_store
from app.genai.ppt.template import catalog as catalog_mod, registry


def _current_classification(session_id: str) -> str:
    plan_dict, _ = session_store.get_plan(session_id)
    return registry.normalize((plan_dict or {}).get("classification"))


def build_layout_tools(session_id: str) -> list:
    """Layout-catalog tool instances bound to one session."""

    @tool
    def get_available_layouts(classification: str = "") -> str:
        """List every slide layout available on the company brand template, with the
        text/heading/body slots and image slots each one offers. Consult this before
        creating or substantially revising a plan so each slide can be given a
        layout_hint that actually matches its content shape (e.g. a comparison needs a
        multi-column layout, a process needs a timeline/stages layout, an image-bearing
        slide needs a layout with a picture slot). Pass `classification` (public,
        internal, confidential, restricted, cobranded) to check a specific template;
        leave blank to use the deck's current classification."""
        resolved = (registry.normalize(classification) if classification
                   else _current_classification(session_id))
        catalog = catalog_mod.for_classification(resolved)
        menu = catalog.prompt_block()
        return f"Layouts available on the {registry.label(resolved)} template:\n{menu}"

    return [get_available_layouts]