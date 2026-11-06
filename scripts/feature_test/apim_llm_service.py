"""Call LLM models exposed through an Azure API Management (APIM) AI Gateway."""
import re

import requests

import config


def get_token(tenant_id: str | None = None, client_id: str | None = None, client_secret: str | None = None,
               scope: str | None = None) -> str:
    """Client-credentials OAuth token, falling back to azure-creds APIM-* values when not passed."""
    tenant_id = tenant_id or config.get("APIM-TENANT-ID")
    client_id = client_id or config.get("APIM-CLIENT-ID")
    client_secret = client_secret or config.get("APIM-CLIENT-SECRET")
    scope = scope or config.get("APIM-SCOPE")
    if not (tenant_id and client_id and client_secret and scope):
        raise ValueError("tenant_id, client_id, client_secret and scope are all required")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }
    resp = requests.post(url, data=data, timeout=30, verify=config.ca_bundle())
    resp.raise_for_status()
    return resp.json()["access_token"]


# Usage-attribution headers accepted by the gateway (see ROK AI Gateway Engineering Guide).
TRACKING_KEYS = (
    "x-rok-user-id",
    "x-rok-user-email",
    "x-rok-agent-id",
    "x-rok-agent-name",
    "x-rok-agent-version",
)


def _headers(token: str | None, tracking: dict | None = None, subscription_key: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    sub_key = subscription_key if subscription_key is not None else config.get("APIM-SUBSCRIPTION-KEY")
    if sub_key:
        headers["Ocp-Apim-Subscription-Key"] = sub_key
        headers["api-key"] = sub_key
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for key in TRACKING_KEYS:
        value = (tracking or {}).get(key)
        if value:
            headers[key] = value
    return headers


def _base_url(endpoint: str | None = None) -> str:
    endpoint = endpoint or config.get("APIM-OPENAI-ENDPOINT")
    if not endpoint:
        raise ValueError("APIM-OPENAI-ENDPOINT is not set in azure-creds")
    return endpoint.rstrip("/")


def list_models(token: str | None = None, tracking: dict | None = None,
                endpoint: str | None = None, subscription_key: str | None = None) -> list[dict]:
    """Model Discovery API: every model deployed behind the gateway."""
    url = f"{_base_url(endpoint)}/model-discovery/models"
    resp = requests.get(
        url, headers=_headers(token, tracking, subscription_key), timeout=30, verify=config.ca_bundle()
    )
    resp.raise_for_status()
    return resp.json().get("models", [])


def _offending_param(response: requests.Response, body: dict) -> str | None:
    """Name the request field a 400 is complaining about, from `param` or a backticked hint."""
    if not response.headers.get("content-type", "").startswith("application/json"):
        return None
    error = response.json().get("error")
    if not isinstance(error, dict):
        return None
    if error.get("param") in body:
        return error["param"]
    for name in re.findall(r"`([^`]+)`", error.get("message", "")):
        if name in body:
            return name
    return None


def _raise_with_body(resp: requests.Response) -> None:
    """Like resp.raise_for_status(), but includes the response body so gateway error details aren't lost."""
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(f"{e} | response body: {resp.text}", response=resp) from e


def _post(url: str, body: dict, headers: dict) -> dict:
    """POST, shedding params the model rejects — reasoning models drop temperature and rename max_tokens."""
    for _ in range(4):
        resp = requests.post(url, headers=headers, json=body, timeout=120, verify=config.ca_bundle())
        if resp.status_code == 400:
            param = _offending_param(resp, body)
            if param == "max_tokens" and "max_completion_tokens" in resp.text:
                body["max_completion_tokens"] = body.pop("max_tokens")
                continue
            if param:
                body.pop(param)
                continue
        _raise_with_body(resp)
        return resp.json()
    _raise_with_body(resp)
    return resp.json()


def chat_completion(deployment: str, messages: list[dict], api_version: str = "2023-12-01-preview",
                    token: str | None = None, temperature: float = 0.7, max_tokens: int = 800,
                    tracking: dict | None = None, endpoint: str | None = None,
                    subscription_key: str | None = None) -> dict:
    """Azure OpenAI chat completions (gpt-4o, gpt-5, ...)."""
    url = f"{_base_url(endpoint)}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    return _post(url, body, _headers(token, tracking, subscription_key))


def foundry_chat(model: str, messages: list[dict], token: str | None = None,
                 tracking: dict | None = None, endpoint: str | None = None,
                 subscription_key: str | None = None, **kwargs) -> dict:
    """Microsoft Foundry multi-model endpoint (claude-*, llama-*, ...)."""
    url = f"{_base_url(endpoint)}/foundry/models/chat/completions"
    return _post(
        url, {"model": model, "messages": messages, **kwargs},
        _headers(token, tracking, subscription_key),
    )


def bedrock_converse(model: str, messages: list[dict], token: str | None = None,
                     tracking: dict | None = None, endpoint: str | None = None,
                     subscription_key: str | None = None, **kwargs) -> dict:
    """AWS Bedrock converse endpoint (amazon.nova-*, deepseek, mistral, ...)."""
    url = f"{_base_url(endpoint)}/aws-bedrock/converse"
    return _post(
        url, {"model": model, "messages": messages, **kwargs},
        _headers(token, tracking, subscription_key),
    )


def route_for(providers: list[str]) -> str:
    """Pick the gateway path for a model, preferring the native Azure OpenAI route."""
    for preferred in ("azure-openai", "foundry", "bedrock"):
        if preferred in providers:
            return preferred
    return "azure-openai"


def complete(model: str, messages: list[dict], route: str = "azure-openai", token: str | None = None,
             tracking: dict | None = None, temperature: float = 0.7, max_tokens: int = 800,
             endpoint: str | None = None, subscription_key: str | None = None,
             api_version: str = "2023-12-01-preview") -> dict:
    """Send a chat request over whichever gateway route serves the model."""
    if route == "bedrock":
        return bedrock_converse(model, messages, token, tracking,
                               endpoint, subscription_key, temperature=temperature, max_tokens=max_tokens)
    if route == "foundry":
        return foundry_chat(model, messages, token, tracking,
                           endpoint, subscription_key, temperature=temperature, max_tokens=max_tokens)
    return chat_completion(
        model, messages, api_version=api_version, token=token, tracking=tracking,
        temperature=temperature, max_tokens=max_tokens, endpoint=endpoint,
        subscription_key=subscription_key,
    )


def reply_text(response: dict) -> str:
    """Pull the assistant text out of an OpenAI, Foundry/Anthropic or Bedrock response."""
    if "choices" in response:
        return response["choices"][0].get("message", {}).get("content") or ""
    blocks = response.get("content")
    if not isinstance(blocks, list):
        blocks = response.get("output", {}).get("output", {}).get("message", {}).get("content", [])
    # Anthropic interleaves "thinking" blocks that carry no user-facing text.
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def usage_of(response: dict) -> dict:
    """Normalise token usage across the three response shapes."""
    usage = response.get("usage", {})
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return {
        "prompt": prompt,
        "completion": completion,
        "total": usage.get("total_tokens") or prompt + completion,
    }
