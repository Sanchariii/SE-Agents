import base64
import binascii
import io
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
import apim_llm_service
# from anthropic import Anthropic
from pathlib import Path
from typing import Dict

def get_apim_token() -> str:
    """Client-credentials token, refreshed well inside its 60-minute lifetime."""
    return apim_llm_service.get_token()


chat_model = "claude-opus-4-6"
temperature = 0.7
token = get_apim_token()
max_tokens = 16000

# Max number of times we'll try to run the generated code and, on failure, ask the LLM to fix it.
MAX_FIX_ATTEMPTS = 3

# Unique id for this run of main.py; the final .pptx is stored under tmp/<RUN_ID>/output.pptx.
RUN_ID = str(uuid.uuid4())
BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
RUN_OUTPUT_DIR = TMP_DIR / RUN_ID
FINAL_OUTPUT_PATH = RUN_OUTPUT_DIR / "output.pptx"


LLM_TRACKING = {
    "x-rok-user-id": "44305",
    "x-rok-user-email": "kumar.nishant@rockwellautomation.com",
    "x-rok-agent-id": "azure-service-monitor",
    "x-rok-agent-name": "Azure Service Monitor",
    "x-rok-agent-version": "1.0.0",
}

def cached_apim_models() -> list[dict]:
    return apim_llm_service.list_models(get_apim_token(), LLM_TRACKING)

headers = apim_llm_service._headers(token=token,tracking=LLM_TRACKING)


def create_prompt_payload(
    skill_md_path: str,
    user_message: str,
    additional_system_prompt: str = ""
) -> Dict:
    """
    Read SKILL.md and create a chat-completion payload.

    Args:
        skill_md_path: Path to SKILL.md
        user_message: User's message/query
        additional_system_prompt: Optional extra instructions

    Returns:
        Dict containing messages payload
    """

    skill_content = Path(skill_md_path).read_text(encoding="utf-8")

    system_prompt = f"""
You are an AI assistant following the instructions defined below.

=== SKILL INSTRUCTIONS ===
{skill_content}

"""

    if additional_system_prompt:
        system_prompt += f"\n\n=== ADDITIONAL INSTRUCTIONS ===\n{additional_system_prompt}"

    payload = {
        "messages": [
            {
                "role": "system",
                "content": system_prompt.strip()
            },
            {
                "role": "user",
                "content": user_message
            }
        ]
    }

    return payload

user_message = """
enrich the  deck plan to make better looking adding more things which are visually appling and create one slide deck
{
  "slide_number": 5,
  "slide_draft": {
    "slide_number": 5,
    "kind": "content",
    "layout": "5 columns with circle headings - color  - Icon or text placeholder",
    "title": "Security Best Practices & Key Takeaways",
    "blocks": [
      {
        "slot": "subtitle",
        "text": "Follow these practices on every integration with the ROK AI Gateway",
        "bullets": [],
        "image_ref": ""
      },
      {
        "slot": "body1",
        "text": "",
        "bullets": [
          "Never hard-code client secrets, subscription",
          "Store all credentials in a secure secret",
          "Never commit secrets to source control or"
        ],
        "image_ref": ""
      },
      {
        "slot": "body2",
        "text": "",
        "bullets": [
          "Rotate client secrets before expiration",
          "Prevents service disruption from expired",
          "Re-issue APIM subscription keys on schedule"
        ],
        "image_ref": ""
      },
      {
        "slot": "body3",
        "text": "",
        "bullets": [
          "Implement retry logic for HTTP 429 responses",
          "Handle transient failures gracefully",
          "Respect gateway quota and rate-limit"
        ],
        "image_ref": ""
      },
      {
        "slot": "body4",
        "text": "",
        "bullets": [
          "Log request IDs and response IDs on every",
          "Enables full traceability and debugging",
          "Required for incident investigation"
        ],
        "image_ref": ""
      },
      {
        "slot": "body5",
        "text": "",
        "bullets": [
          "Always target the correct environment",
          "Non-prod and prod scopes are separate",
          "Misconfigured endpoints risk data leakage"
        ],
        "image_ref": ""
      }
    ],
    "shapes": [],
    "tables": [],
    "icons": [],
    "notes": "The ROK AI Gateway Engineering Guide explicitly states: never hard-code secrets or tokens, rotate secrets before expiration, handle 429s and transient failures, and log request and response IDs for traceability. All concrete values in the guide are examples or placeholders — real client secrets and subscription keys must never appear in documentation or source control. Non-prod and prod environments use separate APIM scopes, so targeting the wrong endpoint is a governance and security risk. These five practices apply to every consumer of the gateway, regardless of application type.",
    "bullets": [],
    "cards": [],
    "steps": [],
    "stat_value": "",
    "stat_label": "",
    "image_path": ""
  },
"""

payload = create_prompt_payload(skill_md_path=r'scripts\feature_test\rockwell-skill.md', user_message= user_message)

body = {"model": chat_model,
        "max_tokens": max_tokens,
"container": {
    "skills": [{"type": "anthropic", "skill_id": "pptx", "version": "latest"}]
  },
  "messages": payload['messages'],
  "tools": [{"type": "code_execution_20260521", "name": "code_execution"}]
}

# print(f"{apim_llm_service._base_url()}/anthropic/v1/messages")

print(json.dumps(body, indent=2))

response = apim_llm_service._post(

    url=f"{apim_llm_service._base_url()}/foundry/models/chat/completions",
    body =body,
    headers = headers
)

print("-------------------------------------------------------")
print(json.dumps(response, indent=2))

