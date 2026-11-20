"""Shared client for the manual scripts: auth, JSON, SSE and uploads.

    python scripts/manual_testing/00_smoke.py

Sign-in is federated, so these scripts cannot mint their own credentials. Supply
either an already-issued session handle (SE_SESSION_TOKEN) or the Entra tokens to
exchange (SE_GRAPH_ACCESS_TOKEN + SE_ID_TOKEN). Point at another environment with
SE_API_URL.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.environ.get("SE_API_URL", "http://127.0.0.1:8000")
API_PREFIX = "/api"                                   # global prefix for every backend route
SESSION_TOKEN = os.environ.get("SE_SESSION_TOKEN", "")
GRAPH_ACCESS_TOKEN = os.environ.get("SE_GRAPH_ACCESS_TOKEN", "")
ID_TOKEN = os.environ.get("SE_ID_TOKEN", "")
TIMEOUT = httpx.Timeout(600.0, connect=15.0)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = REPO_ROOT / "app" / "data" / "sample_docs"


class Colors:
    OK, FAIL, INFO, DIM, RESET = "\033[92m", "\033[91m", "\033[96m", "\033[90m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{Colors.OK}PASS{Colors.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{Colors.FAIL}FAIL{Colors.RESET} {msg}")


def info(msg: str) -> None:
    print(f"{Colors.INFO}····{Colors.RESET} {msg}")


def step(msg: str) -> None:
    print(f"\n{Colors.INFO}=== {msg} ==={Colors.RESET}")


def uniq(prefix: str) -> str:
    return f"{prefix}_{int(time.time()) % 1_000_000}"


def sample_docs() -> list[Path]:
    docs = sorted(SAMPLE_DOCS.glob("*.txt"))
    if not docs:
        sys.exit(f"no sample documents in {SAMPLE_DOCS}")
    return docs


class Api:
    """Thin wrapper that keeps the access token and reports failures loudly."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.refresh_token: str | None = None
        # httpx appends relative request paths onto the base URL's path, so the
        # global /api prefix is applied once here for every call (wrapper or raw).
        self._client = httpx.Client(base_url=f"{self.base_url}{API_PREFIX}", timeout=TIMEOUT)

    # --- plumbing ---
    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        kwargs["headers"] = self._headers(kwargs.pop("headers", None))
        return self._client.request(method, path, **kwargs)

    def get(self, path: str, **params) -> httpx.Response:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: dict | None = None, **params) -> httpx.Response:
        return self.request("POST", path, json=body or {}, params=params or None)

    def delete(self, path: str, **params) -> httpx.Response:
        return self.request("DELETE", path, params=params or None)

    # --- auth ---
    def login(self, access_token: str = GRAPH_ACCESS_TOKEN,
              id_token: str = ID_TOKEN) -> dict:
        """Open a session, either by reusing SE_SESSION_TOKEN or by exchanging tokens."""
        if not (access_token and id_token):
            if not SESSION_TOKEN:
                sys.exit("set SE_SESSION_TOKEN, or SE_GRAPH_ACCESS_TOKEN + SE_ID_TOKEN")
            self.token = SESSION_TOKEN
            return {"token": SESSION_TOKEN}
        response = self._client.post("/auth/login",
                                     json={"access_token": access_token,
                                           "id_token": id_token})
        if response.status_code != 200:
            sys.exit(f"sso login failed: {response.status_code} {response.text}")
        payload = response.json()
        self.token = payload["token"]
        self.refresh_token = payload["refresh_token"]
        return payload

    def logout(self) -> None:
        if self.token:
            self.request("POST", "/auth/logout")
        self.token = None
        self.refresh_token = None

    # --- deck helpers ---
    def create_session(self, title: str = "manual test") -> str:
        session_id = uniq("sess")
        response = self.post("/decks/sessions", {"session_id": session_id, "title": title})
        if response.status_code != 201:
            sys.exit(f"could not create session: {response.status_code} {response.text}")
        return session_id

    def upload(self, session_id: str, paths: list[Path]) -> httpx.Response:
        files = [("files", (p.name, p.read_bytes(), "text/plain")) for p in paths]
        return self.request("POST", f"/decks/{session_id}/documents", files=files)

    def invoke(self, session_id: str, message: str,
               on_event=None, quiet: bool = False) -> list[dict]:
        """POST /invoke and consume the SSE stream until the 'done' event."""
        events: list[dict] = []
        with self._client.stream("POST", f"/decks/{session_id}/invoke",
                                 json={"message": message},
                                 headers=self._headers()) as response:
            if response.status_code != 200:
                response.read()
                fail(f"invoke returned {response.status_code}: {response.text}")
                return events
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if on_event:
                    on_event(event)
                elif not quiet:
                    print(f"{Colors.DIM}  [{event.get('agent')}] "
                          f"{str(event.get('message'))[:140]}{Colors.RESET}")
                if event.get("type") == "done":
                    break
        return events

    @staticmethod
    def result_of(events: list[dict]) -> dict:
        for event in reversed(events):
            if event.get("type") == "done":
                return (event.get("data") or {}).get("result") or {}
        return {}

    def close(self) -> None:
        self._client.close()


def run(name: str, checks) -> int:
    """Run a list of (description, callable->bool) checks; return a process exit code."""
    print(f"{Colors.INFO}{name}{Colors.RESET}  →  {BASE_URL}")
    failures = 0
    for description, check in checks:
        try:
            if check():
                ok(description)
            else:
                fail(description)
                failures += 1
        except Exception as exc:                    # noqa: BLE001
            fail(f"{description} — {type(exc).__name__}: {exc}")
            failures += 1
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0
