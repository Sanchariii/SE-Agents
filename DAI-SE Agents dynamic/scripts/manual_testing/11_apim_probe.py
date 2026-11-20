"""Probe the ROK AI Gateway (APIM) Foundry route for the request shapes the agents use.

Sends raw requests (bypassing the client's param shedding and retries) so each case
shows exactly what the gateway accepts: the server-side code-execution tool, a plain
fenced-code reply, and `temperature` on each Claude model in ModelConstants.

Run from the repository root:
    python scripts/manual_testing/11_apim_probe.py
    python scripts/manual_testing/11_apim_probe.py --models claude-opus-5-5 claude-sonnet-5-5
"""

import argparse
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config.constants import ModelConstants, PptCreatorConstants
from app.utilities import tls_util as tls
from app.utilities.apim_llm_utils import get_apim_llm_client

_PROMPT = ("Write a 3-line python program that prints the numbers 1 to 3. "
           "Reply with the program in one ```python fenced block and nothing else.")
_TIMEOUT_SECONDS = 120


def _parse_args() -> argparse.Namespace:
    """Read the models to probe.

    Returns:
        Parsed command-line arguments.
    """
    defaults = sorted({ModelConstants.PPT_CODER, ModelConstants.PLANNER})
    parser = argparse.ArgumentParser(description="Probe APIM Foundry request shapes.")
    parser.add_argument("--models", nargs="+", default=defaults,
                        help="Foundry models to probe (default: %(default)s).")
    return parser.parse_args()


def _send(label: str, body: dict) -> tuple[int, dict | str]:
    """POST one raw body to the Foundry chat route and print a one-line verdict.

    Args:
        label: Case name for the printout.
        body: The full request body.

    Returns:
        (HTTP status, parsed JSON body or raw text).
    """
    client = get_apim_llm_client()
    url = f"{client._base_url()}/foundry/models/chat/completions"
    started = time.monotonic()
    resp = requests.post(url, headers=client._headers(), json=body,
                         verify=tls.verify_setting(), timeout=_TIMEOUT_SECONDS)
    elapsed = int((time.monotonic() - started) * 1000)
    try:
        payload: dict | str = resp.json()
    except ValueError:
        payload = resp.text
    verdict = "OK  " if resp.status_code < 400 else "FAIL"
    detail = "" if resp.status_code < 400 else f" -> {str(payload)[:300]}"
    print(f"[{verdict}] {resp.status_code} {elapsed:>6}ms  {label}{detail}")
    return resp.status_code, payload


def _probe(model: str) -> dict[str, bool]:
    """Run every case against one model.

    Args:
        model: Foundry model name.

    Returns:
        Case name -> whether the gateway accepted it.
    """
    from app.genai.agents.ppt_creator_agent import _extract_code

    messages = [{"role": "user", "content": _PROMPT}]
    base = {"model": model, "messages": messages, "max_tokens": 512}
    results: dict[str, bool] = {}

    print(f"\n=== {model} ===")
    status, _ = _send("plain chat (no temperature, no tools)", base)
    results["plain"] = status < 400

    status, _ = _send("with temperature=0.2", {**base, "temperature": 0.2})
    results["temperature"] = status < 400

    tool = {"type": PptCreatorConstants.CODE_EXECUTION_TOOL_TYPE,
            "name": PptCreatorConstants.CODE_EXECUTION_TOOL_NAME}
    status, _ = _send(f"with server tool {tool['type']}", {**base, "tools": [tool]})
    results["code_execution_tool"] = status < 400

    # The shape tier 1 falls back to: no tool, the program comes back fenced.
    status, payload = _send("fenced-code reply parsed by _extract_code", base)
    ok = False
    if status < 400 and isinstance(payload, dict):
        try:
            source = _extract_code(payload)
            ok = "print" in source
            print(f"        extracted {len(source)} chars: {source.strip()[:80]!r}")
        except Exception as exc:                     # noqa: BLE001
            print(f"        extraction failed: {exc}")
    results["fenced_extract"] = ok
    return results


def main() -> int:
    """Probe every requested model and print a summary.

    Returns:
        Zero when every model accepts a plain call and yields extractable code.
    """
    args = _parse_args()
    summary = {model: _probe(model) for model in args.models}
    print("\n=== summary (True = accepted) ===")
    for model, results in summary.items():
        print(f"{model:<22} " + "  ".join(f"{k}={v}" for k, v in results.items()))
    healthy = all(r["plain"] and r["fenced_extract"] for r in summary.values())
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
