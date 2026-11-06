"""PPTX Designer orchestration service.

Pipeline:
1. Read design guidance and build an enriched prompt from a SlideDraft/deck plan.
2. Ask the ROK AI Gateway model to generate a Python PPTX builder.
3. Extract and statically validate the generated Python source.
4. Ask the model to repair invalid source when necessary.
5. Execute the builder and optionally persist the resulting PPTX.

The module expects the internal ``apim_llm_service`` package to be available.
"""

from __future__ import annotations

import argparse
import json
import logging
import py_compile
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import apim_llm_service

LOGGER = logging.getLogger("pptx_designer")


DEFAULT_TRACKING = {
    "x-rok-user-id": "44305",
    "x-rok-user-email": "kumar.nishant@rockwellautomation.com",
    "x-rok-agent-id": "pptx-designer",
    "x-rok-agent-name": "PPTX Designer",
    "x-rok-agent-version": "1.0.0",
}


@dataclass(frozen=True)
class DesignerConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 16000
    endpoint: str = "/foundry/models/chat/completions"
    skill_id: str = "pptx"
    skill_version: str = "latest"
    max_repair_attempts: int = 2
    execution_timeout_seconds: int = 180


class PPTXDesignerError(RuntimeError):
    """Base error for PPTXDesigner failures."""


class GeneratedCodeError(PPTXDesignerError):
    """Raised when generated source cannot be extracted or validated."""


