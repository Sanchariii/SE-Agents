"""Call chat + embedding models through the ROK AI Gateway (Azure APIM).

Every LLM call in this service goes through the gateway — there is no direct
Azure OpenAI chat client anymore. The model is chosen per node (see ModelConstants);
the gateway route (azure-openai | foundry | bedrock) is inferred from the model name.

- chat_json / chat_text : structured + free-text chat, used by every agent node.
- embeddings            : vector embeddings for retrieval indexing/search.
- get_langchain_llm     : an APIM-backed LangChain chat model (Anthropic-native shape)
                          for ReAct/tool-calling agents that need LangChain.
"""

import json
import random
import re
import threading
import time
from functools import lru_cache
from typing import TypeVar

import requests
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict

from app.config.constants import AppConstants, ApimConstants, LlmConstants, ModelConstants
from app.config.settings import get_settings
from app.utilities import run_control
from app.utilities import tls_util as tls
from app.utilities.llm_errors import (LlmError, ModelAuthError, ModelRateLimitError,
                                      ModelTimeoutError, PermanentModelRequestError,
                                      TransientGatewayError)
from app.utilities.logger_util import (agent_name_ctx, attempt_ctx, get_logger, redact,
                                       request_id_ctx, run_id_ctx, stage_ctx,
                                       user_email_ctx, user_id_ctx)
from app.utilities.structured_output import structured_call

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


def route_for_model(model: str) -> str:
    """azure-openai for gpt/o-series/text-embedding, foundry for everything else."""
    name = (model or "").lower()
    if name.startswith(ApimConstants.AZURE_OPENAI_PREFIXES):
        return ApimConstants.ROUTE_AZURE_OPENAI
    return ApimConstants.ROUTE_FOUNDRY


def image_block(data: bytes, mime: str, model: str) -> dict:
    """One image content block, shaped for the route `model` is sent over.

    Messages are posted as given on every route, so the caller has to pick the shape:
    the foundry route takes Anthropic-native blocks (per `FOUNDRY_IMAGE_FORMAT`), the
    azure-openai route an OpenAI `image_url` data URL.

    Args:
        data: The encoded image (PNG, JPEG or WebP bytes).
        mime: Its media type, e.g. "image/png".
        model: The model the message goes to; decides the route.

    Returns:
        The content block. The base64 payload is never logged.
    """
    import base64

    encoded = base64.b64encode(data).decode("ascii")
    anthropic = (route_for_model(model) == ApimConstants.ROUTE_FOUNDRY
                 and ApimConstants.FOUNDRY_IMAGE_FORMAT == "anthropic")
    log.debug("image block for %s: %d bytes as %s", model, len(data),
              "anthropic base64" if anthropic else "image_url data URL")
    if anthropic:
        return {"type": "image",
                "source": {"type": "base64", "media_type": mime, "data": encoded}}
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


