"""LLM helpers: chat_text / chat_json (structured, validated) + a LangChain model."""

import json
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config.constants import AzureConstants, LlmConstants
from app.config.settings import get_settings
from app.utilities import tls_util as tls
from app.utilities.clients_util import get_aoai_client
from app.utilities.logger_util import get_logger

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


def chat_text(system: str, messages: list[dict],
              max_tokens: int = LlmConstants.CHAT_MAX_TOKENS) -> str:
    resp = get_aoai_client().chat.completions.create(
        model=get_settings().azure_openai_chat_deployment,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, *messages])
    return (resp.choices[0].message.content or "").strip()


def chat_json(system: str, messages: list[dict], schema: type[T],
              max_tokens: int = LlmConstants.JSON_MAX_TOKENS) -> T:
    client, s = get_aoai_client(), get_settings()
    req = [{"role": "system", "content": system}, *messages]
    for attempt in range(1, LlmConstants.JSON_RETRY_ATTEMPTS + 1):
        resp = client.chat.completions.create(
            model=s.azure_openai_chat_deployment, max_tokens=max_tokens,
            response_format={"type": "json_object"}, messages=req)
        raw = (resp.choices[0].message.content or "").strip()
        try:
            return schema.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("invalid structured output",
                        extra={"attempt": attempt, "error": str(exc)[:300]})
            if attempt == LlmConstants.JSON_RETRY_ATTEMPTS:
                raise
            req += [{"role": "assistant", "content": raw},
                    {"role": "user", "content":
                        f"That JSON was invalid: {exc}. Reply with ONLY corrected JSON."}]
    raise RuntimeError("unreachable")


@lru_cache
def get_langchain_llm(temperature: float = LlmConstants.DEFAULT_TEMPERATURE):
    """AzureChatOpenAI; tiktoken_model_name avoids a None crash on unknown deployments."""
    from langchain_openai import AzureChatOpenAI
    s = get_settings()
    return AzureChatOpenAI(
        azure_endpoint=s.azure_openai_endpoint,
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
        azure_deployment=s.azure_openai_chat_deployment,
        tiktoken_model_name=LlmConstants.TIKTOKEN_MODEL_NAME,
        temperature=temperature,
        http_client=tls.http_client(),
        max_retries=AzureConstants.MAX_RETRIES,
    )