"""
main.py - Template-driven PowerPoint generation for Rockwell Automation decks.

Run with:   python main.py

What happens when you run it
  1. SAMPLE.pptx is inspected and an in-memory catalog of its masters, layouts, placeholders,
     formatting, decorative shapes and recommended use cases is built (nothing is hard-coded).
  2. A tool-calling LLM agent (LangChain when installed, a native loop otherwise) browses the
     catalog through tools, selects layouts for the requested content and submits a structured,
     validated presentation plan.
  3. A build agent turns the plan into python-pptx code (via the DeckBuilder API defined below),
     executes it in a controlled subprocess, fixes errors and validates the produced deck.
  4. If the agent cannot produce a valid deck, the plan is rendered deterministically instead,
     so a branded .pptx is always produced when a plan exists.

Change USER_MESSAGE to generate a different deck. All LLM traffic goes through apim_llm_service.
"""
import ast
import difflib
import functools
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
import typing
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import requests
from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

import apim_llm_service

try:  # LangChain is optional - the native agent loop below is used when it is missing.
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    LANGCHAIN_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    LANGCHAIN_AVAILABLE = False


# ======================================================================================
# 0. Configuration constants
# ======================================================================================

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "SAMPLE.pptx"
SKILL_MD_PATH = BASE_DIR / "rockwell-skill-v3.md"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_PPTX_PATH = OUTPUT_DIR / "presentation.pptx"
GENERATED_CODE_PATH = OUTPUT_DIR / "generated_make_pptx.py"
PLAN_JSON_PATH = OUTPUT_DIR / "presentation_plan.json"

CHAT_MODEL = "claude-opus-5"
TEMPERATURE = 0.8
MAX_TOKENS = 16000
API_STYLE = "anthropic"  # request/tool format tried first; "openai" is tried automatically if rejected

MAX_PLAN_STEPS = 14  # LLM round-trips allowed while planning
MAX_BUILD_STEPS = 12  # LLM round-trips allowed while building
MAX_CODE_ATTEMPTS = 5  # generated-code executions allowed (errors are fed back for fixing)
CODE_TIMEOUT_SECONDS = 400
TOOL_RESULT_MAX_CHARS = 40000
LLM_HTTP_RETRIES = 3

LLM_TRACKING = {
    "x-rok-user-id": "44305",
    "x-rok-user-email": "kumar.nishant@rockwellautomation.com",
    "x-rok-agent-id": "azure-service-monitor",
    "x-rok-agent-name": "Azure Service Monitor",
    "x-rok-agent-version": "1.0.0",
}

USER_MESSAGE = """
enrich the  deck plan to make better looking adding more things which are visually appling and create one slide deck

{
  "title": "ROK AI Gateway – Internal Training",
  "audience": "Full AI team across Rockwell",
  "objective": "Enable the AI team to understand the ROK AI Gateway architecture, authentication model, API patterns, and security best practices for correct and safe integration.",
  "tone": "Professional, factual, concise",
  "classification": "confidential",
  "languages": [
    "English",
    "French",
    "Spanish",
    "Portuguese",
    "Mandarin",
    "Japanese",
    "German"
  ],
  "slides": [
    {
      "slide_number": 1,
      "kind": "cover",
      "title": "ROK AI Gateway – Engineering Overview",
      "key_points": [
        "Internal training deck for the AI team",
        "Covers architecture, authentication, API patterns, and security",
        "Source: ROK AI Gateway Engineering Guide v7.1"
      ],
      "content_needs": [
        "ROK AI Gateway purpose and positioning as governed ingress point for GenAI"
      ],
      "visual": "Rockwell / ROK AI Gateway logo or branding banner",
      "layout_hint": "cover"
    },
    {
      "slide_number": 2,
      "kind": "content",
      "title": "Architecture Overview – How the Gateway Works",
      "key_points": [
        "Single governed ingress point for all GenAI model access across Rockwell",
        "Routes requests via Azure API Management (APIM)",
        "Supports both OpenAI and Microsoft Foundry-hosted models (Claude, LLaMA)",
        "Strict environment separation: non-prod (genai-api-q.rockwell.com) vs prod (genai-api.rockwell.com)"
      ],
      "content_needs": [
        "ROK AI Gateway architecture diagram or screenshot from the engineering guide",
        "Description of APIM routing to OpenAI and Foundry models",
        "Environment endpoint details non-prod and prod URLs"
      ],
      "visual": "Extracted architecture diagram or screenshot from the ROK AI Gateway Engineering Guide (most relevant slide for the diagram)",
      "layout_hint": "image-right"
    },
    {
      "slide_number": 3,
      "kind": "content",
      "title": "Authentication & Client Setup",
      "key_points": [
        "All clients must authenticate using OAuth 2.0 client credentials via Microsoft Entra ID",
        "A Bearer token must be obtained and submitted with every request",
        "A mandatory APIM subscription key is required alongside the Bearer token",
        "Each client integration requires its own Entra App Registration and client secret or certificate"
      ],
      "content_needs": [
        "OAuth 2.0 client credentials flow description",
        "Microsoft Entra ID Bearer token acquisition process",
        "APIM subscription key requirements",
        "Entra App Registration and client secret/certificate requirements per integration"
      ],
      "visual": "Simple flow diagram: Client → Entra ID (token) → APIM (subscription key) → Gateway",
      "layout_hint": "timeline-horizontal"
    },
    {
      "slide_number": 4,
      "kind": "content",
      "title": "Core API Patterns",
      "key_points": [
        "Chat Completions API: synchronous conversational use cases",
        "Responses API: richer inputs, traceability, and async workflows",
        "Requests API: durable, long-running, or batch workloads",
        "Microsoft Foundry models accessed via a single provider-agnostic endpoint using the 'model' field to select the target model"
      ],
      "content_needs": [
        "Description of Chat Completions API pattern and use cases",
        "Description of Responses API pattern including traceability and async support",
        "Description of Requests API pattern for batch and long-running workloads",
        "Provider-agnostic endpoint usage for Foundry-hosted models"
      ],
      "visual": "Three-column comparison or icon-based layout showing the three API patterns side by side",
      "layout_hint": "three-columns"
    },
    {
      "slide_number": 5,
      "kind": "takeaways",
      "title": "Security Best Practices & Key Takeaways",
      "key_points": [
        "Never hard-code secrets — use secure secret management",
        "Rotate credentials before expiration to avoid service disruption",
        "Handle rate limits gracefully: implement retry logic for HTTP 429 responses",
        "Log request and response IDs for full traceability and debugging",
        "Always use the correct environment endpoint (non-prod vs prod)"
      ],
      "content_needs": [
        "Security best practices listed in the ROK AI Gateway Engineering Guide",
        "Guidance on secret management and credential rotation",
        "Rate limit handling recommendations (429 errors)",
        "Traceability and logging recommendations"
      ],
      "visual": "Checklist or shield icon reinforcing security theme",
      "layout_hint": "bullets-icon-left"
    }
  ]
}
"""

filename  = Path(__file__).stem

def get_apim_token() -> str:
    """Client-credentials token, refreshed well inside its 60-minute lifetime."""
    return apim_llm_service.get_token()


def log(section: str, message: str) -> None:
    """Simple progress logging that never crashes on console encodings."""
    line = f"[{time.strftime('%H:%M:%S')}] [{section}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def create_prompt_payload(skill_md_path: str, user_message: str, additional_system_prompt: str = "") -> Dict:
    """Read the skill markdown and create a chat-completion payload (system + user messages)."""
    path = Path(skill_md_path)
    if path.exists():
        skill_content = path.read_text(encoding="utf-8")
    else:
        log("prompt", f"WARNING: skill file not found at {path.name}; using minimal built-in brand notes")
        skill_content = (
            "Rockwell Automation brand: font Barlow, Rockwell Red #CD163F for accents only, "
            "Navy #1B2A4A for headings, body text #5A6470, left-aligned text, no decorative underlines."
        )

    system_prompt = f"""
You are an AI assistant following the instructions defined below.

=== SKILL INSTRUCTIONS ===
{skill_content}
"""
    if additional_system_prompt:
        system_prompt += f"\n\n=== ADDITIONAL INSTRUCTIONS ===\n{additional_system_prompt}"

    return {
        "messages": [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_message},
        ]
    }


@dataclass
class PipelineConfig:
    """Everything environment-specific, injectable for tests and other deployments."""

    template_path: Path = TEMPLATE_PATH
    skill_md_path: Path = SKILL_MD_PATH
    output_pptx_path: Path = OUTPUT_PPTX_PATH
    generated_code_path: Path = GENERATED_CODE_PATH
    plan_json_path: Path = PLAN_JSON_PATH
    model: str = CHAT_MODEL
    temperature: float = TEMPERATURE
    max_tokens: int = MAX_TOKENS
    api_style: str = API_STYLE
    tracking: Dict[str, str] = field(default_factory=lambda: dict(LLM_TRACKING))
    token_provider: Callable[[], str] = get_apim_token
    use_langchain: bool = True
    max_plan_steps: int = MAX_PLAN_STEPS
    max_build_steps: int = MAX_BUILD_STEPS
    max_code_attempts: int = MAX_CODE_ATTEMPTS
    code_timeout_seconds: int = CODE_TIMEOUT_SECONDS


# ======================================================================================
# 1. Template catalog (built in memory from SAMPLE.pptx)
# ======================================================================================

EMU_PER_INCH = 914400
_PROMPT_RE = re.compile(r"click (to|icon)|edit master|second level|third level", re.IGNORECASE)
_SCHEME_ALIASES = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}


def _inches(emu: Optional[int]) -> float:
    return round((emu or 0) / EMU_PER_INCH, 2)


def _luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return 1.0
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@dataclass
class ThemeInfo:
    name: str
    major_font: str
    minor_font: str
    colors: Dict[str, str]


@dataclass
class PlaceholderInfo:
    idx: int
    type: str
    role: str
    slot: str
    name: str
    prompt: str
    left: float
    top: float
    width: float
    height: float
    font_size: Optional[float] = None
    font_name: Optional[str] = None
    bold: Optional[bool] = None
    color: Optional[str] = None
    align: Optional[str] = None
    bullets: Optional[bool] = None
    all_caps: bool = False
    autofit: Optional[str] = None
    accepts_text: bool = True
    approx_chars: int = 0
    approx_lines: int = 0

    def summary(self) -> Dict[str, Any]:
        data = {
            "idx": self.idx, "slot": self.slot, "role": self.role, "type": self.type,
            "position_in": [self.left, self.top, self.width, self.height],
            "font_size": self.font_size, "font": self.font_name, "bold": self.bold,
            "bullets": self.bullets, "all_caps": self.all_caps or None,
            "approx_chars": self.approx_chars, "approx_lines": self.approx_lines,
            "prompt": self.prompt[:60] if self.prompt else "",
        }
        if self.role == "picture":
            data["accepts_text"] = self.accepts_text
        return {k: v for k, v in data.items() if v not in (None, "", [])}


@dataclass
class LayoutInfo:
    key: str
    master_index: int
    layout_index: int
    name: str
    theme_name: str
    placeholders: List[PlaceholderInfo]
    decor: Dict[str, int]
    fixed_texts: List[str]
    background: str
    tags: List[str]
    columns: int
    recommended_use: str
    sample_titles: List[str] = field(default_factory=list)
    usage_count: int = 0
    canonical: bool = True
    duplicates: List[str] = field(default_factory=list)

    def by_role(self, role: str) -> List[PlaceholderInfo]:
        return [p for p in self.placeholders if p.role == role]

    def has_role(self, role: str) -> bool:
        return any(p.role == role for p in self.placeholders)

    def find(self, target: Union[int, str]) -> Optional[PlaceholderInfo]:
        """Resolve a placeholder by idx (int / numeric string) or slot name (case-insensitive)."""
        if isinstance(target, bool):
            return None
        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            idx = int(target)
            return next((p for p in self.placeholders if p.idx == idx), None)
        if isinstance(target, str):
            key = target.strip().lower().replace(" ", "").replace("_", "")
            for p in self.placeholders:
                if p.slot.lower() == key:
                    return p
            if key in ("heading", "body", "picture", "content", "number", "caption", "chart", "table"):
                return next((p for p in self.placeholders if p.role == key), None)
        return None

    def slot_summary(self) -> str:
        counts: Dict[str, int] = {}
        for p in self.placeholders:
            counts[p.role] = counts.get(p.role, 0) + 1
        parts = []
        for role in ("title", "subtitle", "heading", "body", "content", "picture", "number", "caption", "chart", "table"):
            if role in counts:
                extra = ""
                if role == "picture" and any(p.accepts_text for p in self.by_role("picture")):
                    extra = "(text ok)"
                parts.append(f"{counts[role]} {role}{extra}" if counts[role] > 1 else f"{role}{extra}")
        return ", ".join(parts) if parts else "no placeholders"

    def compact(self) -> str:
        line = f"{self.key} '{self.name}' | slots: {self.slot_summary()}"
        if self.columns >= 2:
            line += f" | {self.columns} columns"
        if self.background != "light":
            line += f" | {self.background} background"
        line += f" | tags: {', '.join(self.tags)}"
        return line

    def details(self) -> Dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "master": self.master_index, "theme": self.theme_name,
            "background": self.background, "columns": self.columns, "tags": self.tags,
            "recommended_use": self.recommended_use,
            "placeholders_in_reading_order": [p.summary() for p in self.placeholders],
            "fixed_layout_shapes": self.decor, "fixed_texts_on_layout": self.fixed_texts,
            "sample_slide_titles_using_it": self.sample_titles,
            "duplicate_layouts_in_other_masters": self.duplicates,
        }