class ApimLlmClient:
    def __init__(self, agent_id: str = AppConstants.APP_NAME,
                 agent_name: str = AppConstants.APP_NAME):
        self.agent_id = agent_id
        self.agent_name = agent_name
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        # model -> {param: renamed-to or None}; params that model already rejected.
        self._rejected_params: dict[str, dict[str, str | None]] = {}
        self._rejected_lock = threading.Lock()

    # --- auth ---------------------------------------------------------------
    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and time.time() < self._token_expires_at:
            log.debug("APIM token still valid; expires at %.0f", self._token_expires_at)
            return self._token
        # One refresh at a time: the client is a process-wide singleton shared by every
        # worker thread, and concurrent expiry would otherwise stampede the token endpoint.
        with self._token_lock:
            if not force_refresh and self._token and time.time() < self._token_expires_at:
                return self._token
            s = get_settings()
            if not (s.apim_client_id and s.apim_client_secret and s.apim_scope):
                log.error("APIM client_id, client_secret and scope are all required")
                raise ValueError("APIM client_id, client_secret and scope are all required")
            url = ApimConstants.TOKEN_URL_TEMPLATE.format(
                tenant_id=s.apim_tenant_id or ApimConstants.DEFAULT_TENANT_ID)
            resp = requests.post(url, timeout=ApimConstants.TOKEN_TIMEOUT_SECONDS,
                                 verify=tls.verify_setting(),
                                 data={"grant_type": "client_credentials",
                                       "client_id": s.apim_client_id,
                                       "client_secret": s.apim_client_secret,
                                       "scope": s.apim_scope})
            resp.raise_for_status()
            payload = resp.json()
            expires_in = int(payload.get("expires_in", 3600))
            self._token = payload["access_token"]
            self._token_expires_at = (time.time() + expires_in
                                      - ApimConstants.TOKEN_EXPIRY_SKEW_SECONDS)
            log.info("APIM token refreshed (forced=%s); expires_in=%ss", force_refresh,
                     expires_in)
            return self._token

    # --- headers / url ------------------------------------------------------
    @staticmethod
    def _ambient_tracking() -> dict:
        """Correlation headers derived from the log context of the calling thread.

        Lets a gateway log line be joined back to our request, run, stage and attempt
        without every call site threading a ``tracking`` dict through.
        """
        request_id = request_id_ctx.get()
        run_id = run_id_ctx.get()
        tracking = {}
        if request_id and request_id != "-":
            tracking["x-correlation-id"] = request_id
        if run_id:
            tracking["x-ms-client-request-id"] = ":".join(
                (run_id, stage_ctx.get() or "-", attempt_ctx.get() or "1"))
        elif request_id and request_id != "-":
            tracking["x-ms-client-request-id"] = request_id
        user_id = user_id_ctx.get()
        if user_id and user_id != "-":
            tracking["x-rok-user-id"] = user_id
        user_email = user_email_ctx.get()
        if user_email:
            tracking["x-rok-user-email"] = user_email
        agent_name = agent_name_ctx.get()
        if agent_name:
            tracking["x-rok-agent-name"] = agent_name
        return tracking

    def _tracking_headers(self, tracking: dict | None) -> dict:
        merged = {"x-rok-agent-id": self.agent_id,
                  "x-rok-agent-name": self.agent_name,
                  "x-rok-agent-version": AppConstants.AGENT_VERSION,
                  **self._ambient_tracking(), **(tracking or {})}
        return {k: merged[k] for k in ApimConstants.TRACKING_KEYS if merged.get(k)}

    def _headers(self, tracking: dict | None = None) -> dict:
        s = get_settings()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.get_token()}"}
        if s.apim_subscription_key:
            headers["Ocp-Apim-Subscription-Key"] = s.apim_subscription_key
            headers["api-key"] = s.apim_subscription_key
        headers.update(self._tracking_headers(tracking))
        return headers


    def _base_url(self) -> str:
        endpoint = get_settings().apim_openai_endpoint

        if not endpoint:
            raise ValueError("APIM_OPENAI_ENDPOINT is not set")

        endpoint = endpoint.rstrip("/")

        parsed = urlparse(endpoint)

        # If a full URL is provided, return only scheme + host
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

        return endpoint

    # --- transport ----------------------------------------------------------
    @staticmethod
    def _offending_param(response: requests.Response, body: dict) -> str | None:
        if not response.headers.get("content-type", "").startswith("application/json"):
            return None
        try:
            error = response.json().get("error")
        except ValueError:
            return None                      # a 400 with a malformed JSON body
        if not isinstance(error, dict):
            return None
        if error.get("param") in body:
            return error["param"]
        for name in re.findall(r"`([^`]+)`", error.get("message", "") or ""):
            if name in body:
                return name
        return None

    def _shed_param(self, response: requests.Response, body: dict,
                    model: str = "") -> str | None:
        """Drop (or rename) the one request param a model rejected with a 400.

        Reasoning models refuse ``temperature`` and want ``max_completion_tokens``;
        some routes reject ``response_format``. The rejection is remembered per model
        so later calls never pay the failed round trip again.

        Args:
            response: The 400 response.
            body: The request body, edited in place.
            model: The model the request was for.

        Returns:
            The param handled, or None.
        """
        param = self._offending_param(response, body)
        if param in ApimConstants.UNSHEDDABLE_PARAMS:
            log.warning("a 400 names %r, which is never dropped; the request fails as "
                        "sent (model %s)", param, model or "-")
            return None
        if param == "max_tokens" and "max_completion_tokens" in response.text:
            body["max_completion_tokens"] = body.pop("max_tokens")
            self._remember_rejected(model, param, "max_completion_tokens")
            return param
        if param:
            body.pop(param)
            self._remember_rejected(model, param, None)
            return param
        return None

    def _remember_rejected(self, model: str, param: str, rename_to: str | None) -> None:
        """Record that ``model`` rejects ``param`` (optionally wanting it renamed).

        Args:
            model: Model name; nothing is recorded without one.
            param: The rejected body param.
            rename_to: The name the model wants instead, or None to drop it.
        """
        if not model:
            return
        with self._rejected_lock:
            self._rejected_params.setdefault(model, {})[param] = rename_to
        log.info("remembering that model %s rejects param %r; later calls %s it",
                 model, param, f"rename it to {rename_to!r}" if rename_to else "omit")

    def _apply_known_rejections(self, body: dict, model: str) -> None:
        """Pre-shed params this model rejected before, so the first attempt succeeds.

        Args:
            body: The request body, edited in place.
            model: The model the request is for.
        """
        with self._rejected_lock:
            known = dict(self._rejected_params.get(model) or {})
        if not known:
            return
        for param, rename_to in known.items():
            if param not in body:
                continue
            value = body.pop(param)
            if rename_to and rename_to not in body:
                body[rename_to] = value
            log.debug("pre-shed param %r for model %s", param, model)

    @staticmethod
    def _correlation_ids(response: requests.Response | None) -> dict:
        """The gateway/backend correlation headers of a response (never request headers)."""
        if response is None:
            return {}
        return {name: response.headers[name] for name in ApimConstants.DIAG_HEADERS
                if response.headers.get(name)}

    @staticmethod
    def _retry_after(response: requests.Response | None) -> float | None:
        """Seconds from a numeric ``Retry-After`` header, capped; None if absent."""
        if response is None:
            return None
        try:
            value = float(response.headers.get("retry-after", ""))
        except ValueError:
            return None
        return max(0.0, min(value, ApimConstants.RETRY_AFTER_CAP_SECONDS))

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with full jitter for the given 1-based attempt."""
        ceiling = min(ApimConstants.BACKOFF_CAP_SECONDS,
                      ApimConstants.BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)

    @staticmethod
    def _classify(response: requests.Response, detail: str, **fields) -> LlmError:
        """Map a non-success gateway response to its typed error."""
        status = response.status_code
        if status == 429:
            return ModelRateLimitError(detail, **fields)
        if status in (401, 403):
            return ModelAuthError(detail, **fields)
        if status == 408:
            return ModelTimeoutError(detail, **fields)
        if status >= 500:
            return TransientGatewayError(detail, **fields)
        return PermanentModelRequestError(detail, **fields)

    def _post(self, url: str, body: dict, headers: dict, *, route: str = "",
              model: str = "") -> dict:
        """POST to the gateway with classified, bounded retries.

        * 400 naming an unsupported param: shed it and resend (not a retry).
        * 401: refresh the token once and resend; 403 fails immediately.
        * 429: honour ``Retry-After``; 5xx / timeouts / dropped connections: back off
          exponentially with jitter. No sleep after the final attempt, and the whole
          call respects ``ApimConstants.MAX_CALL_SECONDS``.

        Every failed attempt logs correlation diagnostics (status, route, model, path,
        gateway request ids, a redacted body excerpt). Request headers and payloads are
        never logged.

        Raises:
            LlmError: A typed, classified failure once retries are exhausted or the
                failure is not retryable.
            RunCancelledError / RunFencedError: The run was stopped mid-call.
        """
        path = urlparse(url).path
        started = time.monotonic()
        deadline = started + ApimConstants.MAX_CALL_SECONDS
        attempt, sheds, refreshed = 0, 0, False
        self._apply_known_rejections(body, model)
        while True:
            # A stopped run must not start another (possibly minutes-long) model call.
            run_control.checkpoint()
            attempt += 1
            response: requests.Response | None = None
            cause: BaseException | None = None
            try:
                response = requests.post(
                    url, headers=headers, json=body, verify=tls.verify_setting(),
                    timeout=(ApimConstants.CONNECT_TIMEOUT_SECONDS,
                             ApimConstants.REQUEST_TIMEOUT_SECONDS))
            except requests.Timeout as exc:          # before ConnectionError: ConnectTimeout is both
                cause = exc
            except requests.ConnectionError as exc:
                cause = exc

            if response is not None and response.status_code < 400:
                if attempt > 1:
                    log.info("gateway call recovered: route=%s model=%s path=%s attempts=%d",
                             route, model, path, attempt)
                    run_control.note_recovered()
                return response.json()

            if response is not None and response.status_code == 400 \
                    and sheds < ApimConstants.MAX_PARAM_SHEDS:
                shed = self._shed_param(response, body, model)
                if shed:
                    sheds += 1
                    attempt -= 1                     # shedding a param is not a retry
                    log.info("gateway rejected param %r for model %s; resending without it",
                             shed, model)
                    continue

            if response is not None and response.status_code == 401 and not refreshed:
                refreshed = True
                attempt -= 1                         # a token refresh is not a retry
                log.warning("gateway 401 for route=%s model=%s; refreshing token once",
                            route, model)
                headers = {**headers,
                           "Authorization": f"Bearer {self.get_token(force_refresh=True)}"}
                continue

            error = self._failure(response, cause, route=route, model=model, path=path,
                                  attempt=attempt, started=started, body=body)
            now = time.monotonic()
            if not error.retryable or attempt >= ApimConstants.MAX_ATTEMPTS or now >= deadline:
                log.error("gateway call failed: %s", error.detail,
                          extra={"error_type": type(error).__name__, "attempts": attempt,
                                 "retryable": error.retryable})
                raise error from cause
            delay = self._retry_after(response)
            if delay is None:
                delay = self._backoff(attempt)
            delay = min(delay, max(0.0, deadline - now))
            run_control.note_retry(error.error_code, attempt)
            run_control.sleep(delay)

    def _failure(self, response: requests.Response | None, cause: BaseException | None,
                 *, route: str, model: str, path: str, attempt: int, started: float,
                 body: dict) -> LlmError:
        """Log one failed attempt's diagnostics and build its typed error."""
        status = response.status_code if response is not None else None
        ids = self._correlation_ids(response)
        apim_id = (ids.get("apim-request-id") or ids.get("x-ms-request-id")
                   or ids.get("x-request-id") or ids.get("request-id") or "")
        excerpt = ""
        if response is not None:
            excerpt = redact(response.text[:ApimConstants.DIAG_BODY_CHARS])
        elif cause is not None:
            excerpt = redact(f"{type(cause).__name__}: {cause}"[:ApimConstants.DIAG_BODY_CHARS])
        label = str(status) if status is not None else type(cause).__name__
        detail = (f"{label} from gateway: route={route or '-'} model={model or '-'} "
                  f"path={path} apim_request_id={apim_id or '-'} attempt={attempt}"
                  f"/{ApimConstants.MAX_ATTEMPTS}; body={excerpt[:500]}")
        fields = {"status": status, "route": route, "model": model,
                  "apim_request_id": apim_id, "attempts": attempt, "body_excerpt": excerpt}
        if response is not None:
            error = self._classify(response, detail, **fields)
        elif isinstance(cause, requests.Timeout):
            error = ModelTimeoutError(detail, **fields)
        else:
            error = TransientGatewayError(detail, **fields)
        messages = body.get("messages") or body.get("input") or []
        log.warning(
            "gateway attempt failed: status=%s route=%s model=%s path=%s attempt=%d/%d "
            "elapsed_ms=%d request_bytes=%d messages=%d max_tokens=%s ids=%s body=%s",
            label, route or "-", model or "-", path, attempt, ApimConstants.MAX_ATTEMPTS,
            int((time.monotonic() - started) * 1000), self._body_size(body),
            len(messages) if isinstance(messages, list) else 1,
            body.get("max_tokens") or body.get("max_completion_tokens") or "-",
            ids or "-", excerpt or "-")
        return error

    @staticmethod
    def _body_size(body: dict) -> int:
        """Serialized request size in bytes; computed only for failure diagnostics."""
        try:
            return len(json.dumps(body, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return -1

    # --- chat routes --------------------------------------------------------
    def chat_completion(self, messages: list[dict], deployment: str,
                        temperature: float = LlmConstants.DEFAULT_TEMPERATURE,
                        max_tokens: int = 800, tracking: dict | None = None,
                        **kwargs) -> dict:
        url = (f"{self._base_url()}/openai/deployments/{deployment}/chat/completions"
               f"?api-version={ApimConstants.CHAT_API_VERSION}")
        body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                **kwargs}
        log.info("chat_completion request: %s", {"deployment": deployment, "temperature": temperature, "max_tokens": max_tokens})
        response = self._post(url, body, self._headers(tracking),
                              route=ApimConstants.ROUTE_AZURE_OPENAI, model=deployment)
        self._record_usage(deployment, response)
        return response

    def foundry_chat(self, messages: list[dict], model: str,
                     tracking: dict | None = None, **kwargs) -> dict:
        """Microsoft Foundry multi-model endpoint (claude-*, llama-*, ...)."""
        url = f"{self._base_url()}/foundry/models/chat/completions"
        body = {"model": model, "messages": messages, **kwargs}
        response = self._post(url, body, self._headers(tracking),
                              route=ApimConstants.ROUTE_FOUNDRY, model=model)
        self._record_usage(model, response)
        return response

    def bedrock_converse(self, messages: list[dict], model: str,
                         tracking: dict | None = None, **kwargs) -> dict:
        url = f"{self._base_url()}/aws-bedrock/converse"
        body = {"model": model, "messages": messages, **kwargs}
        response = self._post(url, body, self._headers(tracking),
                              route=ApimConstants.ROUTE_BEDROCK, model=model)
        self._record_usage(model, response)
        return response

    def complete(self, messages: list[dict], model: str, route: str | None = None,
                 temperature: float = LlmConstants.DEFAULT_TEMPERATURE,
                 max_tokens: int = 800, tracking: dict | None = None,
                 **kwargs) -> dict:
        """One chat call on the route inferred from ``model``.

        Args:
            **kwargs: Extra body params (e.g. ``response_format``); a route that
                rejects one has it shed by ``_post`` and the call is resent.
        """
        route = route or route_for_model(model)
        if route == ApimConstants.ROUTE_BEDROCK:
            return self.bedrock_converse(messages, model, tracking,
                                         temperature=temperature, max_tokens=max_tokens,
                                         **kwargs)
        if route == ApimConstants.ROUTE_FOUNDRY:
            return self.foundry_chat(messages, model, tracking,
                                     temperature=temperature, max_tokens=max_tokens,
                                     **kwargs)
        return self.chat_completion(messages, model, temperature=temperature,
                                    max_tokens=max_tokens, tracking=tracking, **kwargs)

    # --- embeddings ---------------------------------------------------------
    def embeddings(self, texts: list[str], model: str = ModelConstants.EMBEDDING,
                   batch_size: int = 64, tracking: dict | None = None) -> list[list[float]]:
        """Vector embeddings over the gateway's azure-openai embeddings route."""
        if not texts:
            return []
        url = (f"{self._base_url()}/openai/deployments/{model}/embeddings"
               f"?api-version={ApimConstants.CHAT_API_VERSION}")
        headers = self._headers(tracking)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            data = self._post(url, {"input": batch}, headers,
                              route=ApimConstants.ROUTE_AZURE_OPENAI, model=model)
            self._record_usage(model, data)
            rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            vectors.extend(row["embedding"] for row in rows)
        return vectors

    # --- response helpers ---------------------------------------------------
    @staticmethod
    def _record_usage(model: str, response: dict) -> None:
        """Fold one response's token usage into the active analytics agent run.

        Called from every chat/embedding route so token/cost capture happens once,
        centrally, for both the direct and LangChain paths. Best effort: a bad usage
        payload is swallowed so it never disturbs the model call.

        Args:
            model: Model/route name the call used.
            response: The raw gateway response.

        Returns:
            None.
        """
        try:
            from app.utilities.analytics.context import record_llm_usage
            usage = ApimLlmClient.usage_of(response)
            record_llm_usage(model, usage["prompt"], usage["completion"], usage["cached"])
        except Exception:                            # noqa: BLE001
            pass

    @staticmethod
    def reply_text(response: dict) -> str:
        if "choices" in response:
            return response["choices"][0].get("message", {}).get("content") or ""
        blocks = response.get("content")
        if not isinstance(blocks, list):
            blocks = (response.get("output", {}).get("output", {})
                      .get("message", {}).get("content", []))
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))

    @staticmethod
    def usage_of(response: dict) -> dict:
        usage = response.get("usage", {})
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        # Cached tokens are named differently per provider: OpenAI nests them under
        # prompt_tokens_details, Anthropic reports cache_read_input_tokens.
        cached = (usage.get("cache_read_input_tokens")
                  or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                  or usage.get("cached_tokens") or 0)
        return {"prompt": prompt, "completion": completion,
                "cached": cached,
                "total": usage.get("total_tokens") or prompt + completion}

    # --- public chat API ----------------------------------------------------
    def chat_text(self, system: str, messages: list[dict],
                  max_tokens: int = LlmConstants.CHAT_MAX_TOKENS,
                  model: str | None = None, route: str | None = None,
                  tracking: dict | None = None) -> str:
        model = model or ModelConstants.DEFAULT
        response = self.complete([{"role": "system", "content": system}, *messages],
                                 model=model, route=route, max_tokens=max_tokens,
                                 tracking=tracking)
        log.info("chat_text usage: %s", self.usage_of(response))
        return self.reply_text(response).strip()

    def chat_json(self, system: str, messages: list[dict], schema: type[T],
                  max_tokens: int = LlmConstants.JSON_MAX_TOKENS,
                  model: str | None = None, route: str | None = None,
                  tracking: dict | None = None, operation: str | None = None) -> T:
        """Structured answer validated against ``schema``; see ``structured_output``.

        Raises:
            StructuredOutputError: Typed, retryable failure after bounded repair.
            LlmError: A classified transport failure.
        """
        return structured_call(
            self, operation=operation or schema.__name__, schema=schema, system=system,
            messages=messages, model=model or ModelConstants.DEFAULT, route=route,
            max_tokens=max_tokens, tracking=tracking)

    # --- LangChain (Anthropic over the gateway) -----------------------------
    def get_langchain_llm(self, model: str | None = None,
                          temperature: float = LlmConstants.DEFAULT_TEMPERATURE,
                          max_tokens: int = 4096):
        """APIM-backed LangChain chat model for ReAct/tool-calling agents."""
        chat_model_cls = _apim_claude_chat_model_class()
        return chat_model_cls(apim_client=self, model_name=model or ModelConstants.DEFAULT,
                              temperature=temperature, max_tokens=max_tokens)