class PPTXDesigner:
    """Generate, validate, execute, and optionally persist a PPTX builder."""

    def __init__(
        self,skill_md_path,config,tracking_headers = None,uploader = None) -> None:

        self.skill_md_path = Path(skill_md_path)
        self.config = config or DesignerConfig()
        self.tracking = tracking_headers or DEFAULT_TRACKING.copy()
        self.uploader = uploader

        if not self.skill_md_path.is_file():
            raise FileNotFoundError(f"Skill file not found: {self.skill_md_path}")

    @staticmethod
    def get_apim_token() -> str:
        """Return a fresh client-credentials token."""
        return apim_llm_service.get_token()

    def cached_apim_models(self) -> list[dict]:
        return apim_llm_service.list_models(self.get_apim_token(), self.tracking)

    def create_prompt_payload(
        self,
        user_message: str,
        additional_system_prompt: str = "",
    ) -> Dict[str, Any]:
        skill_content = self.skill_md_path.read_text(encoding="utf-8")
        system_prompt = f"""
You are the Designer Layer for a production presentation-generation system.
Follow the skill instructions below and author
one polished PowerPoint deck from the supplied slide or deck plan.

=== SKILL INSTRUCTIONS ===
{skill_content}

=== DESIGNER MODE ===
- Analyze the requested layout and infer a clean geometric grid.
- Preserve source-grounded meaning while improving hierarchy and readability.
- Use brand-aware colors, typography, icons, shapes, and whitespace.
- Avoid clipped text, unintended overlap, and out-of-slide elements.
- Write a complete standalone Python builder.
- Save the final .pptx in the current working directory.
- Do not include credentials, access tokens, or subscription keys.
""".strip()

        if additional_system_prompt:
            system_prompt += (
                "\n\n=== ADDITIONAL INSTRUCTIONS ===\n"
                + additional_system_prompt.strip()
            )

        return {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message.strip()},
            ]
        }

    def _build_request_body(self, messages: list[dict[str, str]]) -> Dict[str, Any]:
        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "container": {
                "skills": [
                    {
                        "type": "anthropic",
                        "skill_id": self.config.skill_id,
                        "version": self.config.skill_version,
                    }
                ]
            },
            "messages": messages,
            "tools": [
                {"type": "code_execution_20260521", "name": "code_execution"}
            ],
        }

    def _call_model(self, messages: list[dict[str, str]]) -> Dict[str, Any]:
        token = self.get_apim_token()
        headers = apim_llm_service._headers(token=token, tracking=self.tracking)
        url = f"{apim_llm_service._base_url()}{self.config.endpoint}"
        return apim_llm_service._post(
            url=url,
            body=self._build_request_body(messages),
            headers=headers,
        )

    @staticmethod
    def _content_blocks(response: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        """Support direct Anthropic-style and wrapped chat-completion responses."""
        if isinstance(response.get("content"), list):
            yield from response["content"]

        for choice in response.get("choices", []) or []:
            message = choice.get("message", {})
            content = message.get("content")
            if isinstance(content, list):
                yield from content
            elif isinstance(content, str):
                yield {"type": "text", "text": content}

    @classmethod
    def extract_generated_code(cls, response: Dict[str, Any]) -> str:
        """Extract Python source from tool blocks, file payloads, or fenced text."""
        text_candidates: list[str] = []

        for block in cls._content_blocks(response):
            block_input = block.get("input", {}) or {}
            for key in ("file_text", "code", "content"):
                value = block_input.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

            if isinstance(block.get("text"), str):
                text_candidates.append(block["text"])

        combined = "\n".join(text_candidates)
        fenced = re.search(r"```(?:python|py)?\s*(.*?)```", combined, re.I | re.S)
        if fenced:
            return fenced.group(1).strip()

        heredoc = re.search(
            r"cat\s+>\s+[^\n]+\s+<<['\"]?(\w+)['\"]?\n(.*?)\n\1",
            combined,
            re.S,
        )
        if heredoc:
            return heredoc.group(2).strip()

        raise GeneratedCodeError("The model response did not contain Python source.")

    @staticmethod
    def save_generated_code(code: str, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code.rstrip() + "\n", encoding="utf-8")
        return path

    @staticmethod
    def validate_source(source_path: str | Path) -> None:
        path = Path(source_path)
        code = path.read_text(encoding="utf-8")
        compile(code, str(path), "exec")
        py_compile.compile(str(path), doraise=True)

        risky_patterns = {
            r"\b(os\.system|eval|exec)\s*\(": "dynamic or shell execution",
            r"subprocess\.(Popen|run|call)\s*\(\s*[^\n]*shell\s*=\s*True": "shell=True",
            r"(?:client_secret|subscription_key|access_token)\s*=\s*['\"][^'{<]": "hard-coded credential",
        }
        for pattern, description in risky_patterns.items():
            if re.search(pattern, code, re.I):
                raise GeneratedCodeError(
                    f"Generated source rejected because it contains {description}."
                )

    def _repair_code(self, code: str, error: Exception) -> str:
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Repair the supplied Python PPTX builder. Return the complete corrected "
                    "Python source in one code block only. Preserve the intended slide design, "
                    "write the PPTX to the current working directory, and never add secrets."
                ),
            },
            {
                "role": "user",
                "content": f"Validation error:\n{error}\n\nSource:\n```python\n{code}\n```",
            },
        ]
        return self.extract_generated_code(self._call_model(repair_messages))

    def generate_builder(
        self,
        user_message: str,
        output_path: str | Path = "generated_make_pptx.py",
        additional_system_prompt: str = "",
    ) -> Path:
        payload = self.create_prompt_payload(user_message, additional_system_prompt)
        response = self._call_model(payload["messages"])
        code = self.extract_generated_code(response)
        output = self.save_generated_code(code, output_path)

        for attempt in range(self.config.max_repair_attempts + 1):
            try:
                self.validate_source(output)
                return output
            except (SyntaxError, py_compile.PyCompileError, GeneratedCodeError) as exc:
                if attempt >= self.config.max_repair_attempts:
                    raise GeneratedCodeError(
                        f"Generated builder remained invalid after {attempt} repair attempts: {exc}"
                    ) from exc
                LOGGER.warning("Builder validation failed; requesting repair: %s", exc)
                code = self._repair_code(output.read_text(encoding="utf-8"), exc)
                self.save_generated_code(code, output)

        raise AssertionError("Unreachable validation state")

    def execute_builder(
        self,
        builder_path: str | Path,
        output_directory: str | Path,
    ) -> Path:
        builder = Path(builder_path).resolve()
        output_dir = Path(output_directory).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        before = {p.resolve() for p in output_dir.glob("*.pptx")}

        process = subprocess.run(
            [sys.executable, str(builder)],
            cwd=output_dir,
            capture_output=True,
            text=True,
            timeout=self.config.execution_timeout_seconds,
            check=False,
        )
        if process.returncode != 0:
            raise PPTXDesignerError(
                "Generated builder failed.\n"
                f"STDOUT:\n{process.stdout[-4000:]}\n"
                f"STDERR:\n{process.stderr[-4000:]}"
            )

        created = [p for p in output_dir.glob("*.pptx") if p.resolve() not in before]
        if not created:
            raise PPTXDesignerError("Builder completed but did not create a new .pptx file.")
        return max(created, key=lambda item: item.stat().st_mtime)

    def design(
        self,
        slide_or_deck_plan: Dict[str, Any] | str,
        output_directory: str | Path = ".",
        builder_name: str = "generated_make_pptx.py",
        additional_system_prompt: str = "",
    ) -> Dict[str, Optional[str]]:
        if isinstance(slide_or_deck_plan, str):
            user_message = slide_or_deck_plan
        else:
            user_message = (
                "Enrich this slide/deck plan and create one polished PowerPoint deck.\n"
                + json.dumps(slide_or_deck_plan, indent=2, ensure_ascii=False)
            )

        output_dir = Path(output_directory)
        output_dir.mkdir(parents=True, exist_ok=True)
        builder_path = self.generate_builder(
            user_message=user_message,
            output_path=output_dir / builder_name,
            additional_system_prompt=additional_system_prompt,
        )
        pptx_path = self.execute_builder(builder_path, output_dir)
        blob_url = self.uploader(pptx_path) if self.uploader else None

        return {
            "builder_path": str(builder_path.resolve()),
            "pptx_path": str(pptx_path.resolve()),
            "blob_url": blob_url,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a PPTX through the Designer Layer.")
    parser.add_argument("--skill", required=True, help="Path to rockwell-skill.md")
    parser.add_argument("--plan", required=True, help="Path to slide/deck plan JSON")
    parser.add_argument("--output-dir", default="designer_output")
    parser.add_argument("--builder-name", default="generated_make_pptx.py")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    designer = PPTXDesigner(args.skill)
    result = designer.design(
        slide_or_deck_plan=plan,
        output_directory=args.output_dir,
        builder_name=args.builder_name,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    # main()
    # from PPTXDesigner import PPTXDesigner
    slide_plan ="""
    {
  "slide_number": 5,
  "slide_draft": {
    "slide_number": 5,
    "kind": "content",
    "layout": "Dashboard-3boxes+Takeaway",
    "title": "Key Takeaways and Next Steps for the Team",
    "blocks": [
      {
        "slot": "subtitle",
        "text": "Mandatory standards for all consumers across every environment",
        "bullets": [],
        "image_ref": ""
      },
      {
        "slot": "heading1",
        "text": "Authentication and Access",
        "bullets": [],
        "image_ref": ""
      },
      {
        "slot": "body1",
        "text": "",
        "bullets": [
          "Gateway is the mandatory path — no direct model calls",
          "Every request needs a Bearer token (Entra ID) and an APIM subscription key",
          "Both headers are required on every call",
          "Use the single Foundry endpoint; set the model in the request body"
        ],
        "image_ref": ""
      },
      {
        "slot": "heading2",
        "text": "Security and Next Steps",
        "bullets": [],
        "image_ref": ""
      },
      {
        "slot": "body2",
        "text": "",
        "bullets": [
          "Never hard-code secrets, keys, or tokens in source control",
          "Rotate credentials proactively; handle 429 errors with retry logic",
          "Review the ROK AI Gateway Engineering Guide",
          "Validate non-prod and prod configs before go-live",
          "Align all integrations to gateway standards"
        ],
        "image_ref": ""
      },
      {
        "slot": "body3",
        "text": "",
        "bullets": [
          "Chat Completions: real-time use",
          "Responses API: traceable, multi-step workflows",
          "Requests API: batch and long-running executions",
          "All consumer types covered: apps, agents, services, tools"
        ],
        "image_ref": ""
      }
    ],
    "shapes": [],
    "tables": [],
    "icons": [],
    "notes": "The ROK AI Gateway is the single, governed ingress for all GenAI model access with no exceptions; every consumer type, including GenAI apps, agents, automated services, and internal tools, must route through it. Both authentication headers are mandatory because the Bearer token establishes client identity and authorization while the APIM subscription key enforces product-level quota and analytics. Pattern selection is operationally significant: Chat Completions handles synchronous real-time calls, Responses API supports auditable multi-step workflows, and Requests API is designed for batch or long-running executions where managed retries and durability are required. The Engineering Guide is the canonical integration reference and should be the first document teams consult when configuring or validating their environment.",
    "bullets": [],
    "cards": [],
    "steps": [],
    "stat_value": "",
    "stat_label": "",
    "image_path": ""
  }
    """
    designer = PPTXDesigner(
        config=None,
        skill_md_path="rockwell-skill.md"
    )

    result = designer.design(
        slide_or_deck_plan=slide_plan,
        output_directory="designer_output"
    )

    print(result["builder_path"])
    print(result["pptx_path"])
    print(result["blob_url"])