@dataclass
class TemplateCatalog:
    template_path: str
    slide_width: float
    slide_height: float
    themes: List[ThemeInfo]
    layouts: List[LayoutInfo]
    preferred_master: int
    cover_master: int
    sample_slide_count: int

    # ---- lookups -------------------------------------------------------------------
    @property
    def theme(self) -> ThemeInfo:
        return self.themes[self.preferred_master] if self.themes else ThemeInfo("", "Barlow", "Barlow", {})

    def canonical_layouts(self) -> List[LayoutInfo]:
        return [l for l in self.layouts if l.canonical]

    def get(self, key: str) -> Optional[LayoutInfo]:
        return next((l for l in self.layouts if l.key.lower() == key.strip().lower()), None)

    def resolve_layout(self, ref: Union[str, int, None]) -> LayoutInfo:
        """Resolve a key ('M1-L36'), exact / normalised name, or close match; raise LookupError otherwise."""
        if ref is None or ref == "":
            raise LookupError("layout reference is empty")
        if isinstance(ref, int):
            raise LookupError(f"layout must be a key like 'M1-L{ref}' or a layout name, not a bare number")
        text = str(ref).strip()
        found = self.get(text)
        if found:
            return found
        norm = _norm_name(text)
        exact = [l for l in self.layouts if _norm_name(l.name) == norm]
        if exact:
            return next((l for l in exact if l.canonical), exact[0])
        names = {_norm_name(l.name): l for l in self.canonical_layouts()}
        close = difflib.get_close_matches(norm, list(names.keys()), n=3, cutoff=0.75)
        if len(close) == 1 or (close and difflib.SequenceMatcher(None, norm, close[0]).ratio() > 0.9):
            return names[close[0]]
        suggestions = [names[c].compact() for c in close] or [l.compact() for l in self.search(text, limit=3)]
        raise LookupError(f"unknown layout '{ref}'. Did you mean: " + " || ".join(suggestions))

    # ---- search / recommendation ----------------------------------------------------
    def search(self, query: str = "", tags: str = "", min_text_slots: int = 0, needs_picture: bool = False,
               limit: int = 20, include_duplicates: bool = False) -> List[LayoutInfo]:
        tokens = [t for t in re.split(r"[^a-z0-9]+", (query or "").lower()) if t]
        wanted_tags = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
        scored: List[Tuple[float, LayoutInfo]] = []
        for layout in self.layouts:
            if not include_duplicates and not layout.canonical:
                continue
            text_slots = len(layout.by_role("body")) + len(layout.by_role("content")) + len(layout.by_role("heading"))
            if text_slots < min_text_slots:
                continue
            if needs_picture and not layout.has_role("picture"):
                continue
            if wanted_tags and not all(t in layout.tags or t in layout.name.lower() for t in wanted_tags):
                continue
            hay = f"{layout.name} {' '.join(layout.tags)} {layout.recommended_use}".lower()
            score = sum(2.0 if tok in layout.name.lower() else 1.0 for tok in tokens if tok in hay)
            if tokens and score == 0:
                continue
            score += 0.5 if layout.master_index == self.preferred_master else 0
            score += min(layout.usage_count, 4) * 0.1
            scored.append((score, layout))
        scored.sort(key=lambda s: (-s[0], s[1].master_index, s[1].layout_index))
        return [l for _, l in scored[:limit]]

    def recommend(self, intent: str, text_blocks: int = 0, needs_picture: bool = False, has_table: bool = False,
                  has_chart: bool = False, limit: int = 5) -> List[Tuple[LayoutInfo, float, List[str]]]:
        intent_key = (intent or "content").strip().lower().replace(" ", "_")
        wanted, avoid = INTENT_TAGS.get(intent_key, (None, None))
        tokens = [t for t in re.split(r"[^a-z0-9]+", intent_key) if t]
        if wanted is None:
            wanted, avoid = INTENT_TAGS["content"]
        results: List[Tuple[LayoutInfo, float, List[str]]] = []
        for layout in self.canonical_layouts():
            score, reasons = 0.0, []
            for i, tag in enumerate(wanted):
                if tag in layout.tags:
                    score += 4.0 if i == 0 else 2.0
                    reasons.append(f"tag '{tag}'")
            for tag in avoid:
                if tag in layout.tags:
                    score -= 5.0
            for tok in tokens:
                if tok in layout.name.lower() and tok not in ("content", "slide"):
                    score += 1.5
                    reasons.append(f"name matches '{tok}'")
            bodies = len(layout.by_role("body")) + len(layout.by_role("content"))
            if text_blocks:
                if bodies == text_blocks:
                    score += 3.0
                    reasons.append(f"exactly {text_blocks} text blocks")
                elif bodies > text_blocks:
                    score += 1.0 - min(bodies - text_blocks, 4) * 0.3
                    reasons.append(f"{bodies} text blocks available")
                else:
                    score -= 2.0 * (text_blocks - bodies)
            pictures = layout.by_role("picture")
            if needs_picture:
                if pictures:
                    score += 2.5
                    reasons.append(f"{len(pictures)} picture slot(s)")
                else:
                    score -= 1.5
            elif pictures and not any(p.accepts_text for p in pictures) and "cover" not in layout.tags:
                score -= 0.8 * min(len(pictures), 3)
            if has_table or has_chart:
                big = [p for p in layout.placeholders if p.role in ("content", "body") and p.width >= 4 and p.height >= 2.5]
                if big or "title-only" in layout.tags:
                    score += 2.5
                    reasons.append("room for a table/chart")
                else:
                    score -= 2.0
            if layout.master_index == self.preferred_master:
                score += 1.5
                reasons.append("preferred content master")
            elif layout.master_index == self.cover_master and "cover" in layout.tags:
                score += 1.5
            score += min(layout.usage_count, 5) * 0.3
            if layout.usage_count:
                reasons.append(f"used by {layout.usage_count} sample slide(s)")
            results.append((layout, round(score, 2), reasons))
        results.sort(key=lambda r: (-r[1], r[0].master_index, r[0].layout_index))
        return results[:limit]

    # ---- summaries -----------------------------------------------------------------
    def summary_text(self, max_per_tag: int = 60) -> str:
        theme = self.theme
        colors = ", ".join(f"{k} {v}" for k, v in theme.colors.items() if k.startswith("accent") or k in ("dk1", "lt1"))
        lines = [
            f"Template: {Path(self.template_path).name} | slide {self.slide_width} x {self.slide_height} in",
            f"Theme fonts: headings '{theme.major_font}', body '{theme.minor_font}' | theme colors: {colors}",
            f"Sample slides in template: {self.sample_slide_count} (they are removed; only layouts are reused)",
            "Masters:",
        ]
        for i, th in enumerate(self.themes):
            count = sum(1 for l in self.layouts if l.master_index == i)
            used = sum(l.usage_count for l in self.layouts if l.master_index == i)
            note = " <- preferred for content slides" if i == self.preferred_master else ""
            note += " <- cover layouts" if i == self.cover_master and i != self.preferred_master else ""
            lines.append(f"  M{i} '{th.name}': {count} layouts, used by {used} sample slides{note}")
        canon = self.canonical_layouts()
        lines.append(f"Unique layouts: {len(canon)} (keys below; identical layouts in other masters are hidden)")
        families = ["cover", "agenda", "section", "quote", "closing", "title-only", "columns", "process",
                    "dashboard", "photo", "chart", "content", "blank"]
        seen = set()
        for fam in families:
            members = [l for l in canon if fam in l.tags and l.key not in seen]
            if not members:
                continue
            lines.append(f"[{fam}]")
            for l in members[:max_per_tag]:
                seen.add(l.key)
                lines.append(f"  {l.compact()}")
        rest = [l for l in canon if l.key not in seen]
        if rest:
            lines.append("[other]")
            lines.extend(f"  {l.compact()}" for l in rest)
        lines.append("Use get_layout_details(key) for placeholder idx / slot names, geometry and capacity before using a layout.")
        return "\n".join(lines)

    def stats(self) -> Dict[str, Any]:
        return {
            "masters": len(self.themes), "layouts": len(self.layouts),
            "unique_layouts": len(self.canonical_layouts()), "sample_slides": self.sample_slide_count,
            "slide_size_in": [self.slide_width, self.slide_height],
            "fonts": [self.theme.major_font, self.theme.minor_font],
        }

    # ---- construction ---------------------------------------------------------------
    @classmethod
    def from_presentation(cls, prs: Presentation, template_path: Union[str, Path]) -> "TemplateCatalog":
        slide_w, slide_h = _inches(prs.slide_width), _inches(prs.slide_height)
        masters = list(prs.slide_masters)
        themes = [_read_theme(m) for m in masters]
        txstyles = [_read_master_text_styles(m) for m in masters]

        # Sample-slide usage tells us how the template author intended the layouts to be used.
        usage: Dict[Tuple[int, int], List[str]] = {}
        for slide in prs.slides:
            loc = _locate_layout(masters, slide.slide_layout)
            if loc is None:
                continue
            title = ""
            try:
                if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                    title = slide.shapes.title.text_frame.text.strip().replace("\n", " ")
            except Exception:
                title = ""
            if not title:
                for shape in slide.shapes:
                    if shape.has_text_frame and shape.text_frame.text.strip():
                        title = shape.text_frame.text.strip().split("\n")[0]
                        break
            usage.setdefault(loc, []).append(title[:70])

        layouts: List[LayoutInfo] = []
        for mi, master in enumerate(masters):
            for li, layout in enumerate(master.slide_layouts):
                titles = usage.get((mi, li), [])
                layouts.append(_read_layout(layout, mi, li, themes[mi], txstyles[mi], slide_w, slide_h, titles))

        # Canonical selection: identical names across masters -> prefer the most used master.
        master_usage = {mi: sum(len(v) for (m, _), v in usage.items() if m == mi) for mi in range(len(masters))}
        content_masters = [mi for mi in range(len(masters)) if any("content" in l.tags for l in layouts if l.master_index == mi)]
        preferred = max(content_masters or [0], key=lambda mi: (master_usage.get(mi, 0), -mi))
        cover_masters = [mi for mi in range(len(masters)) if any("cover" in l.tags for l in layouts if l.master_index == mi)]
        cover_master = max(cover_masters or [preferred], key=lambda mi: (master_usage.get(mi, 0), -mi))
        groups: Dict[str, List[LayoutInfo]] = {}
        for l in layouts:
            groups.setdefault(_norm_name(l.name), []).append(l)
        for members in groups.values():
            members.sort(key=lambda l: (-(l.master_index == preferred), -master_usage.get(l.master_index, 0), l.master_index))
            for i, l in enumerate(members):
                l.canonical = i == 0
                l.duplicates = [m.key for m in members if m is not l]
        return cls(str(template_path), slide_w, slide_h, themes, layouts, preferred, cover_master, len(prs.slides))


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _locate_layout(masters, layout) -> Optional[Tuple[int, int]]:
    for mi, master in enumerate(masters):
        for li, candidate in enumerate(master.slide_layouts):
            if candidate.part is layout.part:
                return mi, li
    return None


def _read_theme(master) -> ThemeInfo:
    name, major, minor, colors = "", "Barlow", "Barlow", {}
    try:
        theme_part = next((rel.target_part for rel in master.part.rels.values() if rel.reltype == RT.THEME), None)
        if theme_part is not None:
            root = etree.fromstring(theme_part.blob)
            name = root.get("name", "")
            maj = root.find(".//" + qn("a:majorFont") + "/" + qn("a:latin"))
            mnr = root.find(".//" + qn("a:minorFont") + "/" + qn("a:latin"))
            major = maj.get("typeface") if maj is not None and maj.get("typeface") else major
            minor = mnr.get("typeface") if mnr is not None and mnr.get("typeface") else minor
            scheme = root.find(".//" + qn("a:clrScheme"))
            if scheme is not None:
                for child in scheme:
                    tag = etree.QName(child).localname
                    if len(child):
                        val = child[0].get("val") or child[0].get("lastClr")
                        if val:
                            colors[tag] = val.upper()
    except Exception:
        pass
    return ThemeInfo(name, major, minor, colors)


def _read_master_text_styles(master) -> Dict[str, Optional[float]]:
    sizes: Dict[str, Optional[float]] = {"title": None, "body": None, "other": None}
    try:
        tx = master.element.find(qn("p:txStyles"))
        if tx is not None:
            for style_tag, key in (("p:titleStyle", "title"), ("p:bodyStyle", "body"), ("p:otherStyle", "other")):
                style = tx.find(qn(style_tag))
                if style is None:
                    continue
                lvl1 = style.find(qn("a:lvl1pPr"))
                rpr = lvl1.find(qn("a:defRPr")) if lvl1 is not None else None
                if rpr is not None and rpr.get("sz"):
                    sizes[key] = int(rpr.get("sz")) / 100
    except Exception:
        pass
    return sizes


def _scheme_hex(val: str, theme: ThemeInfo) -> Optional[str]:
    return theme.colors.get(_SCHEME_ALIASES.get(val, val))


def _color_element_hex(clr, theme: ThemeInfo) -> Optional[str]:
    """Hex of an <a:srgbClr>/<a:schemeClr> element, applying lumMod/lumOff modifiers approximately."""
    local = etree.QName(clr).localname
    if local == "srgbClr":
        base = clr.get("val")
    elif local == "schemeClr":
        base = _scheme_hex(clr.get("val", ""), theme)
    else:
        return None
    if not base or len(base) != 6:
        return None
    lum_mod = clr.find(qn("a:lumMod"))
    lum_off = clr.find(qn("a:lumOff"))
    if lum_mod is None and lum_off is None:
        return base.upper()
    factor = int(lum_mod.get("val", "100000")) / 100000 if lum_mod is not None else 1.0
    offset = int(lum_off.get("val", "0")) / 100000 if lum_off is not None else 0.0
    channels = []
    for i in (0, 2, 4):
        value = int(base[i:i + 2], 16) / 255
        value = min(1.0, max(0.0, value * factor + offset))
        channels.append(int(round(value * 255)))
    return "".join(f"{c:02X}" for c in channels)


def _fill_luminance(shape, theme: ThemeInfo) -> Optional[float]:
    """Perceived luminance (0 dark .. 1 light) of a shape's solid or gradient fill, None when unknown."""
    sp_pr = shape._element.find(qn("p:spPr"))
    if sp_pr is None:
        return None
    solid = sp_pr.find(qn("a:solidFill"))
    if solid is not None and len(solid):
        hex_value = _color_element_hex(solid[0], theme)
        return _luminance(hex_value) if hex_value else None
    grad = sp_pr.find(qn("a:gradFill"))
    if grad is not None:
        values = []
        for stop in grad.iter(qn("a:gs")):
            if len(stop):
                hex_value = _color_element_hex(stop[0], theme)
                if hex_value:
                    values.append(_luminance(hex_value))
        return sum(values) / len(values) if values else None
    return None


def _read_placeholder_format(sp, theme: ThemeInfo) -> Dict[str, Any]:
    """Formatting hints declared on a layout placeholder (list style level 1 + body properties)."""
    info: Dict[str, Any] = {}
    tx_body = sp.find(qn("p:txBody"))
    if tx_body is None:
        return info
    body_pr = tx_body.find(qn("a:bodyPr"))
    if body_pr is not None:
        if body_pr.find(qn("a:spAutoFit")) is not None:
            info["autofit"] = "shape"
        elif body_pr.find(qn("a:normAutofit")) is not None:
            info["autofit"] = "text"
        elif body_pr.find(qn("a:noAutofit")) is not None:
            info["autofit"] = "none"
    lst = tx_body.find(qn("a:lstStyle"))
    lvl1 = lst.find(qn("a:lvl1pPr")) if lst is not None else None
    if lvl1 is None:
        return info
    if lvl1.get("algn"):
        info["align"] = {"l": "left", "ctr": "center", "r": "right", "just": "justify"}.get(lvl1.get("algn"), lvl1.get("algn"))
    if lvl1.find(qn("a:buNone")) is not None:
        info["bullets"] = False
    elif lvl1.find(qn("a:buChar")) is not None or lvl1.find(qn("a:buAutoNum")) is not None:
        info["bullets"] = True
    rpr = lvl1.find(qn("a:defRPr"))
    if rpr is not None:
        if rpr.get("sz"):
            info["font_size"] = int(rpr.get("sz")) / 100
        if rpr.get("b") is not None:
            info["bold"] = rpr.get("b") == "1"
        if rpr.get("cap") == "all":
            info["all_caps"] = True
        latin = rpr.find(qn("a:latin"))
        if latin is not None and latin.get("typeface"):
            info["font_name"] = latin.get("typeface")
        solid = rpr.find(qn("a:solidFill"))
        if solid is not None and len(solid):
            clr = solid[0]
            if etree.QName(clr).localname == "srgbClr":
                info["color"] = clr.get("val")
            elif etree.QName(clr).localname == "schemeClr":
                info["color"] = _scheme_hex(clr.get("val", ""), theme) or clr.get("val")
    return info


def _placeholder_role(ph_type: str, idx: int, prompt: str, width: float, height: float, top: float) -> Tuple[str, bool]:
    """Infer the semantic role of a placeholder from its type, prompt text and geometry."""
    p = (prompt or "").strip().lower()
    if ph_type in ("TITLE", "CENTER_TITLE"):
        return "title", True
    if ph_type == "SUBTITLE":
        return "subtitle", True
    if ph_type == "PICTURE":
        return "picture", "text" in p
    if ph_type == "CHART":
        return "chart", False
    if ph_type == "TABLE":
        return "table", False
    if p.isdigit():
        return "number", True
    if ph_type == "OBJECT":
        return "content", True
    if ph_type == "BODY":
        if "subhead" in p or "subtitle" in p or (idx == 10 and height <= 0.6 and width >= 6):
            return "subtitle", True
        if "name" in p or "date" in p:
            return "caption", True
        if "second level" in p or height > 0.75:
            return "body", True
        return "heading", True
    return ph_type.lower(), True


def _read_layout(layout, mi: int, li: int, theme: ThemeInfo, txstyles: Dict[str, Optional[float]],
                 slide_w: float, slide_h: float, sample_titles: List[str]) -> LayoutInfo:
    placeholders: List[PlaceholderInfo] = []
    decor: Dict[str, int] = {}
    fixed_texts: List[str] = []
    background = "light"
    slide_area = slide_w * slide_h
    for shape in layout.shapes:
        if shape.is_placeholder:
            pf = shape.placeholder_format
            ph_type = pf.type.name if pf.type is not None else "BODY"
            prompt = shape.text_frame.text.strip().replace("\n", " / ") if shape.has_text_frame else ""
            left, top, width, height = (_inches(shape.left), _inches(shape.top), _inches(shape.width), _inches(shape.height))
            role, accepts_text = _placeholder_role(ph_type, pf.idx, prompt, width, height, top)
            fmt = _read_placeholder_format(shape._element, theme)
            size = fmt.get("font_size")
            if size is None:
                size = txstyles.get("title") if role == "title" else txstyles.get("body")
            size = size or 18.0
            chars_per_line, lines = _capacity(width, height, size, fmt.get("bullets", role == "body"))
            placeholders.append(PlaceholderInfo(
                idx=pf.idx, type=ph_type, role=role, slot="", name=shape.name, prompt=prompt,
                left=left, top=top, width=width, height=height, font_size=round(size, 1),
                font_name=fmt.get("font_name") or theme.minor_font, bold=fmt.get("bold"), color=fmt.get("color"),
                align=fmt.get("align"), bullets=fmt.get("bullets"), all_caps=fmt.get("all_caps", False),
                autofit=fmt.get("autofit"), accepts_text=accepts_text,
                approx_chars=chars_per_line * lines, approx_lines=lines,
            ))
            continue
        kind = shape.shape_type.name.lower() if shape.shape_type is not None else "shape"
        decor[kind] = decor.get(kind, 0) + 1
        if shape.has_text_frame and shape.text_frame.text.strip():
            fixed_texts.append(shape.text_frame.text.strip().replace("\n", " ")[:60])
        area = _inches(shape.width) * _inches(shape.height)
        if area >= 0.45 * slide_area:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                background = "photo"
            else:
                lum = _fill_luminance(shape, theme)
                if lum is not None and lum < 0.5:
                    background = "dark"

    ordered = _reading_order(placeholders)
    counters: Dict[str, int] = {}
    for p in ordered:
        if p.role in ("title", "subtitle") and counters.get(p.role, 0) == 0:
            p.slot = p.role
            counters[p.role] = 1
        else:
            counters[p.role] = counters.get(p.role, 0) + 1
            p.slot = f"{p.role}{counters[p.role]}"
    columns = _column_count(ordered)
    tags = _derive_tags(layout.name, ordered, decor, columns, background)
    use = _recommended_use(layout.name, ordered, tags, columns, background, fixed_texts)
    return LayoutInfo(
        key=f"M{mi}-L{li}", master_index=mi, layout_index=li, name=layout.name.strip(), theme_name=theme.name,
        placeholders=ordered, decor=decor, fixed_texts=fixed_texts[:6], background=background, tags=sorted(tags),
        columns=columns, recommended_use=use, sample_titles=sample_titles[:3], usage_count=len(sample_titles),
    )


def _reading_order(placeholders: List[PlaceholderInfo]) -> List[PlaceholderInfo]:
    rows: List[List[PlaceholderInfo]] = []
    for p in sorted(placeholders, key=lambda x: (x.top, x.left)):
        if rows and abs(p.top - rows[-1][0].top) < 0.35:
            rows[-1].append(p)
        else:
            rows.append([p])
    ordered: List[PlaceholderInfo] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda x: x.left))
    return ordered


def _column_count(ordered: List[PlaceholderInfo]) -> int:
    best = 1
    rows: Dict[float, Dict[str, int]] = {}
    for p in ordered:
        if p.role in ("heading", "body", "content", "picture", "number"):
            key = round(p.top / 0.4)
            rows.setdefault(key, {})
            rows[key][p.role] = rows[key].get(p.role, 0) + 1
    for counts in rows.values():
        best = max(best, max(counts.values()))
    return best if best >= 2 else (1 if ordered else 0)