@lru_cache
def get_apim_llm_client() -> ApimLlmClient:
    return ApimLlmClient()


# --------------------------------------------------------------------------- #
# LangChain adapter (Anthropic-native request/response over foundry_chat)      #
# --------------------------------------------------------------------------- #

def _messages_to_anthropic(messages: list) -> list[dict]:
    """LangChain BaseMessage list -> Anthropic-style dicts; consecutive ToolMessages
    merge into a single user turn of tool_result blocks."""
    from langchain_core.messages import AIMessage, ToolMessage

    out: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, ToolMessage):
            blocks = []
            while i < len(messages) and isinstance(messages[i], ToolMessage):
                tm = messages[i]
                blocks.append({"type": "tool_result", "tool_use_id": tm.tool_call_id,
                               "content": str(tm.content)})
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        if isinstance(msg, AIMessage):
            content = []
            if msg.content:
                content.append({"type": "text",
                                "text": msg.content if isinstance(msg.content, str) else str(msg.content)})
            for tc in (msg.tool_calls or []):
                content.append({"type": "tool_use", "id": tc["id"],
                                "name": tc["name"], "input": tc["args"]})
            out.append({"role": "assistant", "content": content or ""})
            i += 1
            continue
        role = {"system": "system", "human": "user", "ai": "assistant"}.get(msg.type, "user")
        out.append({"role": role,
                    "content": msg.content if isinstance(msg.content, str) else str(msg.content)})
        i += 1
    return out