def save_generated_code(response: dict, output_path: str = str(RUN_OUTPUT_DIR / "generated_make_pptx.py")) -> str:
    """Save Python source embedded in bash heredocs from code execution blocks."""

    for block in response.get("content", []):
        try:
            if block.get("type") == "server_tool_use" and block.get("name") == "text_editor_code_execution":
                Path(output_path).write_text(block["input"]['file_text'], encoding="utf-8")
                return output_path
        except Exception as e:
            raise RuntimeError("The response did not contain generated Python code in a bash heredoc")
    raise RuntimeError("The response did not contain generated Python code in a code execution block")


def normalize_generated_code(code_path: str) -> None:
    """Rewrite POSIX-only temp paths so the sandbox-oriented code runs on Windows.

    The LLM/skill container assumes a Linux sandbox and often hardcodes paths like
    ``/tmp/foo.pptx``, which are invalid on Windows and raise ``FileNotFoundError``.
    We redirect any ``/tmp/`` literal into the run's own output directory so the
    generated deck lands somewhere we control and can locate afterwards.

    Args:
        code_path: Path to the generated Python script to rewrite in place.

    Returns:
        None. The file at ``code_path`` is updated when a substitution is needed.
    """
    source = Path(code_path).read_text(encoding="utf-8")
    # Use forward slashes (valid on Windows in Python) to avoid backslash escapes.
    run_dir_posix = RUN_OUTPUT_DIR.as_posix()
    if "/tmp/" in source:
        source = source.replace("/tmp/", f"{run_dir_posix}/")
        Path(code_path).write_text(source, encoding="utf-8")
        print(f"Normalized generated code: redirected /tmp/ -> {run_dir_posix}/")


# Ensure the run output directory exists before generated code (or tempfile) writes into it.
RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

generated_code_path = save_generated_code(response)
normalize_generated_code(generated_code_path)
print(f"Saved generated code: {generated_code_path}")


def run_generated_code(code_path: str) -> tuple[bool, str, str]:
    """Execute the generated script and report whether it succeeded.

    Args:
        code_path: Path to the generated Python script to execute.

    Returns:
        Tuple of (success flag, captured stdout, captured stderr).
    """
    # Point tempfile at our run dir so code using tempfile.gettempdir() writes where we can find it.
    env = os.environ.copy()
    env["TMP"] = str(RUN_OUTPUT_DIR)
    env["TEMP"] = str(RUN_OUTPUT_DIR)
    env["TMPDIR"] = str(RUN_OUTPUT_DIR)
    result = subprocess.run(
        [sys.executable, code_path],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode == 0, result.stdout, result.stderr


def request_code_fix(broken_code: str, error_output: str) -> dict:
    """Ask the LLM to fix the generated code given the error it raised."""
    fix_messages = payload["messages"] + [
        {"role": "assistant", "content": broken_code},
        {
            "role": "user",
            "content": (
                "Running the generated code above failed. Fix the code so it runs "
                "successfully and still produces the requested .pptx file.\n\n"
                f"=== ERROR OUTPUT ===\n{error_output}"
            ),
        },
    ]
    fix_body = {
        "model": chat_model,
        "max_tokens": max_tokens,
        "container": {
            "skills": [{"type": "anthropic", "skill_id": "pptx", "version": "latest"}]
        },
        "messages": fix_messages,
        "tools": [{"type": "code_execution_20260521", "name": "code_execution"}],
    }
    return apim_llm_service._post(
        url=f"{apim_llm_service._base_url()}/foundry/models/chat/completions",
        body=fix_body,
        headers=headers,
    )


def find_latest_pptx(search_dir: Path, exclude: Path) -> Path | None:
    """Find the most recently modified .pptx file under a directory.

    Args:
        search_dir: Directory tree to search recursively.
        exclude: A path to ignore (e.g. the final copied output) to avoid re-finding it.

    Returns:
        The newest matching .pptx path, or None if none were found.
    """
    candidates = [
        p for p in search_dir.rglob("*.pptx")
        if p.resolve() != exclude.resolve()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ── Eval loop: run generated code, fix on error, retry up to MAX_FIX_ATTEMPTS times ──
success = False
stderr = ""
for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
    print(f"--- Running generated code (attempt {attempt}/{MAX_FIX_ATTEMPTS}) ---")
    success, stdout, stderr = run_generated_code(generated_code_path)
    if stdout:
        print(stdout)
    if success:
        print("Generated code ran successfully.")
        break

    print(f"Generated code failed:\n{stderr}")
    if attempt == MAX_FIX_ATTEMPTS:
        break

    print("Asking the LLM to fix the generated code...")
    fix_response = request_code_fix(Path(generated_code_path).read_text(encoding="utf-8"), stderr)
    generated_code_path = save_generated_code(fix_response, output_path=generated_code_path)
    # Re-apply Windows path normalization to the freshly regenerated code.
    normalize_generated_code(generated_code_path)

if success:
    # Generated code writes into RUN_OUTPUT_DIR (via /tmp redirect or the temp env vars).
    output_file = find_latest_pptx(RUN_OUTPUT_DIR, exclude=FINAL_OUTPUT_PATH)
    if output_file:
        shutil.copy(str(output_file), str(FINAL_OUTPUT_PATH))
        print(f"Final output saved to: {FINAL_OUTPUT_PATH}")
    else:
        print("Generated code succeeded but no .pptx output file was found.")
else:
    print(f"Generated code still failing after {MAX_FIX_ATTEMPTS} attempts.")