INTENT_TAGS: Dict[str, Tuple[List[str], List[str]]] = {
    "cover": (["cover"], ["closing"]),
    "title": (["cover"], ["closing"]),
    "agenda": (["agenda"], ["cover", "closing"]),
    "section": (["section", "quote"], ["cover", "closing", "agenda"]),
    "divider": (["section"], ["cover", "closing"]),
    "quote": (["quote", "section"], ["cover", "closing"]),
    "closing": (["closing"], ["cover"]),
    "thank_you": (["closing"], ["cover"]),
    "content": (["content", "text"], ["cover", "agenda", "section", "closing", "blank", "quote"]),
    "bullets": (["text", "title-only", "content"], ["cover", "agenda", "section", "closing", "blank"]),
    "comparison": (["2-column", "columns", "content"], ["cover", "agenda", "section", "closing"]),
    "columns": (["columns", "content"], ["cover", "agenda", "section", "closing"]),
    "process": (["process", "columns", "content"], ["cover", "agenda", "section", "closing"]),
    "timeline": (["process", "content"], ["cover", "agenda", "section", "closing"]),
    "dashboard": (["dashboard", "content"], ["cover", "agenda", "section", "closing"]),
    "kpi": (["dashboard", "content"], ["cover", "agenda", "section", "closing"]),
    "photo": (["photo", "picture-slots", "content"], ["cover", "closing"]),
    "image": (["photo", "picture-slots", "content"], ["cover", "closing"]),
    "screenshot": (["photo", "content"], ["cover", "closing"]),
    "team": (["photo", "content"], ["cover", "closing"]),
    "table": (["title-only", "content"], ["cover", "agenda", "section", "closing"]),
    "chart": (["chart", "title-only", "content"], ["cover", "agenda", "section", "closing"]),
    "blank": (["blank"], []),
}


def _derive_tags(name: str, phs: List[PlaceholderInfo], decor: Dict[str, int], columns: int, background: str) -> set:
    n = name.lower()
    tags = set()
    roles = {p.role for p in phs}
    if any(k in n for k in ("cover", "title slide")):
        tags.add("cover")
    if "agenda" in n or "agena" in n:
        tags.add("agenda")
    if any(k in n for k in ("section", "chapter", "divider")):
        tags.add("section")
    if "quote" in n:
        tags.add("quote")
    if "thank" in n:
        tags.add("closing")
    if "blank" in n and not phs:
        tags.add("blank")
    if any(k in n for k in ("photo", "picture", "image", "screenshot", "headshot", "laptop")) and "no photo" not in n:
        tags.add("photo")
    if any(k in n for k in ("timeline", "progress", "stages", "arrow", "steps", "circles")):
        tags.add("process")
    if any(k in n for k in ("dashboard", "highlights", "quadrant", "infographic", "kpi", "overview")):
        tags.add("dashboard")
    if "chart" in n:
        tags.add("chart")
    if "smartart" in n:
        tags.add("diagram")
    if "title only" in n or (
        "title" in roles and not roles & {"body", "content", "picture", "heading", "number"} and not tags
    ):
        tags.add("title-only")
    if columns >= 2:
        tags.add("columns")
        tags.add(f"{columns}-column")
    if "picture" in roles:
        tags.add("picture-slots")
    if roles & {"body", "content"}:
        tags.add("text")
    if background == "dark":
        tags.add("dark")
    if not tags & {"cover", "agenda", "section", "quote", "closing", "blank"}:
        tags.add("content")
    return tags


def _recommended_use(name: str, phs: List[PlaceholderInfo], tags: set, columns: int, background: str, fixed: List[str]) -> str:
    bodies = len([p for p in phs if p.role in ("body", "content")])
    headings = len([p for p in phs if p.role == "heading"])
    pictures = [p for p in phs if p.role == "picture"]
    if "cover" in tags:
        return "Opening / title slide" + (" with a full-bleed photo slot" if pictures else " (no photo needed)")
    if "closing" in tags:
        return "Closing 'thank you' slide; fixed text: " + "; ".join(fixed[:3])
    if "agenda" in tags:
        return f"Agenda slide with {bodies or headings} numbered items"
    if "section" in tags or "quote" in tags:
        return "Section divider or quote; short title (+ optional subtitle)" + (" on a dark background" if background == "dark" else "")
    if "blank" in tags:
        return "Empty canvas for fully custom shapes"
    if "title-only" in tags:
        return "Title (+ subtitle) with free canvas below for custom cards, tables, charts or images"
    parts = []
    if columns >= 2:
        parts.append(f"{columns}-column comparison / grouped points")
    if pictures:
        parts.append(f"{len(pictures)} picture slot(s)" + (" that accept short text" if any(p.accepts_text for p in pictures) else ""))
    if headings:
        parts.append(f"{headings} heading(s)")
    if bodies:
        parts.append(f"{bodies} text block(s)")
    if "process" in tags:
        parts.append("process / timeline steps")
    if "dashboard" in tags:
        parts.append("KPI or highlight boxes")
    return "Content slide: " + ", ".join(parts) if parts else "Content slide"


def build_catalog(template_path: Union[str, Path]) -> TemplateCatalog:
    """Open the template and build the in-memory catalog."""
    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    prs = Presentation(str(template_path))
    return TemplateCatalog.from_presentation(prs, template_path)


# ======================================================================================
# 2. Structured plan models + validation
# ======================================================================================

class PlanError(ValueError):
    """Raised when a plan cannot be parsed into the structured models."""


@dataclass
class TableSpec:
    columns: List[str]
    rows: List[List[str]]
    font_size: float = 11
    col_widths: Optional[List[float]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TableSpec":
        if not isinstance(d, dict):
            raise PlanError("table must be an object with 'columns' and 'rows'")
        columns = [str(c) for c in d.get("columns") or d.get("header") or []]
        rows = [[str(c) for c in row] for row in (d.get("rows") or [])]
        if not rows and not columns:
            raise PlanError("table needs 'columns' and/or 'rows'")
        return cls(columns, rows, float(d.get("font_size", 11)), d.get("col_widths"))


@dataclass
class ChartSpec:
    chart_type: str
    categories: List[str]
    series: List[Dict[str, Any]]
    title: Optional[str] = None
    show_legend: Optional[bool] = None
    data_labels: bool = False
    number_format: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChartSpec":
        if not isinstance(d, dict):
            raise PlanError("chart must be an object")
        categories = [str(c) for c in d.get("categories") or []]
        series = []
        for s in d.get("series") or []:
            if not isinstance(s, dict) or "values" not in s:
                raise PlanError("each chart series needs 'name' and 'values'")
            values = []
            for v in s["values"]:
                try:
                    values.append(float(v))
                except (TypeError, ValueError):
                    raise PlanError(f"chart value '{v}' is not numeric")
            series.append({"name": str(s.get("name", "Series")), "values": values})
        if not categories or not series:
            raise PlanError("chart needs 'categories' and at least one series")
        for s in series:
            if len(s["values"]) != len(categories):
                raise PlanError(f"series '{s['name']}' has {len(s['values'])} values for {len(categories)} categories")
        return cls(str(d.get("chart_type") or d.get("type") or "column").lower(), categories, series,
                   d.get("title"), d.get("show_legend"), bool(d.get("data_labels", False)), d.get("number_format"))


@dataclass
class PlaceholderContent:
    target: Union[int, str]
    text: Optional[str] = None
    bullets: Optional[List[Any]] = None
    image: Optional[str] = None
    table: Optional[TableSpec] = None
    chart: Optional[ChartSpec] = None
    size: Optional[float] = None
    bold: Optional[bool] = None
    color: Optional[str] = None
    align: Optional[str] = None

    def kind(self) -> str:
        if self.chart:
            return "chart"
        if self.table:
            return "table"
        if self.image:
            return "image"
        if self.bullets:
            return "bullets"
        return "text"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlaceholderContent":
        if not isinstance(d, dict):
            raise PlanError("placeholder entries must be objects")
        target = d.get("target", d.get("idx", d.get("slot")))
        if target is None or target == "":
            raise PlanError("placeholder entry needs 'target' (idx or slot name)")
        bullets = d.get("bullets")
        if bullets is not None and not isinstance(bullets, list):
            bullets = [bullets]
        bullets = [b for b in (bullets or []) if b not in (None, "")] or None
        text = d.get("text")
        text = str(text) if text not in (None, "") else None
        item = cls(
            target=target if isinstance(target, int) else str(target), text=text, bullets=bullets,
            image=d.get("image") or d.get("image_path") or d.get("image_ref") or None,
            table=TableSpec.from_dict(d["table"]) if d.get("table") else None,
            chart=ChartSpec.from_dict(d["chart"]) if d.get("chart") else None,
            size=d.get("size"), bold=d.get("bold"), color=d.get("color"), align=d.get("align"),
        )
        if not any([item.text, item.bullets, item.image, item.table, item.chart]):
            raise PlanError(f"placeholder '{target}' has no content (text, bullets, image, table or chart)")
        return item

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"target": self.target}
        for key in ("text", "bullets", "image", "size", "bold", "color", "align"):
            val = getattr(self, key)
            if val not in (None, [], ""):
                d[key] = val
        if self.table:
            d["table"] = asdict(self.table)
        if self.chart:
            d["chart"] = asdict(self.chart)
        return d


EXTRA_TYPES = ("textbox", "card", "shape", "stat", "icon_circle", "image", "table", "chart", "line")


@dataclass
class ExtraShape:
    type: str
    left: float
    top: float
    width: float
    height: float
    props: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExtraShape":
        if not isinstance(d, dict) or not d.get("type"):
            raise PlanError("extras must be objects with a 'type'")
        kind = str(d["type"]).lower().replace("-", "_")
        if kind not in EXTRA_TYPES:
            raise PlanError(f"unknown extra type '{kind}' (allowed: {', '.join(EXTRA_TYPES)})")
        try:
            left, top = float(d.get("left", d.get("x"))), float(d.get("top", d.get("y")))
        except (TypeError, ValueError):
            raise PlanError(f"extra '{kind}' needs numeric 'left' and 'top' (inches)")
        if kind == "icon_circle":
            width = height = float(d.get("diameter", d.get("width", 0.8)))
        else:
            try:
                width, height = float(d.get("width", d.get("w"))), float(d.get("height", d.get("h")))
            except (TypeError, ValueError):
                raise PlanError(f"extra '{kind}' needs numeric 'width' and 'height' (inches)")
        props = {k: v for k, v in d.items() if k not in ("type", "left", "top", "width", "height", "x", "y", "w", "h")}
        if kind == "table":
            TableSpec.from_dict(props)
        if kind == "chart":
            ChartSpec.from_dict(props)
        return cls(kind, left, top, width, height, props)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "left": self.left, "top": self.top, "width": self.width, "height": self.height, **self.props}


@dataclass
class SlideSpec:
    slide_number: int
    layout: str
    intent: str = "content"
    title: Optional[str] = None
    subtitle: Optional[str] = None
    placeholders: List[PlaceholderContent] = field(default_factory=list)
    extras: List[ExtraShape] = field(default_factory=list)
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any], position: int) -> "SlideSpec":
        if not isinstance(d, dict):
            raise PlanError(f"slide #{position} must be an object")
        layout = d.get("layout") or d.get("layout_key") or d.get("layout_name")
        if not layout:
            raise PlanError(f"slide #{position} has no 'layout'")
        try:
            number = int(d.get("slide_number", position))
        except (TypeError, ValueError):
            number = position
        placeholders = []
        for p in d.get("placeholders") or d.get("blocks") or []:
            if isinstance(p, dict) and not any(v not in (None, "", [], {}) for k, v in p.items() if k not in ("target", "idx", "slot")):
                continue  # an empty draft block (no text/bullets/image/table/chart) is simply skipped
            placeholders.append(PlaceholderContent.from_dict(p))
        extras = [ExtraShape.from_dict(e) for e in (d.get("extras") or d.get("shapes") or [])]
        title = d.get("title")
        subtitle = d.get("subtitle")
        return cls(number, str(layout), str(d.get("intent") or d.get("kind") or "content"),
                   str(title) if title not in (None, "") else None,
                   str(subtitle) if subtitle not in (None, "") else None,
                   placeholders, extras, str(d["notes"]) if d.get("notes") else None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slide_number": self.slide_number, "intent": self.intent, "layout": self.layout,
            "title": self.title, "subtitle": self.subtitle,
            "placeholders": [p.to_dict() for p in self.placeholders],
            "extras": [e.to_dict() for e in self.extras], "notes": self.notes,
        }


@dataclass
class PresentationPlan:
    title: str
    slides: List[SlideSpec]
    purpose: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "PresentationPlan":
        if isinstance(data, list):
            data = {"slides": data}
        if not isinstance(data, dict):
            raise PlanError("plan must be a JSON object")
        raw_slides = data.get("slides")
        if raw_slides is None and "layout" in data:
            raw_slides = [data]
        if not raw_slides:
            raise PlanError("plan has no 'slides'")
        slides = [SlideSpec.from_dict(s, i + 1) for i, s in enumerate(raw_slides)]
        title = data.get("title") or data.get("deck_title") or (slides[0].title or "Presentation")
        return cls(str(title), slides, data.get("purpose"))

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "purpose": self.purpose, "slides": [s.to_dict() for s in self.slides]}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


@dataclass
class PlanValidation:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fixable: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _estimate_lines(items: Sequence[str], chars_per_line: int) -> int:
    return sum(max(1, math.ceil(len(str(t)) / max(chars_per_line, 1))) for t in items)


def _capacity(width: float, height: float, size: float, bullets: bool = True) -> Tuple[int, int]:
    """Conservative (chars per line, lines) a text box of width x height inches holds at `size` pt."""
    usable_width = max(0.4, width - 0.2 - (0.22 if bullets else 0.0)) * 72
    usable_height = max(0.2, height - 0.1) * 72
    return max(6, int(usable_width / (size * 0.52))), max(1, int(usable_height / (size * 1.25)))


def _text_fits(items: Sequence[str], width: float, height: float, size: float, bullets: bool = True) -> bool:
    chars_per_line, lines = _capacity(width, height, size, bullets)
    needed = _estimate_lines(items, chars_per_line) + 0.3 * max(0, len(items) - 1)
    return needed <= lines


def validate_plan(plan: PresentationPlan, catalog: TemplateCatalog) -> PlanValidation:
    """Check a plan against the catalog: layouts, placeholder targets, content fit, geometry."""
    result = PlanValidation()
    masters_used = set()
    numbers = [s.slide_number for s in plan.slides]
    if len(set(numbers)) != len(numbers):
        result.warnings.append("duplicate slide_number values; slides are rendered in list order")
    for s in plan.slides:
        tag = f"slide {s.slide_number}"
        try:
            layout = catalog.resolve_layout(s.layout)
        except LookupError as exc:
            result.errors.append(f"{tag}: {exc}")
            continue
        if layout.key != s.layout:
            s.layout = layout.key  # normalise to the catalog key
        if "cover" not in layout.tags and "closing" not in layout.tags:
            masters_used.add(layout.master_index)
        if s.title and not layout.has_role("title"):
            result.warnings.append(f"{tag}: layout '{layout.name}' has no title placeholder; title is drawn as a text box")
        if not s.title and layout.has_role("title") and "closing" not in layout.tags:
            result.fixable.append(f"{tag}: no title although the layout has a title placeholder")
        if s.subtitle and not layout.has_role("subtitle"):
            result.warnings.append(f"{tag}: layout has no subtitle placeholder; subtitle is drawn as a text box under the title")
        used = set()
        for pc in s.placeholders:
            info = layout.find(pc.target)
            if info is None:
                available = ", ".join(f"{p.slot}(idx {p.idx})" for p in layout.placeholders)
                result.errors.append(f"{tag}: placeholder '{pc.target}' does not exist on '{layout.name}'. Available: {available}")
                continue
            if info.idx in used:
                result.warnings.append(f"{tag}: placeholder {info.slot} is filled twice; the last entry wins")
            used.add(info.idx)
            kind = pc.kind()
            if kind == "image":
                if not Path(str(pc.image)).exists() and not (BASE_DIR / str(pc.image)).exists():
                    result.fixable.append(f"{tag}: image '{pc.image}' not found on disk; the slot will stay empty (use text or remove it)")
                elif info.role not in ("picture", "content"):
                    result.warnings.append(f"{tag}: image placed on a {info.role} placeholder; it is inserted at that position instead")
            elif kind in ("table", "chart"):
                if info.role not in ("content", "body", "chart", "table") or info.width < 3 or info.height < 1.5:
                    result.warnings.append(f"{tag}: {kind} on {info.slot} ({info.width}x{info.height} in) may be cramped")
            else:
                if info.role == "picture" and not info.accepts_text:
                    result.warnings.append(f"{tag}: text placed on picture slot {info.slot}; keep it very short")
                items = [str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in (pc.bullets or [pc.text or ""])]
                size = pc.size or info.font_size or 18
                chars_per_line, lines = _capacity(info.width, info.height, size, bool(info.bullets))
                needed = _estimate_lines(items, chars_per_line)
                if needed > lines and not _text_fits(items, info.width, info.height, max(size * 0.7, 11), bool(info.bullets)):
                    result.fixable.append(
                        f"{tag}: {info.slot} (idx {info.idx}) holds ~{lines} lines at {size}pt but the content needs ~{needed}; "
                        "the font will be shrunk below 70% - shorten the text or use a layout with more room")
        for p in layout.placeholders:
            if p.idx not in used and p.role in ("body", "content", "heading") and p.slot not in ("title", "subtitle"):
                result.warnings.append(f"{tag}: {p.slot} (idx {p.idx}) is left empty and will be removed from the slide")
        for e in s.extras:
            if e.left < -0.1 or e.top < -0.1 or e.left + e.width > catalog.slide_width + 0.1 or e.top + e.height > catalog.slide_height + 0.1:
                result.fixable.append(f"{tag}: extra '{e.type}' at ({e.left},{e.top}) {e.width}x{e.height} in exceeds the {catalog.slide_width}x{catalog.slide_height} in slide")
            if e.type == "image" and not Path(str(e.props.get("path", ""))).exists():
                result.fixable.append(f"{tag}: extra image '{e.props.get('path')}' not found on disk")
            if e.type in ("textbox", "card", "stat") and not (e.props.get("text") or e.props.get("title") or e.props.get("value")):
                result.errors.append(f"{tag}: extra '{e.type}' has no text")
    if len(masters_used) > 1:
        result.warnings.append(f"content slides mix masters {sorted(masters_used)}; prefer layouts from M{catalog.preferred_master} for a consistent look")
    return result