@lru_cache
def _apim_claude_chat_model_class():
    """Build the class lazily so callers that only need chat_json don't import langchain_core."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool

    class ApimClaudeChatModel(BaseChatModel):
        """LangChain chat model over ApimLlmClient.foundry_chat (Anthropic response shape)."""

        model_config = ConfigDict(arbitrary_types_allowed=True)
        apim_client: ApimLlmClient
        model_name: str
        temperature: float = LlmConstants.DEFAULT_TEMPERATURE
        max_tokens: int = 4096

        @property
        def _llm_type(self) -> str:
            return "apim-claude"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            anthropic_messages = _messages_to_anthropic(messages)
            extra = {}
            if kwargs.get("tools"):
                extra["tools"] = kwargs["tools"]
            response = self.apim_client.foundry_chat(
                anthropic_messages, model=self.model_name,
                temperature=self.temperature, max_tokens=self.max_tokens, **extra)
            text, tool_calls = "", []
            for block in response.get("content") or []:
                if block.get("type") == "text":
                    text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append({"name": block.get("name"),
                                       "args": block.get("input") or {}, "id": block.get("id")})
            message = AIMessage(content=text, tool_calls=tool_calls)
            return ChatResult(generations=[ChatGeneration(message=message)])

        def bind_tools(self, tools, **kwargs):
            anthropic_tools = []
            for tool in tools:
                spec = convert_to_openai_tool(tool)["function"]
                anthropic_tools.append({
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "input_schema": spec.get("parameters", {"type": "object", "properties": {}}),
                })
            return self.bind(tools=anthropic_tools, **kwargs)

    return ApimClaudeChatModel