# ======================================================================================
# 3. DeckBuilder - deterministic python-pptx renderer that preserves the template
# ======================================================================================

BRAND_DEFAULTS: Dict[str, str] = {
    "red": "CD163F", "red_dark": "A8112F", "navy": "1B2A4A", "blue": "003E7E", "light_blue": "00AEEF",
    "orange": "F58024", "yellow": "F9C20A", "green": "77C14C", "body": "5A6470", "caption": "8A929E",
    "border": "D8DBDF", "surface": "F6F7F8", "white": "FFFFFF", "black": "000000", "red_light": "FEF2F2",
    "gray": "CCCCCC", "dark_gray": "4C4C4C",
}
_THEME_TO_BRAND = {"accent1": "red", "accent2": "blue", "accent3": "orange", "accent4": "light_blue",
                   "accent5": "yellow", "accent6": "green", "lt1": "white", "dk1": "black", "dk2": "gray", "lt2": "dark_gray"}
_SHAPES = {
    "rectangle": MSO_SHAPE.RECTANGLE, "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE, "oval": MSO_SHAPE.OVAL,
    "chevron": MSO_SHAPE.CHEVRON, "right_arrow": MSO_SHAPE.RIGHT_ARROW, "left_arrow": MSO_SHAPE.LEFT_ARROW,
    "up_arrow": MSO_SHAPE.UP_ARROW, "down_arrow": MSO_SHAPE.DOWN_ARROW, "pentagon": MSO_SHAPE.PENTAGON,
    "diamond": MSO_SHAPE.DIAMOND, "hexagon": MSO_SHAPE.HEXAGON, "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "snip_rectangle": MSO_SHAPE.SNIP_2_SAME_RECTANGLE, "round_top_rectangle": MSO_SHAPE.ROUND_2_SAME_RECTANGLE,
}
_CHART_TYPES = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED, "stacked_column": XL_CHART_TYPE.COLUMN_STACKED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED, "stacked_bar": XL_CHART_TYPE.BAR_STACKED, "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE, "doughnut": XL_CHART_TYPE.DOUGHNUT, "area": XL_CHART_TYPE.AREA,
}
_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT, "justify": PP_ALIGN.JUSTIFY}
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
BRAND: Dict[str, str] = dict(BRAND_DEFAULTS)


def brand_palette(theme: ThemeInfo) -> Dict[str, str]:
    palette = dict(BRAND_DEFAULTS)
    for scheme_name, brand_name in _THEME_TO_BRAND.items():
        value = theme.colors.get(scheme_name)
        if value and len(value) == 6:
            palette[brand_name] = value.upper()
    return palette


def to_rgb(color: Union[str, RGBColor, None], palette: Optional[Dict[str, str]] = None) -> RGBColor:
    """Brand colour name ('navy'), hex ('#1B2A4A' / '1B2A4A') or RGBColor -> RGBColor."""
    palette = palette or BRAND
    if isinstance(color, RGBColor):
        return color
    if not color:
        return RGBColor.from_string(palette["body"])
    key = str(color).strip().lower().replace("-", "_").replace(" ", "_")
    if key in palette:
        return RGBColor.from_string(palette[key])
    hex_value = str(color).strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
        return RGBColor.from_string(hex_value.upper())
    raise ValueError(f"unknown colour '{color}'. Use a brand name ({', '.join(sorted(palette))}) or hex")


class SlideHandle:
    """One generated slide. Fills template placeholders first, then adds optional extra shapes."""

    def __init__(self, deck: "DeckBuilder", slide, layout: LayoutInfo):
        self.deck = deck
        self.slide = slide
        self.layout = layout
        self.used_idx: set = set()
        self.warnings: List[str] = []

    # ---- helpers -------------------------------------------------------------------
    def _warn(self, message: str) -> None:
        self.warnings.append(message)
        if self.deck.verbose:
            log("deck", f"WARNING slide {self.index}: {message}")

    @property
    def index(self) -> int:
        return self.deck.slides.index(self) + 1 if self in self.deck.slides else len(self.deck.slides) + 1

    def _placeholder_shapes(self) -> Dict[int, Any]:
        return {ph.placeholder_format.idx: ph for ph in self.slide.placeholders}

    def _resolve(self, target: Union[int, str]) -> Tuple[Optional[PlaceholderInfo], Any]:
        info = self.layout.find(target)
        if info is None:
            self._warn(f"placeholder '{target}' does not exist on layout '{self.layout.name}' - skipped")
            return None, None
        shape = self._placeholder_shapes().get(info.idx)
        if shape is None:
            self._warn(f"placeholder {info.slot} (idx {info.idx}) is no longer on the slide - skipped")
            return info, None
        return info, shape

    def placeholder_bounds(self, target: Union[int, str]) -> Optional[Tuple[float, float, float, float]]:
        """(left, top, width, height) in inches of a placeholder, or None if it does not exist."""
        info = self.layout.find(target)
        if info is None:
            return None
        return info.left, info.top, info.width, info.height

    def remove_placeholder(self, target: Union[int, str]) -> bool:
        info = self.layout.find(target)
        shape = self._placeholder_shapes().get(info.idx) if info else None
        if shape is None:
            return False
        shape._element.getparent().remove(shape._element)
        self.used_idx.add(info.idx)
        return True

    def _fit_size(self, info: PlaceholderInfo, items: Sequence[str], requested: Optional[float]) -> Optional[float]:
        """Return an explicit font size only when the inherited size would overflow the placeholder."""
        if requested:
            return requested
        base = info.font_size or 18.0
        minimum = 22.0 if info.role == "title" else 10.0
        bullets = bool(info.bullets) and len(items) > 1
        size = base
        while size > minimum and not _text_fits(items, info.width, info.height, size, bullets):
            size -= 1.0
        return None if size >= base else max(size, minimum)

    def _write_paragraph(self, paragraph, text: str, size: Optional[float] = None, bold: Optional[bool] = None,
                         color: Optional[str] = None, font: Optional[str] = None, italic: Optional[bool] = None) -> None:
        for run in list(paragraph.runs):
            run._r.getparent().remove(run._r)
        segments: List[Tuple[str, bool]] = []
        pos = 0
        for m in _BOLD_RE.finditer(text):
            if m.start() > pos:
                segments.append((text[pos:m.start()], False))
            segments.append((m.group(1), True))
            pos = m.end()
        if pos < len(text) or not segments:
            segments.append((text[pos:], False))
        for seg_text, seg_bold in segments:
            run = paragraph.add_run()
            run.text = seg_text
            if size:
                run.font.size = Pt(size)
            if bold or seg_bold:
                run.font.bold = True
            elif bold is False:
                run.font.bold = False
            if italic:
                run.font.italic = True
            if color:
                run.font.color.rgb = to_rgb(color, self.deck.brand)
            if font:
                run.font.name = font

    def _fill_text_frame(self, tf, items: Sequence[Any], info: Optional[PlaceholderInfo] = None, size: Optional[float] = None,
                         bold: Optional[bool] = None, color: Optional[str] = None, align: Optional[str] = None,
                         font: Optional[str] = None) -> None:
        normalised: List[Tuple[str, int, Optional[bool]]] = []
        for item in items:
            if isinstance(item, dict):
                normalised.append((str(item.get("text", "")), int(item.get("level", 0) or 0), item.get("bold")))
            else:
                normalised.append((str(item), 0, None))
        fit = self._fit_size(info, [t for t, _, _ in normalised], size) if info else size
        tf.clear()
        tf.word_wrap = True
        for i, (text, level, item_bold) in enumerate(normalised):
            paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            if level:
                paragraph.level = min(level, 4)
            if align:
                paragraph.alignment = _ALIGN.get(align.lower(), PP_ALIGN.LEFT)
            self._write_paragraph(paragraph, text, fit, item_bold if item_bold is not None else bold, color, font)

    # ---- placeholder population ----------------------------------------------------
    def set_title(self, text: str, size: Optional[float] = None) -> bool:
        """Fill the title placeholder (falls back to a text box at the template title position)."""
        info = self.layout.find("title")
        if info is None:
            self._warn("layout has no title placeholder; drawing the title as a text box")
            self.add_textbox(text, 1.14, 0.38, self.deck.slide_width - 1.6, 0.6, size=size or 28,
                             color="navy", font=self.deck.heading_font)
            return False
        return self.set_text(info.idx, text, size=size)

    def set_subtitle(self, text: str, size: Optional[float] = None) -> bool:
        """Fill the subtitle placeholder (falls back to a text box under the title)."""
        info = self.layout.find("subtitle")
        if info is None:
            title = self.layout.find("title")
            top = (title.top + title.height + 0.05) if title else 1.0
            left = title.left if title else 1.14
            width = title.width if title else self.deck.slide_width - 1.6
            self._warn("layout has no subtitle placeholder; drawing the subtitle as a text box")
            self.add_textbox(text, left, top, width, 0.4, size=size or 14, color="body")
            return False
        return self.set_text(info.idx, text, size=size)

    def set_text(self, target: Union[int, str], text: Union[str, Sequence[Any]], size: Optional[float] = None,
                 bold: Optional[bool] = None, color: Optional[str] = None, align: Optional[str] = None) -> bool:
        """Put text (or a list of paragraphs) into a placeholder addressed by idx or slot name."""
        info, shape = self._resolve(target)
        if shape is None:
            return False
        items = list(text) if isinstance(text, (list, tuple)) else [text]
        if shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE or not shape.has_text_frame:
            # Picture slots cannot reliably display typed text: swap in a text box with the slot's formatting.
            label = " ".join(str(i.get("text", i) if isinstance(i, dict) else i) for i in items)
            short = len(label) <= 4
            self.remove_placeholder(info.idx)
            self.add_textbox(label, info.left, info.top, info.width, info.height,
                             size=size or (max(info.font_size or 14, 24) if short else (info.font_size or 14)),
                             bold=bool(bold) or short, color=color or info.color or "white",
                             align=align or info.align or "center", anchor="middle")
            return True
        self._fill_text_frame(shape.text_frame, items, info, size, bold, color, align)
        self.used_idx.add(info.idx)
        return True

    def set_bullets(self, target: Union[int, str], items: Sequence[Any], size: Optional[float] = None,
                    color: Optional[str] = None) -> bool:
        """Fill a placeholder with bullet items: strings or {'text', 'level', 'bold'} dicts."""
        return self.set_text(target, list(items), size=size, color=color)

    def set_picture(self, target: Union[int, str], image_path: Union[str, Path]) -> bool:
        """Insert an image into a picture placeholder (cropped to fill) or at another placeholder's bounds."""
        info, shape = self._resolve(target)
        if shape is None:
            return False
        path = Path(str(image_path))
        if not path.exists():
            path = BASE_DIR / str(image_path)
        if not path.exists():
            self._warn(f"image '{image_path}' not found; placeholder {info.slot} removed")
            self.remove_placeholder(info.idx)
            return False
        if shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE:
            shape.insert_picture(str(path))
        else:
            self.remove_placeholder(info.idx)
            self.add_image(str(path), info.left, info.top, info.width, info.height)
        self.used_idx.add(info.idx)
        return True

    def set_table(self, target: Union[int, str], columns: Sequence[str], rows: Sequence[Sequence[Any]],
                  font_size: float = 11, col_widths: Optional[Sequence[float]] = None) -> Any:
        """Replace a placeholder with a branded table occupying the same area."""
        info, shape = self._resolve(target)
        if shape is None:
            return None
        self.remove_placeholder(info.idx)
        return self.add_table(columns, rows, info.left, info.top, info.width, info.height, font_size, col_widths)

    def set_chart(self, target: Union[int, str], chart_type: str, categories: Sequence[str], series: Sequence[Dict[str, Any]],
                  title: Optional[str] = None, show_legend: Optional[bool] = None, data_labels: bool = False,
                  number_format: Optional[str] = None) -> Any:
        """Replace a placeholder with a branded chart occupying the same area."""
        info, shape = self._resolve(target)
        if shape is None:
            return None
        self.remove_placeholder(info.idx)
        return self.add_chart(chart_type, categories, series, info.left, info.top, info.width, info.height,
                              title, show_legend, data_labels, number_format)

    def set_notes(self, text: str) -> None:
        """Speaker notes."""
        self.slide.notes_slide.notes_text_frame.text = text

    # ---- extra shapes (brand styled) -----------------------------------------------
    def add_textbox(self, text: Union[str, Sequence[Any]], left: float, top: float, width: float, height: float,
                    size: float = 12, bold: bool = False, color: str = "body", align: str = "left",
                    fill: Optional[str] = None, italic: bool = False, font: Optional[str] = None,
                    anchor: str = "top", line_spacing: Optional[float] = None) -> Any:
        """Free text box. `text` may contain **bold** segments or be a list of paragraphs."""
        box = self.slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = Inches(0.05)
        tf.margin_top = tf.margin_bottom = Inches(0.03)
        tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}.get(anchor, MSO_ANCHOR.TOP)
        if fill:
            box.fill.solid()
            box.fill.fore_color.rgb = to_rgb(fill, self.deck.brand)
        items = list(text) if isinstance(text, (list, tuple)) else str(text).split("\n")
        for i, item in enumerate(items):
            paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            paragraph.alignment = _ALIGN.get(align, PP_ALIGN.LEFT)
            if line_spacing:
                paragraph.line_spacing = line_spacing
            raw = str(item.get("text", "")) if isinstance(item, dict) else str(item)
            if raw.startswith(("- ", "* ")):
                raw = "\u2022 " + raw[2:]
            self._write_paragraph(paragraph, raw, size, bold, color, font or self.deck.body_font, italic)
        return box

    def add_shape(self, shape: str, left: float, top: float, width: float, height: float, fill: Optional[str] = "red",
                  line: Optional[str] = None, text: str = "", size: float = 12, color: str = "white", bold: bool = True,
                  align: str = "center", corner_radius: Optional[float] = None) -> Any:
        """Auto shape (rectangle, rounded_rectangle, oval, chevron, right_arrow, pentagon, diamond, ...)."""
        kind = _SHAPES.get(shape.lower().replace("-", "_").replace(" ", "_"))
        if kind is None:
            raise ValueError(f"unknown shape '{shape}'. Allowed: {', '.join(sorted(_SHAPES))}")
        shp = self.slide.shapes.add_shape(kind, Inches(left), Inches(top), Inches(width), Inches(height))
        shp.shadow.inherit = False
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = to_rgb(fill, self.deck.brand)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = to_rgb(line, self.deck.brand)
            shp.line.width = Pt(1)
        else:
            shp.line.fill.background()
        if corner_radius is not None and kind == MSO_SHAPE.ROUNDED_RECTANGLE:
            shp.adjustments[0] = corner_radius
        if text:
            tf = shp.text_frame
            tf.word_wrap = True
            tf.margin_left = tf.margin_right = Inches(0.08)
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            for i, line_text in enumerate(str(text).split("\n")):
                paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                paragraph.alignment = _ALIGN.get(align, PP_ALIGN.CENTER)
                self._write_paragraph(paragraph, line_text, size, bold, color, self.deck.body_font)
        return shp

    def add_line(self, left: float, top: float, width: float, height: float, color: str = "border", weight: float = 1.0) -> Any:
        """Straight connector from (left, top) to (left+width, top+height)."""
        connector = self.slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(left), Inches(top),
                                                    Inches(left + width), Inches(top + height))
        connector.line.color.rgb = to_rgb(color, self.deck.brand)
        connector.line.width = Pt(weight)
        return connector

    def add_card(self, title: str, body: Union[str, Sequence[Any]], left: float, top: float, width: float, height: float,
                 accent: Optional[str] = "red", title_size: float = 14, body_size: float = 11, fill: str = "white",
                 title_color: str = "navy") -> Any:
        """Rounded card with an accent bar, a bold title and body text / bullets (brand guide component)."""
        card = self.add_shape("rounded_rectangle", left, top, width, height, fill=fill, line="border", corner_radius=0.06)
        if accent:
            self.add_shape("rectangle", left + 0.15, top + 0.12, 0.5, 0.06, fill=accent)
        self.add_textbox(title, left + 0.12, top + 0.22, width - 0.24, 0.45, size=title_size, bold=True, color=title_color)
        body_items = list(body) if isinstance(body, (list, tuple)) else [body]
        self.add_textbox(["\u2022 " + str(b) if isinstance(body, (list, tuple)) and len(body_items) > 1 else str(b) for b in body_items],
                         left + 0.12, top + 0.68, width - 0.24, max(height - 0.8, 0.3), size=body_size, color="body")
        return card

    def add_stat(self, value: str, label: str, left: float, top: float, width: float, height: float, color: str = "red",
                 value_size: float = 32, label_size: float = 11, fill: Optional[str] = None, align: str = "left") -> Any:
        """Big number with a caption - for KPIs and key figures."""
        if fill:
            self.add_shape("rounded_rectangle", left, top, width, height, fill=fill, line=None, corner_radius=0.08)
        self.add_textbox(value, left + 0.1, top + 0.05, width - 0.2, height * 0.6, size=value_size, bold=True, color=color, align=align)
        return self.add_textbox(label, left + 0.1, top + height * 0.6, width - 0.2, height * 0.4 - 0.05, size=label_size, color="body", align=align)

    def add_icon_circle(self, text: str, left: float, top: float, diameter: float = 0.8, fill: str = "red",
                        color: str = "white", size: Optional[float] = None) -> Any:
        """Filled circle with a short label or number - a solid, brand-style icon substitute."""
        return self.add_shape("oval", left, top, diameter, diameter, fill=fill, text=text,
                              size=size or max(10, diameter * 22), color=color, bold=True)

    def add_image(self, path: Union[str, Path], left: float, top: float, width: Optional[float] = None,
                  height: Optional[float] = None) -> Any:
        """Picture fitted inside the given box (aspect ratio preserved)."""
        file_path = Path(str(path))
        if not file_path.exists():
            file_path = BASE_DIR / str(path)
        if not file_path.exists():
            self._warn(f"image '{path}' not found - skipped")
            return None
        pic = self.slide.shapes.add_picture(str(file_path), Inches(left), Inches(top), width=Inches(width) if width else None,
                                            height=None if width else (Inches(height) if height else None))
        if width and height and pic.height > Inches(height):
            ratio = pic.width / pic.height
            pic.height = Inches(height)
            pic.width = int(Inches(height) * ratio)
        if width and height:  # centre inside the box
            pic.left = Inches(left) + int((Inches(width) - pic.width) / 2)
            pic.top = Inches(top) + int((Inches(height) - pic.height) / 2)
        return pic

    def add_table(self, columns: Sequence[str], rows: Sequence[Sequence[Any]], left: float, top: float, width: float,
                  height: float, font_size: float = 11, col_widths: Optional[Sequence[float]] = None,
                  header_fill: str = "navy") -> Any:
        """Brand table: navy header with white uppercase text, white body cells, thin bottom borders."""
        columns = [str(c) for c in (columns or [])]
        rows = [[str(c) for c in r] for r in (rows or [])]
        n_cols = len(columns) or max((len(r) for r in rows), default=1)
        n_rows = len(rows) + (1 if columns else 0)
        if n_rows == 0:
            raise ValueError("table needs at least one row")
        row_height = Inches(min(height / n_rows, 0.55))  # compact rows, top-aligned inside the given area
        frame = self.slide.shapes.add_table(n_rows, n_cols, Inches(left), Inches(top), Inches(width), row_height * n_rows)
        table = frame.table
        tbl_pr = frame._element.graphic.graphicData.tbl.tblPr
        tbl_pr.set("bandRow", "0")
        tbl_pr.set("firstRow", "1" if columns else "0")
        widths = list(col_widths) if col_widths and len(col_widths) == n_cols else [width / n_cols] * n_cols
        scale = width / sum(widths)
        for i, w in enumerate(widths):
            table.columns[i].width = Inches(w * scale)
        brand = self.deck.brand
        for r in range(n_rows):
            table.rows[r].height = row_height
            is_header = bool(columns) and r == 0
            values = columns if is_header else rows[r - (1 if columns else 0)]
            for c in range(n_cols):
                cell = table.cell(r, c)
                value = values[c] if c < len(values) else ""
                cell.margin_left = cell.margin_right = Inches(0.08)
                cell.margin_top = cell.margin_bottom = Inches(0.04)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.fill.solid()
                cell.fill.fore_color.rgb = to_rgb(header_fill if is_header else "white", brand)
                paragraph = cell.text_frame.paragraphs[0]
                self._write_paragraph(paragraph, value.upper() if is_header else value, font_size, is_header,
                                      "white" if is_header else "body", self.deck.body_font)
                _set_cell_borders(cell, bottom=brand["border"] if not is_header else None)
        return frame

    def add_chart(self, chart_type: str, categories: Sequence[str], series: Sequence[Dict[str, Any]], left: float, top: float,
                  width: float, height: float, title: Optional[str] = None, show_legend: Optional[bool] = None,
                  data_labels: bool = False, number_format: Optional[str] = None) -> Any:
        """Native chart (column, stacked_column, bar, stacked_bar, line, pie, doughnut, area) in brand colours."""
        xl_type = _CHART_TYPES.get(str(chart_type).lower())
        if xl_type is None:
            raise ValueError(f"unknown chart_type '{chart_type}'. Allowed: {', '.join(_CHART_TYPES)}")
        data = CategoryChartData()
        data.categories = [str(c) for c in categories]
        for s in series:
            data.add_series(str(s.get("name", "Series")), [float(v) for v in s["values"]])
        frame = self.slide.shapes.add_chart(xl_type, Inches(left), Inches(top), Inches(width), Inches(height), data)
        chart = frame.chart
        brand = self.deck.brand
        palette = [brand[k] for k in ("red", "blue", "orange", "light_blue", "green", "yellow", "navy", "caption")]
        chart.font.name = self.deck.body_font
        chart.font.size = Pt(11)
        chart.font.color.rgb = to_rgb("body", brand)
        chart.has_title = bool(title)
        if title:
            chart.chart_title.text_frame.text = title
            run = chart.chart_title.text_frame.paragraphs[0].runs[0]
            run.font.size, run.font.bold, run.font.color.rgb = Pt(14), True, to_rgb("navy", brand)
        is_pie = xl_type in (XL_CHART_TYPE.PIE, XL_CHART_TYPE.DOUGHNUT)
        legend = show_legend if show_legend is not None else (len(series) > 1 or is_pie)
        chart.has_legend = legend
        if legend:
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            chart.legend.font.size = Pt(10)
        plot = chart.plots[0]
        if is_pie:
            for i, point in enumerate(plot.series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = RGBColor.from_string(palette[i % len(palette)])
        else:
            for i, s in enumerate(plot.series):
                s.format.fill.solid()
                s.format.fill.fore_color.rgb = RGBColor.from_string(palette[i % len(palette)])
                if xl_type == XL_CHART_TYPE.LINE_MARKERS:
                    s.format.line.color.rgb = RGBColor.from_string(palette[i % len(palette)])
                    s.format.line.width = Pt(2.25)
                    s.smooth = False
            try:
                chart.value_axis.has_major_gridlines = True
                chart.value_axis.major_gridlines.format.line.color.rgb = to_rgb("border", brand)
                chart.value_axis.format.line.fill.background()
                chart.value_axis.tick_labels.font.size = Pt(10)
                chart.category_axis.tick_labels.font.size = Pt(10)
                chart.category_axis.format.line.color.rgb = to_rgb("border", brand)
            except Exception:
                pass
        if data_labels:
            plot.has_data_labels = True
            plot.data_labels.font.size = Pt(10)
            if number_format:
                plot.data_labels.number_format = number_format
                plot.data_labels.number_format_is_linked = False
        return frame

    # ---- finalisation --------------------------------------------------------------
    def finalize(self) -> None:
        """Remove placeholders that were never filled so no 'Click to edit' prompts remain."""
        if not self.deck.remove_unused_placeholders:
            return
        for idx, shape in self._placeholder_shapes().items():
            if idx in self.used_idx:
                continue
            if shape.placeholder_format.type in (PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE):
                continue
            shape._element.getparent().remove(shape._element)

    def apply_spec(self, spec: SlideSpec) -> None:
        """Render a structured SlideSpec onto this slide (placeholders first, then extras)."""
        if spec.title:
            self.set_title(spec.title)
        if spec.subtitle:
            self.set_subtitle(spec.subtitle)
        for pc in spec.placeholders:
            kind = pc.kind()
            if kind == "chart":
                self.set_chart(pc.target, pc.chart.chart_type, pc.chart.categories, pc.chart.series, pc.chart.title,
                               pc.chart.show_legend, pc.chart.data_labels, pc.chart.number_format)
            elif kind == "table":
                self.set_table(pc.target, pc.table.columns, pc.table.rows, pc.table.font_size, pc.table.col_widths)
            elif kind == "image":
                self.set_picture(pc.target, pc.image)
            elif kind == "bullets":
                self.set_text(pc.target, pc.bullets, size=pc.size, bold=pc.bold, color=pc.color, align=pc.align)
            else:
                self.set_text(pc.target, pc.text, size=pc.size, bold=pc.bold, color=pc.color, align=pc.align)
        for extra in spec.extras:
            self.apply_extra(extra)
        if spec.notes:
            self.set_notes(spec.notes)

    def apply_extra(self, extra: ExtraShape) -> None:
        p, geo = extra.props, (extra.left, extra.top, extra.width, extra.height)
        kind = extra.type
        if kind == "textbox":
            self.add_textbox(p.get("text", ""), *geo, size=float(p.get("size", 12)), bold=bool(p.get("bold", False)),
                             color=p.get("color", "body"), align=p.get("align", "left"), fill=p.get("fill"),
                             italic=bool(p.get("italic", False)))
        elif kind == "card":
            self.add_card(p.get("title", ""), p.get("body", ""), *geo, accent=p.get("accent", "red"),
                          title_size=float(p.get("title_size", 14)), body_size=float(p.get("body_size", 11)),
                          fill=p.get("fill", "white"), title_color=p.get("title_color", "navy"))
        elif kind == "shape":
            self.add_shape(p.get("shape", "rectangle"), *geo, fill=p.get("fill", "red"), line=p.get("line"),
                           text=p.get("text", ""), size=float(p.get("size", 12)), color=p.get("color", "white"),
                           bold=bool(p.get("bold", True)), align=p.get("align", "center"))
        elif kind == "stat":
            self.add_stat(str(p.get("value", "")), p.get("label", ""), *geo, color=p.get("color", "red"),
                          value_size=float(p.get("value_size", 32)), label_size=float(p.get("label_size", 11)),
                          fill=p.get("fill"), align=p.get("align", "left"))
        elif kind == "icon_circle":
            self.add_icon_circle(str(p.get("text", "")), extra.left, extra.top, extra.width, fill=p.get("fill", "red"),
                                 color=p.get("color", "white"), size=p.get("size"))
        elif kind == "image":
            self.add_image(p.get("path", ""), *geo)
        elif kind == "table":
            spec = TableSpec.from_dict(p)
            self.add_table(spec.columns, spec.rows, *geo, font_size=spec.font_size, col_widths=spec.col_widths)
        elif kind == "chart":
            spec = ChartSpec.from_dict(p)
            self.add_chart(spec.chart_type, spec.categories, spec.series, *geo, title=spec.title,
                           show_legend=spec.show_legend, data_labels=spec.data_labels, number_format=spec.number_format)
        elif kind == "line":
            self.add_line(*geo, color=p.get("color", "border"), weight=float(p.get("weight", 1.0)))


def _set_cell_borders(cell, bottom: Optional[str] = None, others_hidden: bool = True) -> None:
    """Hide left/right/top borders and optionally draw a thin bottom border (schema-ordered insertion)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        for existing in tc_pr.findall(qn(tag)):
            tc_pr.remove(existing)
    elements = []
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        ln = etree.Element(qn(tag))
        if tag == "a:lnB" and bottom:
            ln.set("w", str(int(0.75 * 12700)))
            solid = etree.SubElement(ln, qn("a:solidFill"))
            etree.SubElement(solid, qn("a:srgbClr")).set("val", bottom)
        elif others_hidden or tag != "a:lnB":
            ln.set("w", "0")
            etree.SubElement(ln, qn("a:noFill"))
        elements.append(ln)
    for i, ln in enumerate(elements):
        tc_pr.insert(i, ln)


class DeckBuilder:
    """Opens the template, removes its sample slides and adds new slides from the template layouts."""

    def __init__(self, template_path: Union[str, Path] = TEMPLATE_PATH, catalog: Optional[TemplateCatalog] = None,
                 remove_unused_placeholders: bool = True, verbose: bool = True):
        self.template_path = Path(template_path)
        self.prs = Presentation(str(self.template_path))
        self.catalog = catalog or TemplateCatalog.from_presentation(self.prs, self.template_path)
        self.brand = brand_palette(self.catalog.theme)
        self.heading_font = self.catalog.theme.major_font or "Barlow"
        self.body_font = self.catalog.theme.minor_font or "Barlow"
        self.remove_unused_placeholders = remove_unused_placeholders
        self.verbose = verbose
        self.slide_width = self.catalog.slide_width
        self.slide_height = self.catalog.slide_height
        self.slides: List[SlideHandle] = []
        self._clear_slides()

    def _clear_slides(self) -> None:
        sld_id_lst = self.prs.slides._sldIdLst
        for sld_id in list(sld_id_lst):
            r_id = sld_id.rId
            sld_id_lst.remove(sld_id)
            self.prs.part.drop_rel(r_id)

    def _layout_object(self, info: LayoutInfo):
        return self.prs.slide_masters[info.master_index].slide_layouts[info.layout_index]

    def add_slide(self, layout: Union[str, LayoutInfo]) -> SlideHandle:
        """Add a slide using a catalog key ('M1-L36') or layout name."""
        info = layout if isinstance(layout, LayoutInfo) else self.catalog.resolve_layout(layout)
        slide = self.prs.slides.add_slide(self._layout_object(info))
        handle = SlideHandle(self, slide, info)
        self.slides.append(handle)
        if self.verbose:
            log("deck", f"slide {len(self.slides)}: layout {info.key} '{info.name}'")
        return handle

    def render_plan(self, plan: PresentationPlan) -> None:
        """Deterministically render a structured plan."""
        for spec in plan.slides:
            handle = self.add_slide(spec.layout)
            handle.apply_spec(spec)

    def save(self, path: Union[str, Path] = OUTPUT_PPTX_PATH) -> Path:
        """Finalise every slide (drop unused placeholders) and save the deck."""
        for handle in self.slides:
            handle.finalize()
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(out))
        if self.verbose:
            log("deck", f"saved {len(self.slides)} slide(s) -> {out}")
        return out

    @property
    def warnings(self) -> List[str]:
        return [w for handle in self.slides for w in handle.warnings]


# ======================================================================================
# 4. Code generation, controlled execution and output validation
# ======================================================================================

DECKBUILDER_API_REFERENCE = """
DeckBuilder API (import from {filename}; all positions/sizes are inches on a {W} x {H} in slide)
  from {filename} import DeckBuilder, TEMPLATE_PATH, OUTPUT_PPTX_PATH
  deck = DeckBuilder(TEMPLATE_PATH)              # opens the template, drops its sample slides
  s = deck.add_slide("M1-L36")                   # catalog key or exact layout name
  s.set_title(text) / s.set_subtitle(text)       # fall back to a text box if the layout lacks the slot
  s.set_text(target, text_or_list, size=None, bold=None, color=None, align=None)
  s.set_bullets(target, ["item", {"text": "sub", "level": 1}], size=None)   # **bold** markup allowed
  s.set_picture(target, "path.png")              # picture placeholder -> cropped fill; else fitted at bounds
  s.set_table(target, columns, rows, font_size=11, col_widths=None)
  s.set_chart(target, "column|bar|line|pie|doughnut|stacked_column|area", categories, [{"name": .., "values": [..]}], title=None)
  s.placeholder_bounds(target) -> (left, top, width, height) or None ; s.remove_placeholder(target)
  s.add_textbox(text, left, top, width, height, size=12, bold=False, color="body", align="left", fill=None)
  s.add_card(title, body_or_bullets, left, top, width, height, accent="red")
  s.add_shape("rounded_rectangle|rectangle|oval|chevron|right_arrow|pentagon|diamond|hexagon", left, top, width, height,
              fill="red", line=None, text="", size=12, color="white", bold=True)
  s.add_stat(value, label, left, top, width, height, color="red", fill=None)
  s.add_icon_circle("01", left, top, diameter=0.8, fill="red", color="white")
  s.add_image(path, left, top, width=None, height=None) ; s.add_line(left, top, width, height, color="border")
  s.add_table(columns, rows, left, top, width, height) ; s.add_chart(chart_type, categories, series, left, top, width, height)
  s.set_notes(text) ; s.slide -> raw python-pptx slide for anything else
  deck.save(OUTPUT_PPTX_PATH)                    # removes unused placeholders, writes the file
  target = placeholder idx (int) or slot name ("body1", "heading2", "picture3", "content1", "number1", "caption1")
  colours = brand names (red, red_dark, navy, blue, light_blue, orange, yellow, green, body, caption, border, surface,
            white, black, red_light, gray, dark_gray) or hex "#RRGGBB"
""".strip()


def compile_plan_to_code(plan: PresentationPlan, catalog: TemplateCatalog) -> str:
    """Deterministically compile the plan into a runnable python-pptx script based on DeckBuilder."""
    lines = [
        '"""Generated by main.py - builds the presentation from the accepted plan."""',
        "import sys",
        "from pathlib import Path",
        "",
        f"sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `{filename}` importable",
        f"from {filename} import DeckBuilder, TEMPLATE_PATH, OUTPUT_PPTX_PATH",
        "",
        "deck = DeckBuilder(TEMPLATE_PATH)",
        "",
    ]
    for i, spec in enumerate(plan.slides, start=1):
        try:
            layout = catalog.resolve_layout(spec.layout)
            layout_name = layout.name
            layout_ref = layout.key
        except LookupError:
            layout_name, layout_ref = spec.layout, spec.layout
        var = f"s{i}"
        label = re.sub(r"\s+", " ", spec.title or spec.intent).strip()
        lines.append(f"# --- Slide {i}: {label} ({layout_ref} '{layout_name}')")
        lines.append(f"{var} = deck.add_slide({layout_ref!r})")
        if spec.title:
            lines.append(f"{var}.set_title({spec.title!r})")
        if spec.subtitle:
            lines.append(f"{var}.set_subtitle({spec.subtitle!r})")
        for pc in spec.placeholders:
            kind = pc.kind()
            if kind == "chart":
                c = pc.chart
                lines.append(f"{var}.set_chart({pc.target!r}, {c.chart_type!r}, {c.categories!r}, {c.series!r}, title={c.title!r}, "
                             f"show_legend={c.show_legend!r}, data_labels={c.data_labels!r}, number_format={c.number_format!r})")
            elif kind == "table":
                t = pc.table
                lines.append(f"{var}.set_table({pc.target!r}, {t.columns!r}, {t.rows!r}, font_size={t.font_size!r}, col_widths={t.col_widths!r})")
            elif kind == "image":
                lines.append(f"{var}.set_picture({pc.target!r}, {pc.image!r})")
            else:
                opts = "".join(f", {k}={v!r}" for k, v in (("size", pc.size), ("bold", pc.bold), ("color", pc.color), ("align", pc.align)) if v is not None)
                if kind == "bullets":
                    lines.append(f"{var}.set_bullets({pc.target!r}, {pc.bullets!r}{opts})")
                else:
                    lines.append(f"{var}.set_text({pc.target!r}, {pc.text!r}{opts})")
        for extra in spec.extras:
            p, geo = extra.props, f"{extra.left!r}, {extra.top!r}, {extra.width!r}, {extra.height!r}"
            kind = extra.type
            if kind == "textbox":
                lines.append(f"{var}.add_textbox({p.get('text', '')!r}, {geo}, size={float(p.get('size', 12))!r}, bold={bool(p.get('bold', False))!r}, "
                             f"color={p.get('color', 'body')!r}, align={p.get('align', 'left')!r}, fill={p.get('fill')!r})")
            elif kind == "card":
                lines.append(f"{var}.add_card({p.get('title', '')!r}, {p.get('body', '')!r}, {geo}, accent={p.get('accent', 'red')!r})")
            elif kind == "shape":
                lines.append(f"{var}.add_shape({p.get('shape', 'rectangle')!r}, {geo}, fill={p.get('fill', 'red')!r}, line={p.get('line')!r}, "
                             f"text={p.get('text', '')!r}, size={float(p.get('size', 12))!r}, color={p.get('color', 'white')!r}, bold={bool(p.get('bold', True))!r})")
            elif kind == "stat":
                lines.append(f"{var}.add_stat({str(p.get('value', ''))!r}, {p.get('label', '')!r}, {geo}, color={p.get('color', 'red')!r}, fill={p.get('fill')!r})")
            elif kind == "icon_circle":
                lines.append(f"{var}.add_icon_circle({str(p.get('text', ''))!r}, {extra.left!r}, {extra.top!r}, {extra.width!r}, "
                             f"fill={p.get('fill', 'red')!r}, color={p.get('color', 'white')!r})")
            elif kind == "image":
                lines.append(f"{var}.add_image({p.get('path', '')!r}, {geo})")
            elif kind == "table":
                t = TableSpec.from_dict(p)
                lines.append(f"{var}.add_table({t.columns!r}, {t.rows!r}, {geo}, font_size={t.font_size!r}, col_widths={t.col_widths!r})")
            elif kind == "chart":
                c = ChartSpec.from_dict(p)
                lines.append(f"{var}.add_chart({c.chart_type!r}, {c.categories!r}, {c.series!r}, {geo}, title={c.title!r}, "
                             f"show_legend={c.show_legend!r}, data_labels={c.data_labels!r})")
            elif kind == "line":
                lines.append(f"{var}.add_line({geo}, color={p.get('color', 'border')!r}, weight={float(p.get('weight', 1.0))!r})")
        if spec.notes:
            lines.append(f"{var}.set_notes({spec.notes!r})")
        lines.append("")
    lines.append("deck.save(OUTPUT_PPTX_PATH)")
    lines.append('print("Saved", OUTPUT_PPTX_PATH)')
    return "\n".join(lines) + "\n"


@dataclass
class ExecutionResult:
    ok: bool
    returncode: Optional[int]
    stdout: str
    stderr: str
    duration: float
    output_written: bool
    message: str

    def summary(self) -> str:
        head = "SUCCESS" if self.ok else "FAILED"
        parts = [f"{head}: {self.message} (exit={self.returncode}, {self.duration:.1f}s)"]
        if self.stdout.strip():
            parts.append("--- stdout (tail) ---\n" + self.stdout.strip()[-3000:])
        if self.stderr.strip():
            parts.append("--- stderr (tail) ---\n" + self.stderr.strip()[-4000:])
        return "\n".join(parts)


_FORBIDDEN_MODULES = {"subprocess", "socket", "shutil", "urllib", "requests", "http", "ftplib", "ctypes", "importlib", "multiprocessing"}
_FORBIDDEN_CALLS = {"os.system", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.popen", "eval", "exec", "__import__", "os.execv", "os.spawn"}


def _static_code_check(code: str) -> Optional[str]:
    """Syntax check plus a small deny-list so generated code stays within building a deck."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg} (line {exc.lineno})"
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if name.split(".")[0] in _FORBIDDEN_MODULES:
                    problems.append(f"import of '{name}' is not allowed")
        elif isinstance(node, ast.Call):
            func = node.func
            dotted = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                dotted = f"{func.value.id}.{func.attr}"
            elif isinstance(func, ast.Name):
                dotted = func.id
            if dotted and any(dotted == f or dotted.startswith(f) and f.endswith("spawn") for f in _FORBIDDEN_CALLS):
                problems.append(f"call to '{dotted}' is not allowed")
    if f"from {filename} import" not in code and f"import {filename}" not in code:
        problems.append(f"the script must use the DeckBuilder API: `from {filename} import DeckBuilder, TEMPLATE_PATH, OUTPUT_PPTX_PATH`")
    return "; ".join(problems) if problems else None


def strip_code_fences(code: str) -> str:
    text = code.strip()
    match = re.match(r"^```(?:python|py)?\s*\n(.*?)\n```$", text, re.DOTALL)
    return match.group(1) if match else text


def run_code_in_subprocess(code: str, script_path: Path, output_path: Path, timeout: int = CODE_TIMEOUT_SECONDS) -> ExecutionResult:
    """Write the script and execute it in a separate interpreter with a timeout, capturing all output."""
    code = strip_code_fences(code)
    problem = _static_code_check(code)
    if problem:
        return ExecutionResult(False, None, "", "", 0.0, False, f"static check failed: {problem}")
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(code, encoding="utf-8")
    before = output_path.stat().st_mtime if output_path.exists() else None
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BASE_DIR) + (";" if sys.platform.startswith("win") else ":") + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", str(script_path)], cwd=str(BASE_DIR), env=env,
                              capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(False, None, (exc.stdout or "") if isinstance(exc.stdout, str) else "", "", time.time() - started,
                               False, f"timed out after {timeout}s")
    duration = time.time() - started
    written = output_path.exists() and (before is None or output_path.stat().st_mtime >= before)
    if proc.returncode != 0:
        return ExecutionResult(False, proc.returncode, proc.stdout, proc.stderr, duration, written, "script raised an error")
    if not written:
        return ExecutionResult(False, proc.returncode, proc.stdout, proc.stderr, duration, False,
                               f"script finished but did not write {output_path.name} (call deck.save(OUTPUT_PPTX_PATH))")
    return ExecutionResult(True, proc.returncode, proc.stdout, proc.stderr, duration, True, f"wrote {output_path.name}")


@dataclass
class ValidationReport:
    ok: bool
    errors: List[str]
    warnings: List[str]
    stats: Dict[str, Any]

    def to_text(self) -> str:
        return json.dumps({"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "stats": self.stats}, indent=2)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u2022", " ")).strip().lower()


def validate_presentation(pptx_path: Path, plan: Optional[PresentationPlan], catalog: TemplateCatalog) -> ValidationReport:
    """Reopen the produced file and check integrity, slide count, layouts and required content."""
    errors: List[str] = []
    warnings: List[str] = []
    stats: Dict[str, Any] = {"file": str(pptx_path)}
    if not pptx_path.exists() or pptx_path.stat().st_size == 0:
        return ValidationReport(False, [f"{pptx_path.name} does not exist or is empty"], [], stats)
    stats["size_kb"] = round(pptx_path.stat().st_size / 1024)
    try:
        with zipfile.ZipFile(pptx_path) as zf:
            bad = zf.testzip()
            if bad:
                errors.append(f"corrupt zip member: {bad}")
            if "[Content_Types].xml" not in zf.namelist():
                errors.append("missing [Content_Types].xml - not a valid Office package")
    except zipfile.BadZipFile:
        return ValidationReport(False, ["file is not a valid zip/pptx package"], [], stats)
    try:
        prs = Presentation(str(pptx_path))
    except Exception as exc:
        return ValidationReport(False, [f"python-pptx cannot open the file: {exc}"], [], stats)
    stats["slides"] = len(prs.slides)
    if (_inches(prs.slide_width), _inches(prs.slide_height)) != (catalog.slide_width, catalog.slide_height):
        errors.append("slide size differs from the template")
    if len(prs.slides) == 0:
        errors.append("the deck has no slides")
    masters = list(prs.slide_masters)
    layouts_used: List[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        loc = _locate_layout(masters, slide.slide_layout)
        key = f"M{loc[0]}-L{loc[1]}" if loc else "?"
        layouts_used.append(f"{key} {slide.slide_layout.name}")
        texts, pictures, tables, charts = [], 0, 0, 0
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
                if shape.is_placeholder and _PROMPT_RE.search(shape.text_frame.text or ""):
                    errors.append(f"slide {i}: placeholder '{shape.name}' still shows template prompt text")
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or (shape.is_placeholder and shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE
                                                               and shape._element.tag.endswith("}pic")):
                pictures += 1
            if getattr(shape, "has_table", False) and shape.has_table:
                tables += 1
                for row in shape.table.rows:
                    for cell in row.cells:
                        texts.append(cell.text)
            if getattr(shape, "has_chart", False) and shape.has_chart:
                charts += 1
        joined = _norm_text(" ".join(texts))
        if plan is None or i > len(plan.slides):
            continue
        spec = plan.slides[i - 1]
        try:
            expected = catalog.resolve_layout(spec.layout)
            if loc != (expected.master_index, expected.layout_index):
                errors.append(f"slide {i}: uses layout {key} '{slide.slide_layout.name}' but the plan asked for {expected.key} '{expected.name}'")
        except LookupError:
            warnings.append(f"slide {i}: planned layout '{spec.layout}' is not in the catalog")
        required: List[Tuple[str, str]] = []
        if spec.title:
            required.append(("title", spec.title))
        if spec.subtitle:
            required.append(("subtitle", spec.subtitle))
        planned_images = planned_tables = planned_charts = 0
        for pc in spec.placeholders:
            kind = pc.kind()
            if kind == "text":
                required.append((f"placeholder {pc.target}", pc.text or ""))
            elif kind == "bullets":
                first = pc.bullets[0]
                required.append((f"placeholder {pc.target}", first.get("text", "") if isinstance(first, dict) else str(first)))
            elif kind == "image":
                planned_images += 1 if Path(str(pc.image)).exists() else 0
            elif kind == "table":
                planned_tables += 1
            elif kind == "chart":
                planned_charts += 1
        for e in spec.extras:
            if e.type in ("textbox", "stat"):
                required.append((f"extra {e.type}", str(e.props.get("text") or e.props.get("value") or "")))
            elif e.type == "card":
                required.append(("card", str(e.props.get("title", ""))))
            elif e.type == "table":
                planned_tables += 1
            elif e.type == "chart":
                planned_charts += 1
            elif e.type == "image" and Path(str(e.props.get("path", ""))).exists():
                planned_images += 1
        for label, text in required:
            probe = _norm_text(re.sub(r"\*\*", "", text))[:40]
            if probe and probe not in joined:
                errors.append(f"slide {i}: expected {label} text '{text[:50]}' not found")
        if pictures < planned_images:
            errors.append(f"slide {i}: {planned_images} image(s) planned but {pictures} found")
        if tables < planned_tables:
            errors.append(f"slide {i}: {planned_tables} table(s) planned but {tables} found")
        if charts < planned_charts:
            errors.append(f"slide {i}: {planned_charts} chart(s) planned but {charts} found")
    stats["layouts"] = layouts_used
    if plan is not None and len(prs.slides) != len(plan.slides):
        errors.append(f"deck has {len(prs.slides)} slides but the plan has {len(plan.slides)}")
    return ValidationReport(not errors, errors, warnings, stats)


# ======================================================================================
# 5. LLM gateway client (all calls go through apim_llm_service)
# ======================================================================================

@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    raw_content: Any = None
    raw_style: Optional[str] = None


@dataclass
class LLMReply:
    text: str
    tool_calls: List[ToolCall]
    raw_content: Any
    stop_reason: Optional[str]
    usage: Dict[str, int]
    api_style: str

    def as_message(self) -> ChatMessage:
        return ChatMessage("assistant", self.text, list(self.tool_calls), raw_content=self.raw_content, raw_style=self.api_style)


def _parse_json_loose(text: str) -> Any:
    """Parse JSON from text that may contain code fences or surrounding prose."""
    if isinstance(text, (dict, list)):
        return text
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)


class ApimClient:
    """Chat + tool calling over the APIM AI gateway, using apim_llm_service for auth, headers and POSTs."""

    def __init__(self, model: str = CHAT_MODEL, temperature: float = TEMPERATURE, max_tokens: int = MAX_TOKENS,
                 tracking: Optional[Dict[str, str]] = None, token_provider: Optional[Callable[[], str]] = None,
                 api_style: str = API_STYLE, verbose: bool = True):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tracking = dict(tracking or LLM_TRACKING)
        self._token_provider = token_provider or get_apim_token
        self._token: Optional[str] = None
        self.api_style = api_style if api_style in ("anthropic", "openai") else "anthropic"
        self.system_mode = "message"  # system prompt as a role=system message (as before); "top_level" if the route insists
        self.tools_supported: Optional[bool] = None
        self.verbose = verbose
        self.calls = 0
        self.total_usage = {"prompt": 0, "completion": 0, "total": 0}

    # ---- transport -----------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        if not self._token:
            self._token = self._token_provider()
        return apim_llm_service._headers(token=self._token, tracking=self.tracking)

    def _endpoint(self) -> str:
        return f"{apim_llm_service._base_url()}/foundry/models/chat/completions"

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, LLM_HTTP_RETRIES + 1):
            try:
                return apim_llm_service._post(url=self._endpoint(), body=body, headers=self._headers())
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 401 and attempt == 1:
                    log("llm", "token rejected (401) - refreshing token and retrying")
                    self._token = None
                    continue
                if status in (429, 500, 502, 503, 504) and attempt < LLM_HTTP_RETRIES:
                    wait = 3 * attempt
                    log("llm", f"gateway returned {status}; retrying in {wait}s")
                    time.sleep(wait)
                    last_exc = exc
                    continue
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < LLM_HTTP_RETRIES:
                    log("llm", f"network error ({exc.__class__.__name__}); retrying in {3 * attempt}s")
                    time.sleep(3 * attempt)
                    continue
                raise
        raise RuntimeError(f"LLM request failed after {LLM_HTTP_RETRIES} attempts: {last_exc}")

    # ---- request building ----------------------------------------------------------
    @staticmethod
    def _format_tool(tool: Dict[str, Any], style: str) -> Dict[str, Any]:
        params = tool.get("parameters") or {"type": "object", "properties": {}}
        if style == "anthropic":
            return {"name": tool["name"], "description": tool.get("description", ""), "input_schema": params}
        return {"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": params}}

    @staticmethod
    def _format_messages(messages: Sequence[ChatMessage], style: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for m in messages:
            if m.role in ("system", "user"):
                out.append({"role": m.role, "content": m.content})
            elif m.role == "assistant":
                if style == "anthropic":
                    if m.raw_content and m.raw_style == "anthropic":
                        raw = [b for b in m.raw_content if not (isinstance(b, dict) and b.get("type") == "text" and not str(b.get("text", "")).strip())]
                        if raw:
                            out.append({"role": "assistant", "content": raw})
                            continue
                    blocks: List[Dict[str, Any]] = []
                    if m.content.strip():
                        blocks.append({"type": "text", "text": m.content})
                    for tc in m.tool_calls:
                        blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                    out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(continuing)"}]})
                else:
                    msg: Dict[str, Any] = {"role": "assistant", "content": m.content or None}
                    if m.tool_calls:
                        msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                              "function": {"name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)}}
                                             for tc in m.tool_calls]
                    elif msg["content"] is None:
                        msg["content"] = ""
                    out.append(msg)
            elif m.role == "tool":
                if style == "anthropic":
                    block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                    if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                        out[-1]["content"].append(block)
                    else:
                        out.append({"role": "user", "content": [block]})
                else:
                    out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        return out

    def _build_body(self, messages: Sequence[ChatMessage], tools: Optional[Sequence[Dict[str, Any]]], style: str,
                    temperature_override: Optional[float], max_tokens_override: Optional[int]) -> Dict[str, Any]:
        formatted = self._format_messages(messages, style)
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens_override or self.max_tokens,
            "temperature": self.temperature if temperature_override is None else temperature_override,
            "messages": formatted,
        }
        if self.system_mode == "top_level":
            body["system"] = "\n\n".join(m["content"] for m in formatted if m["role"] == "system")
            body["messages"] = [m for m in formatted if m["role"] != "system"]
        if tools:
            body["tools"] = [self._format_tool(t, style) for t in tools]
        return body

    # ---- response parsing ----------------------------------------------------------
    @staticmethod
    def _parse(response: Dict[str, Any], requested_style: str) -> LLMReply:
        usage = apim_llm_service.usage_of(response)
        if isinstance(response.get("choices"), list) and response["choices"]:
            msg = response["choices"][0].get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            calls: List[ToolCall] = []
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    args = _parse_json_loose(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {"_invalid_json": raw_args}
                calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name", ""), args if isinstance(args, dict) else {"value": args}))
            return LLMReply(content or "", calls, None, response["choices"][0].get("finish_reason"), usage, "openai")
        blocks = response.get("content")
        if isinstance(blocks, list):
            text, calls = [], []
            for i, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    args = block.get("input") or {}
                    calls.append(ToolCall(block.get("id") or f"toolu_{i}", block.get("name", ""), args if isinstance(args, dict) else {"value": args}))
            return LLMReply("".join(text), calls, blocks, response.get("stop_reason"), usage, "anthropic")
        message = ((response.get("output") or {}).get("message")) or (((response.get("output") or {}).get("output") or {}).get("message"))
        if isinstance(message, dict):
            text, calls = [], []
            for i, block in enumerate(message.get("content") or []):
                if "text" in block:
                    text.append(block["text"])
                if "toolUse" in block:
                    tu = block["toolUse"]
                    calls.append(ToolCall(tu.get("toolUseId") or f"tool_{i}", tu.get("name", ""), tu.get("input") or {}))
            return LLMReply("".join(text), calls, None, response.get("stopReason"), usage, requested_style)
        raise RuntimeError(f"unrecognised gateway response shape: keys={list(response.keys())[:8]}")

    # ---- public API ----------------------------------------------------------------
    def complete(self, messages: Sequence[ChatMessage], tools: Optional[Sequence[Dict[str, Any]]] = None,
                 temperature_override: Optional[float] = None, max_tokens_override: Optional[int] = None) -> LLMReply:
        """Send the conversation (and tool definitions) to the gateway; returns text + parsed tool calls."""
        tools = list(tools or [])
        if tools and self.tools_supported is False:
            tools = []
        queue = [self.api_style] + ([s for s in ("anthropic", "openai") if s != self.api_style] if tools else [])
        attempts = 0
        while queue and attempts < 4:
            attempts += 1
            style = queue.pop(0)
            body = self._build_body(messages, tools, style, temperature_override, max_tokens_override)
            sent_keys = set(body)
            started = time.time()
            try:
                response = self._post(body)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                detail = (exc.response.text[:400] if exc.response is not None else str(exc)).replace("\n", " ")
                lowered = detail.lower()
                role_problem = "role" in lowered and ("system" in lowered or "'user' or 'assistant'" in lowered or "messages.0" in lowered)
                if status == 400 and self.system_mode == "message" and role_problem:
                    log("llm", "gateway wants the system prompt as a top-level parameter - switching and retrying")
                    self.system_mode = "top_level"
                    queue.insert(0, style)
                    continue
                if status == 400 and tools and queue:
                    log("llm", f"gateway rejected the {style}-style tool request (400: {detail}); trying the other format")
                    continue
                raise RuntimeError(f"LLM gateway error {status}: {detail}") from exc
            self.calls += 1
            reply = self._parse(response, style)
            for k in ("prompt", "completion", "total"):
                self.total_usage[k] += int(reply.usage.get(k) or 0)
            if self.verbose:
                log("llm", f"call {self.calls} ({style} format): {reply.usage.get('prompt', 0)} prompt / "
                           f"{reply.usage.get('completion', 0)} completion tokens, {time.time() - started:.1f}s, "
                           f"{len(reply.tool_calls)} tool call(s)")
            rejected = sent_keys - set(body)  # apim_llm_service._post drops parameters the gateway refused
            if tools and "tools" in rejected:
                log("llm", f"gateway dropped the 'tools' parameter for the {style} format")
                if queue:
                    continue
                self.tools_supported = False
                log("llm", "tool calling is unavailable on this route - continuing in text-only mode")
                reply.tool_calls = []
                return reply
            if tools:
                self.tools_supported = True
            if reply.api_style in ("anthropic", "openai") and reply.api_style != self.api_style:
                log("llm", f"gateway answers in {reply.api_style} format; using it for the next requests")
                self.api_style = reply.api_style
            elif style != self.api_style:
                self.api_style = style
            return reply
        raise RuntimeError("LLM request could not be completed with any supported request format")


# ======================================================================================
# 6. Tools (shared by the LangChain agent and the native agent)
# ======================================================================================

@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable[..., str]
    parameters: Dict[str, Any]

    def invoke(self, args: Dict[str, Any]) -> str:
        allowed = set(self.parameters.get("properties", {}))
        clean = {k: v for k, v in (args or {}).items() if k in allowed}
        missing = [k for k in self.parameters.get("required", []) if k not in clean]
        if missing:
            return f"ERROR: missing required argument(s) {missing} for {self.name}"
        try:
            return str(self.func(**clean))
        except Exception as exc:  # pragma: no cover - defensive
            return f"ERROR in {self.name}: {exc}"

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


_TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _json_schema_for(func: Callable[..., Any]) -> Dict[str, Any]:
    """JSON schema for a function's parameters from its type hints and 'Args:' docstring section."""
    sig = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = getattr(func, "__annotations__", {})
    doc_args: Dict[str, str] = {}
    in_args = False
    for line in (inspect.getdoc(func) or "").splitlines():
        if line.strip().lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            m = re.match(r"^\s*(\w+)\s*[:\-]\s*(.+)$", line)
            if m:
                doc_args[m.group(1)] = m.group(2).strip()
            elif not line.strip():
                in_args = False
    props: Dict[str, Any] = {}
    required: List[str] = []
    for name, param in sig.parameters.items():
        hint = hints.get(name, str)
        origin = getattr(hint, "__origin__", None)
        if hint is Any or (origin is Union and any(a is dict or getattr(a, "__origin__", None) is dict for a in hint.__args__)):
            schema: Dict[str, Any] = {}
        elif origin is Union:
            inner = [a for a in hint.__args__ if a is not type(None)]
            schema = {"type": _TYPE_MAP.get(inner[0], "string")} if inner else {"type": "string"}
        elif origin in (list, List):
            schema = {"type": "array", "items": {"type": _TYPE_MAP.get(getattr(hint, "__args__", [str])[0], "string")}}
        else:
            schema = {"type": _TYPE_MAP.get(hint, "string")}
        if name in doc_args:
            schema["description"] = doc_args[name]
        props[name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def _safe_tool(func: Callable[..., str]) -> Callable[..., str]:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            result = f"ERROR in {func.__name__}: {exc.__class__.__name__}: {exc}"
        text = str(result)
        if len(text) > TOOL_RESULT_MAX_CHARS:
            text = text[:TOOL_RESULT_MAX_CHARS] + f"\n... [truncated {len(text) - TOOL_RESULT_MAX_CHARS} chars]"
        return text
    return wrapper


def make_tool(func: Callable[..., str]) -> ToolSpec:
    wrapped = _safe_tool(func)
    description = (inspect.getdoc(func) or func.__name__).strip()
    return ToolSpec(func.__name__, description, wrapped, _json_schema_for(func))


@dataclass
class PipelineState:
    """Mutable state shared by the tools of one pipeline run."""

    config: PipelineConfig
    catalog: TemplateCatalog
    plan: Optional[PresentationPlan] = None
    plan_accepted: bool = False
    plan_submissions: int = 0
    generated_code: Optional[str] = None
    code_attempts: int = 0
    last_execution: Optional[ExecutionResult] = None
    validation: Optional[ValidationReport] = None


def planning_tools(state: PipelineState) -> List[ToolSpec]:
    catalog = state.catalog

    def get_template_summary() -> str:
        """Template inspection: slide size, theme fonts/colours, masters and every unique layout with its slot summary."""
        return catalog.summary_text()

    def search_layouts(query: str = "", tags: str = "", min_text_slots: int = 0, needs_picture: bool = False, limit: int = 20) -> str:
        """Catalog retrieval: find layouts by keywords and filters; returns compact rows (key, name, slots, tags).

        Args:
            query: keywords matched against layout name, tags and recommended use (e.g. "three columns", "timeline")
            tags: comma separated tags that must all be present (cover, agenda, section, quote, closing, content, columns, 2-column, 3-column, process, dashboard, photo, picture-slots, title-only, dark, blank)
            min_text_slots: minimum number of heading/body/content placeholders
            needs_picture: only layouts that have picture placeholders
            limit: maximum rows to return
        """
        rows = catalog.search(query, tags, int(min_text_slots or 0), bool(needs_picture), int(limit or 20))
        if not rows:
            return "No layouts match. Relax the filters or call get_template_summary for the full list."
        return "\n".join(l.compact() + f" | use: {l.recommended_use}" for l in rows)

    def get_layout_details(layout: str) -> str:
        """Full details of one layout: placeholders in reading order (idx, slot, role, geometry, font size, capacity), fixed shapes, background, sample usage.

        Args:
            layout: catalog key such as "M1-L36" or the exact layout name
        """
        info = catalog.resolve_layout(layout)
        return json.dumps(info.details(), indent=1, ensure_ascii=False)

    def recommend_layouts(intent: str, text_blocks: int = 0, needs_picture: bool = False, has_table: bool = False,
                          has_chart: bool = False, limit: int = 5) -> str:
        """Layout selection: rank layouts for a slide intent and its content shape, with reasons.

        Args:
            intent: cover, agenda, section, quote, closing, content, bullets, comparison, columns, process, timeline, dashboard, kpi, photo, screenshot, team, table, chart, blank
            text_blocks: number of separate text blocks / columns the slide needs (0 = unknown)
            needs_picture: the slide must show an image file
            has_table: the slide contains a table
            has_chart: the slide contains a chart
            limit: number of candidates to return
        """
        ranked = catalog.recommend(intent, int(text_blocks or 0), bool(needs_picture), bool(has_table), bool(has_chart), int(limit or 5))
        lines = [f"{l.compact()}\n   score={score} | {'; '.join(reasons[:5])} | use: {l.recommended_use}" for l, score, reasons in ranked]
        return "\n".join(lines) if lines else "No candidates found."

    def submit_presentation_plan(plan_json: Any) -> str:
        """Submit the complete presentation plan (JSON object or JSON string following the PLAN SCHEMA). Returns ACCEPTED, or the errors/warnings to fix before resubmitting.

        Args:
            plan_json: the full plan - {"title": ..., "slides": [...]} - as a JSON object or JSON string
        """
        state.plan_submissions += 1
        try:
            data = _parse_json_loose(plan_json)
            plan = PresentationPlan.from_dict(data)
        except (json.JSONDecodeError, PlanError) as exc:
            return f"REJECTED: the plan could not be parsed - {exc}. Follow the PLAN JSON SCHEMA exactly."
        validation = validate_plan(plan, catalog)
        log("plan", f"submission {state.plan_submissions}: {len(plan.slides)} slide(s), "
                    f"{len(validation.errors)} error(s), {len(validation.fixable)} fixable, {len(validation.warnings)} note(s)")
        for msg in validation.errors:
            log("plan", f"  error: {msg}")
        for msg in validation.fixable + validation.warnings:
            log("plan", f"  note: {msg}")
        if validation.errors:
            return "REJECTED - fix these errors and resubmit:\n- " + "\n- ".join(validation.errors) + (
                "\nAlso consider:\n- " + "\n- ".join(validation.fixable + validation.warnings) if (validation.fixable or validation.warnings) else "")
        if validation.fixable and state.plan_submissions == 1:
            state.plan = plan  # keep it as a fallback in case the revision never arrives
            return "REVISE - the plan is valid but these issues should be fixed once, then resubmit the full plan:\n- " + "\n- ".join(validation.fixable) + (
                "\nNotes:\n- " + "\n- ".join(validation.warnings) if validation.warnings else "")
        state.plan = plan
        state.plan_accepted = True
        notes = validation.fixable + validation.warnings
        return f"ACCEPTED: {len(plan.slides)} slide(s) planned." + ("\nNotes:\n- " + "\n- ".join(notes) if notes else "")

    return [make_tool(f) for f in (get_template_summary, search_layouts, get_layout_details, recommend_layouts, submit_presentation_plan)]


def build_tools(state: PipelineState) -> List[ToolSpec]:
    catalog, config = state.catalog, state.config

    def get_layout_details(layout: str) -> str:
        """Full details of one layout: placeholders (idx, slot, role, geometry, capacity), fixed shapes and background.

        Args:
            layout: catalog key such as "M1-L36" or the exact layout name
        """
        return json.dumps(catalog.resolve_layout(layout).details(), indent=1, ensure_ascii=False)

    def get_accepted_plan() -> str:
        """The accepted presentation plan as JSON."""
        return state.plan.to_json() if state.plan else "No plan accepted yet."

    def generate_presentation_code() -> str:
        """Presentation generation: compile the accepted plan into a complete, runnable python-pptx script (DeckBuilder API). Use it as the baseline to enhance and run."""
        if not state.plan:
            return "ERROR: no accepted plan to compile."
        code = compile_plan_to_code(state.plan, catalog)
        state.generated_code = code
        log("codegen", f"compiled baseline script ({len(code.splitlines())} lines)")
        return code

    def run_presentation_code(code: str) -> str:
        """Code execution: save the COMPLETE python script and run it in a separate process with a timeout; returns stdout/stderr and whether the .pptx was written.

        Args:
            code: the full python script (must import DeckBuilder from main_v3 and call deck.save(OUTPUT_PPTX_PATH))
        """
        if state.code_attempts >= config.max_code_attempts:
            return f"LIMIT REACHED: {config.max_code_attempts} executions already used. Do not call this again; reply with a short summary."
        state.code_attempts += 1
        log("exec", f"attempt {state.code_attempts}/{config.max_code_attempts}: running generated code ({len(code.splitlines())} lines)")
        result = run_code_in_subprocess(code, config.generated_code_path, config.output_pptx_path, config.code_timeout_seconds)
        state.generated_code = strip_code_fences(code)
        state.last_execution = result
        state.validation = None
        log("exec", ("OK - " if result.ok else "FAILED - ") + result.message)
        if not result.ok and result.stderr.strip():
            log("exec", "stderr tail: " + result.stderr.strip().splitlines()[-1][:200])
        return result.summary()

    def validate_presentation() -> str:
        """Output validation: reopen the generated .pptx and check integrity, slide count, layouts, required content and leftover prompt text."""
        report = validate_presentation_file(state)
        return report.to_text()

    return [make_tool(f) for f in (get_layout_details, get_accepted_plan, generate_presentation_code, run_presentation_code, validate_presentation)]


def validate_presentation_file(state: PipelineState) -> ValidationReport:
    report = validate_presentation(state.config.output_pptx_path, state.plan, state.catalog)
    state.validation = report
    log("validate", ("PASS" if report.ok else "FAIL") + f" - {report.stats.get('slides', 0)} slide(s), "
                    f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    for msg in report.errors:
        log("validate", f"  error: {msg}")
    return report


# ======================================================================================
# 7. Agents: LangChain tool-calling agent (preferred) and a native fallback loop
# ======================================================================================

@dataclass
class AgentResult:
    final_text: str
    steps: int
    tool_calls: int
    used_tools: bool


def _log_tool_call(name: str, args: Dict[str, Any]) -> None:
    preview = json.dumps(args, ensure_ascii=False)
    if len(preview) > 160:
        preview = preview[:160] + "..."
    log("agent", f"tool -> {name}({preview})")


class NativeToolAgent:
    """Minimal tool-calling loop on top of ApimClient (used when LangChain is not installed)."""

    def __init__(self, client: ApimClient, tools: Sequence[ToolSpec], max_steps: int, label: str = "agent"):
        self.client = client
        self.tools = {t.name: t for t in tools}
        self.max_steps = max_steps
        self.label = label

    def run(self, system_prompt: str, user_message: str, stop_when: Callable[[], bool] = lambda: False,
            temperature_override: Optional[float] = None) -> AgentResult:
        history = [ChatMessage("system", system_prompt), ChatMessage("user", user_message)]
        tool_defs = [t.as_dict() for t in self.tools.values()]
        calls_made, final_text, step = 0, "", 0
        for step in range(1, self.max_steps + 1):
            log(self.label, f"step {step}/{self.max_steps}: asking the model")
            reply = self.client.complete(history, tool_defs, temperature_override=temperature_override)
            history.append(reply.as_message())
            final_text = reply.text or final_text
            if not reply.tool_calls:
                if reply.text.strip():
                    log(self.label, "model reply: " + reply.text.strip().splitlines()[0][:160])
                break
            for tc in reply.tool_calls:
                _log_tool_call(tc.name, tc.args)
                spec = self.tools.get(tc.name)
                result = spec.invoke(tc.args) if spec else f"ERROR: unknown tool '{tc.name}'. Available: {', '.join(self.tools)}"
                calls_made += 1
                history.append(ChatMessage("tool", result, tool_call_id=tc.id))
            if stop_when():
                log(self.label, "goal reached - stopping the agent loop")
                break
        return AgentResult(final_text, step, calls_made, calls_made > 0)


if LANGCHAIN_AVAILABLE:

    class ApimChatModel(BaseChatModel):
        """LangChain chat model that delegates to ApimClient (and therefore to apim_llm_service)."""

        client: Any = None
        temperature_override: Optional[float] = None

        @property
        def _llm_type(self) -> str:
            return "apim-gateway"

        def bind_tools(self, tools: Sequence[Any], **kwargs: Any):
            return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

        def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, run_manager: Any = None,
                      **kwargs: Any) -> ChatResult:
            tools = kwargs.get("tools")
            specs = []
            for t in tools or []:
                fn = t.get("function", t)
                specs.append({"name": fn["name"], "description": fn.get("description", ""), "parameters": fn.get("parameters")})
            reply = self.client.complete([_lc_to_neutral(m) for m in messages], specs, temperature_override=self.temperature_override)
            ai = AIMessage(
                content=reply.text,
                tool_calls=[{"name": tc.name, "args": tc.args, "id": tc.id, "type": "tool_call"} for tc in reply.tool_calls],
                additional_kwargs={"raw_content": reply.raw_content, "raw_style": reply.api_style},
            )
            return ChatResult(generations=[ChatGeneration(message=ai)])

    def _lc_to_neutral(message: BaseMessage) -> ChatMessage:
        content = message.content
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        if isinstance(message, SystemMessage):
            return ChatMessage("system", content)
        if isinstance(message, AIMessage):
            calls = [ToolCall(tc.get("id") or f"call_{i}", tc["name"], tc.get("args") or {}) for i, tc in enumerate(message.tool_calls or [])]
            return ChatMessage("assistant", content or "", calls, raw_content=message.additional_kwargs.get("raw_content"),
                               raw_style=message.additional_kwargs.get("raw_style"))
        if isinstance(message, ToolMessage):
            return ChatMessage("tool", content, tool_call_id=message.tool_call_id)
        return ChatMessage("user", content)

    def to_langchain_tools(specs: Sequence[ToolSpec]) -> List[Any]:
        """Expose the tool functions as LangChain StructuredTools (schema inferred from type hints)."""
        return [StructuredTool.from_function(func=spec.func, name=spec.name, description=spec.description) for spec in specs]

    class LangChainToolAgent:
        """LangChain tool-calling agent: model.bind_tools + StructuredTool execution + ToolMessage feedback loop."""

        def __init__(self, client: ApimClient, tools: Sequence[ToolSpec], max_steps: int, label: str = "agent"):
            self.model = ApimChatModel(client=client)
            self.lc_tools = to_langchain_tools(tools)
            self.tools_by_name = {t.name: t for t in self.lc_tools}
            self.max_steps = max_steps
            self.label = label

        def run(self, system_prompt: str, user_message: str, stop_when: Callable[[], bool] = lambda: False,
                temperature_override: Optional[float] = None) -> AgentResult:
            self.model.temperature_override = temperature_override
            bound = self.model.bind_tools(self.lc_tools)
            messages: List[BaseMessage] = [SystemMessage(content=system_prompt), HumanMessage(content=user_message)]
            calls_made, final_text, step = 0, "", 0
            for step in range(1, self.max_steps + 1):
                log(self.label, f"step {step}/{self.max_steps}: asking the model (LangChain)")
                ai: AIMessage = bound.invoke(messages)
                messages.append(ai)
                text = ai.content if isinstance(ai.content, str) else "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in ai.content)
                final_text = text or final_text
                if not ai.tool_calls:
                    if text.strip():
                        log(self.label, "model reply: " + text.strip().splitlines()[0][:160])
                    break
                for tc in ai.tool_calls:
                    _log_tool_call(tc["name"], tc.get("args") or {})
                    tool = self.tools_by_name.get(tc["name"])
                    try:
                        result = tool.invoke(tc.get("args") or {}) if tool else f"ERROR: unknown tool '{tc['name']}'"
                    except Exception as exc:
                        result = f"ERROR: {exc}"
                    calls_made += 1
                    messages.append(ToolMessage(content=str(result), tool_call_id=tc.get("id") or f"call_{calls_made}"))
                if stop_when():
                    log(self.label, "goal reached - stopping the agent loop")
                    break
            return AgentResult(final_text, step, calls_made, calls_made > 0)


def make_agent(client: ApimClient, tools: Sequence[ToolSpec], max_steps: int, label: str, use_langchain: bool):
    if use_langchain and LANGCHAIN_AVAILABLE:
        return LangChainToolAgent(client, tools, max_steps, label)
    return NativeToolAgent(client, tools, max_steps, label)


# ======================================================================================
# 8. Pipeline orchestration
# ======================================================================================

PLAN_SCHEMA_DOC = """
{
  "title": "Deck title",
  "purpose": "one line on audience / goal (optional)",
  "slides": [
    {
      "slide_number": 1,
      "intent": "cover | agenda | section | content | comparison | process | timeline | dashboard | photo | quote | closing",
      "layout": "M1-L36",                      // catalog key from the tools (or exact layout name)
      "title": "Slide title",                  // goes into the title placeholder
      "subtitle": "optional one-liner",        // goes into the subtitle placeholder when the layout has one
      "placeholders": [                        // fill template placeholders FIRST; target = idx or slot name
        {"target": "heading1", "text": "Short heading"},
        {"target": "body1", "bullets": ["Point one", "Point two", {"text": "sub point", "level": 1}]},
        {"target": "picture1", "text": "01"},                       // picture slots marked (text ok) accept a short label
        {"target": "picture2", "image": "images/photo.png"},        // only files that exist on disk
        {"target": "content1", "table": {"columns": ["A", "B"], "rows": [["1", "2"]]}},
        {"target": "content1", "chart": {"chart_type": "column", "categories": ["Q1", "Q2"], "series": [{"name": "Sales", "values": [10, 12]}]}}
      ],
      "extras": [                              // optional additional shapes, positions in inches, all inside the slide
        {"type": "card", "title": "Key takeaway", "body": ["one", "two"], "left": 0.8, "top": 5.2, "width": 3.8, "height": 1.6, "accent": "red"},
        {"type": "icon_circle", "text": "1", "left": 1.0, "top": 2.0, "diameter": 0.8, "fill": "red"},
        {"type": "stat", "value": "99.9%", "label": "availability", "left": 9.5, "top": 5.0, "width": 3.0, "height": 1.4},
        {"type": "textbox", "text": "Footnote with **bold** part", "left": 1.1, "top": 6.7, "width": 11, "height": 0.4, "size": 11, "color": "caption"},
        {"type": "shape", "shape": "chevron", "text": "Step", "left": 1, "top": 3, "width": 2, "height": 0.8, "fill": "blue"},
        {"type": "table", "columns": ["A"], "rows": [["1"]], "left": 1, "top": 2, "width": 5, "height": 2},
        {"type": "chart", "chart_type": "pie", "categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}], "left": 7, "top": 2, "width": 5, "height": 4}
      ],
      "notes": "speaker notes (optional)"
    }
  ]
}
Colour names: red, red_dark, navy, blue, light_blue, orange, yellow, green, body, caption, border, surface, white, black, red_light.
""".strip()

PLANNING_INSTRUCTIONS = """
=== WORKFLOW: PLAN THE PRESENTATION ===
You plan a PowerPoint deck that is generated from the company template. The template's real layouts, placeholders and
formatting are exposed through tools - never assume a layout or placeholder exists without checking the catalog.

1. Read the user's request carefully. It may contain a draft plan (slide numbers, layout hints, slots like body1..body5,
   bullets, notes, image references) mixed with details that do not matter. Decide what is important: keep the facts,
   sharpen the wording, complete truncated sentences, and enrich the slide visually where it helps the message.
2. Shortlist layouts with recommend_layouts / search_layouts (the template summary is in the user message), then call
   get_layout_details for every layout you intend to use - it lists the placeholder idx / slot names, geometry and
   capacity you must respect.
3. Choose layouts by slide intent, content type, visual needs, placeholder availability and consistency: keep all
   content slides on one master (the preferred one), use a cover layout only for an opening slide, a closing layout
   only at the end. Follow the user's requested slide count; if unspecified, derive it from the content - do not invent
   unrelated slides.
4. Fill template placeholders FIRST (title, subtitle, headings, bodies, picture slots). Add "extras" only when they add
   clear value (key takeaway card, stat tile, icon circles, a table or chart for real data) and only in free space
   inside the slide. Never cover filled placeholders.
5. Respect capacity hints (approx_chars / approx_lines). Keep bullets short (<= ~90 characters), 3-5 per block, in
   sentence case. Reference only image files that exist; if none are available use short text labels in picture slots
   that accept text, or icon_circle extras.
6. Preserve the template: never ask for different fonts or colours than the brand palette, no decorative underlines.
7. Call submit_presentation_plan with the COMPLETE plan JSON. If it returns REJECTED or REVISE, fix the listed issues
   and resubmit the full plan. Stop as soon as it returns ACCEPTED.

=== PLAN JSON SCHEMA ===
""" + PLAN_SCHEMA_DOC

BUILD_INSTRUCTIONS = f"""
=== WORKFLOW: BUILD THE PRESENTATION ===
The plan in the user message is accepted. Produce the .pptx with python-pptx through the DeckBuilder API below.
1. Call generate_presentation_code to obtain a working baseline script compiled from the plan.
2. Optionally enhance it with brand-consistent visual polish using DeckBuilder helpers (cards, icon circles, stat tiles,
   tables, charts, thin divider lines). Keep every element inside the slide, do not overlap filled placeholders, do not
   change layouts or remove planned content. If unsure, run the baseline unchanged.
3. Call run_presentation_code with the COMPLETE script (never a fragment). Read the result; if it failed, fix the code
   and run again. The number of executions is limited - make each attempt count.
4. Call validate_presentation. If it reports errors, fix the script and rerun. Finish with a two-sentence summary.
Rules: keep the baseline's header (sys.path line + `from {filename} import DeckBuilder, TEMPLATE_PATH, OUTPUT_PPTX_PATH`),
keep `deck = DeckBuilder(TEMPLATE_PATH)`, add slides with deck.add_slide(<layout key>) in plan order and end with
deck.save(OUTPUT_PPTX_PATH). No network access, subprocesses, file deletion or other side effects.

=== DECKBUILDER API ===
"""


@dataclass
class PipelineResult:
    ok: bool
    output_path: Optional[Path]
    plan: Optional[PresentationPlan]
    validation: Optional[ValidationReport]
    route: str
    warnings: List[str] = field(default_factory=list)


def _system_prompt(config: PipelineConfig, user_message: str, extra: str) -> str:
    return create_prompt_payload(str(config.skill_md_path), user_message, extra)["messages"][0]["content"]


def plan_presentation(state: PipelineState, client: ApimClient, user_message: str) -> PresentationPlan:
    """Phase 1 - the planning agent selects layouts and submits a validated plan (with text-only fallbacks)."""
    config, catalog = state.config, state.catalog
    system_prompt = _system_prompt(config, user_message, PLANNING_INSTRUCTIONS)
    user_content = (
        f"{user_message.strip()}\n\n=== TEMPLATE SUMMARY (from the catalog) ===\n{catalog.summary_text()}\n\n"
        "Plan the deck now: inspect the candidate layouts with the tools, then call submit_presentation_plan."
    )
    agent = make_agent(client, planning_tools(state), config.max_plan_steps, "plan", config.use_langchain)
    result = agent.run(system_prompt, user_content, stop_when=lambda: state.plan_accepted)
    if state.plan_accepted and state.plan:
        return state.plan
    if state.plan and state.plan_submissions:  # a valid plan flagged REVISE but never resubmitted
        log("plan", "using the last valid submission")
        state.plan_accepted = True
        return state.plan

    # Fallback 1: the model may have answered with the plan JSON in plain text.
    for attempt_text in (result.final_text,):
        try:
            plan = PresentationPlan.from_dict(_parse_json_loose(attempt_text))
            validation = validate_plan(plan, catalog)
            if validation.ok:
                log("plan", "plan parsed from the model's text reply")
                state.plan, state.plan_accepted = plan, True
                return plan
            log("plan", f"text plan rejected: {validation.errors[:3]}")
        except (json.JSONDecodeError, PlanError):
            pass

    # Fallback 2: explicit JSON-only planning (works without tool calling).
    log("plan", "falling back to JSON-only planning without tools")
    details = "\n".join(json.dumps(l.details(), ensure_ascii=False) for l, _, _ in catalog.recommend("content", limit=6))
    details += "\n" + "\n".join(json.dumps(l.details(), ensure_ascii=False) for l, _, _ in catalog.recommend("cover", limit=2))
    prompt = (f"{user_content}\n\n=== LAYOUT DETAILS ===\n{details}\n\n"
              "Reply with ONLY the plan JSON (no prose, no code fences).")
    errors: List[str] = []
    for round_no in range(1, 4):
        messages = [ChatMessage("system", system_prompt), ChatMessage("user", prompt if not errors else prompt + "\n\nFix these problems:\n- " + "\n- ".join(errors))]
        reply = client.complete(messages, tools=None)
        try:
            plan = PresentationPlan.from_dict(_parse_json_loose(reply.text))
        except (json.JSONDecodeError, PlanError) as exc:
            errors = [f"plan could not be parsed: {exc}"]
            log("plan", f"round {round_no}: {errors[0]}")
            continue
        validation = validate_plan(plan, catalog)
        if validation.ok:
            state.plan, state.plan_accepted = plan, True
            return plan
        errors = validation.errors
        log("plan", f"round {round_no}: {len(errors)} error(s)")
    raise RuntimeError("The model did not produce a valid presentation plan. Last errors: " + "; ".join(errors[:5]))


def build_presentation(state: PipelineState, client: ApimClient) -> bool:
    """Phase 2 - the build agent generates code, executes it with retries and validates the deck."""
    config = state.config
    if client.tools_supported is False:
        log("build", "tool calling unavailable - skipping the build agent")
        return False
    api_ref = DECKBUILDER_API_REFERENCE.replace("{W}", str(state.catalog.slide_width)).replace("{H}", str(state.catalog.slide_height)).replace("{filename}", filename)
    system_prompt = _system_prompt(config, "", BUILD_INSTRUCTIONS + api_ref)
    user_content = ("=== ACCEPTED PLAN ===\n" + state.plan.to_json() +
                    "\n\nStart by calling generate_presentation_code, then run and validate the script.")
    agent = make_agent(client, build_tools(state), config.max_build_steps, "build", config.use_langchain)
    result = agent.run(system_prompt, user_content, stop_when=lambda: bool(state.validation and state.validation.ok),
                       temperature_override=min(config.temperature, 0.3))
    if state.validation is None and state.last_execution and state.last_execution.ok:
        validate_presentation_file(state)
    if state.validation is None and not state.last_execution and result.final_text:
        # The model answered with code in plain text instead of calling the tool.
        match = re.search(r"```(?:python|py)?\s*\n(.*?)```", result.final_text, re.DOTALL)
        if match:
            log("build", "running the script found in the model's text reply")
            state.code_attempts += 1
            state.last_execution = run_code_in_subprocess(match.group(1), config.generated_code_path, config.output_pptx_path, config.code_timeout_seconds)
            if state.last_execution.ok:
                validate_presentation_file(state)
    return bool(state.validation and state.validation.ok)


def deterministic_fallback(state: PipelineState) -> bool:
    """Phase 3 - compile the plan ourselves, execute it in a subprocess and validate; render in-process if needed."""
    config = state.config
    code = compile_plan_to_code(state.plan, state.catalog)
    state.generated_code = code
    log("fallback", "executing the compiled baseline script")
    state.last_execution = run_code_in_subprocess(code, config.generated_code_path, config.output_pptx_path, config.code_timeout_seconds)
    log("fallback", ("OK - " if state.last_execution.ok else "FAILED - ") + state.last_execution.message)
    if not state.last_execution.ok:
        log("fallback", "rendering the plan in-process")
        deck = DeckBuilder(config.template_path, state.catalog)
        deck.render_plan(state.plan)
        deck.save(config.output_pptx_path)
    return validate_presentation_file(state).ok


def run_pipeline(user_message: str = USER_MESSAGE, config: Optional[PipelineConfig] = None,
                 client: Optional[ApimClient] = None) -> PipelineResult:
    """End-to-end: catalog -> plan agent -> build agent -> validation (-> deterministic fallback)."""
    config = config or PipelineConfig()
    print("=" * 78)
    print(" Template-driven presentation generator")
    print("=" * 78)
    log("setup", f"template: {config.template_path.name} | skill: {config.skill_md_path.name} | model: {config.model}")
    log("setup", f"agent backend: {'LangChain' if (config.use_langchain and LANGCHAIN_AVAILABLE) else 'native tool loop (LangChain not installed)'}")

    log("template", "inspecting the template and building the layout catalog ...")
    catalog = build_catalog(config.template_path)
    st = catalog.stats()
    log("template", f"{st['masters']} masters, {st['layouts']} layouts ({st['unique_layouts']} unique), "
                    f"{st['sample_slides']} sample slides, {st['slide_size_in'][0]}x{st['slide_size_in'][1]} in, fonts {st['fonts']}")
    log("template", f"preferred content master: M{catalog.preferred_master} '{catalog.themes[catalog.preferred_master].name}'")

    state = PipelineState(config=config, catalog=catalog)
    client = client or ApimClient(config.model, config.temperature, config.max_tokens, config.tracking,
                                  config.token_provider, config.api_style)
    config.output_pptx_path.parent.mkdir(parents=True, exist_ok=True)

    log("plan", "phase 1/3: planning the deck with the layout catalog ...")
    plan = plan_presentation(state, client, user_message)
    config.plan_json_path.write_text(plan.to_json(), encoding="utf-8")
    log("plan", f"plan accepted: '{plan.title}' with {len(plan.slides)} slide(s) -> {config.plan_json_path.name}")
    for s in plan.slides:
        log("plan", f"  slide {s.slide_number}: {s.layout} | {s.title or s.intent}")

    log("build", "phase 2/3: generating, executing and validating the python-pptx code ...")
    route = "agent-generated code"
    ok = False
    try:
        ok = build_presentation(state, client)
    except Exception as exc:
        log("build", f"build agent failed: {exc.__class__.__name__}: {exc}")
    if not ok:
        log("fallback", "phase 3/3: the agent did not deliver a valid deck - rendering the plan deterministically")
        route = "deterministic render"
        ok = deterministic_fallback(state)
    else:
        log("build", "phase 3/3: validation passed - no fallback needed")

    warnings = list(state.validation.warnings) if state.validation else []
    print("-" * 78)
    log("done", f"result: {'SUCCESS' if ok else 'FAILED'} via {route}")
    log("done", f"deck: {config.output_pptx_path}")
    log("done", f"code: {config.generated_code_path} | plan: {config.plan_json_path}")
    if state.validation:
        for line in state.validation.stats.get("layouts", []):
            log("done", f"  layout used: {line}")
        for msg in state.validation.errors:
            log("done", f"  validation error: {msg}")
    log("done", f"LLM calls: {client.calls}, tokens: {client.total_usage['prompt']} prompt / {client.total_usage['completion']} completion")
    return PipelineResult(ok, config.output_pptx_path if ok else None, plan, state.validation, route, warnings)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        result = run_pipeline(USER_MESSAGE)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        print("-" * 78)
        log("error", f"{exc.__class__.__name__}: {exc}")
        traceback.print_exc()
        return 1
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
