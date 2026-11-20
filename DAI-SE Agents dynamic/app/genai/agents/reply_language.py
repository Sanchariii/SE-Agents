"""Chat replies follow the user's language; the deck language is decided separately."""

from app.config.constants import AgentNames, PptConstants
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_SEPARATOR = "\n=====\n"


class ReplyLanguage:
    """Localises the fixed, code-written parts of a reply (the model-written parts
    already follow the language instruction in their prompts)."""

    def __init__(self, default: str = PptConstants.DEFAULT_LANGUAGE) -> None:
        self._default = default

    def is_default(self, language: str | None) -> bool:
        return not language or language.strip().lower() == self._default.lower()

    def instruction(self, language: str | None) -> str:
        """Appended to an agent's input so its final reply uses the user's language."""
        if self.is_default(language):
            return ""
        return (f"\n\n(Write your final reply to the user in {language}. "
                f"Plan and slide text stay in English.)")

    def translate(self, texts: list[str], language: str | None) -> list[str]:
        """Translate short UI sentences in one call; on any problem, return them as written."""
        if self.is_default(language) or not texts:
            return texts
        system = (f"Translate each segment into {language}. Keep numbers, quotes and "
                  f"version labels like v2 unchanged. Segments are separated by a line "
                  f"containing only =====. Return only the translated segments, "
                  f"separated the same way.")
        try:
            reply = get_apim_llm_client().chat_text(
                system, [{"role": "user", "content": _SEPARATOR.join(texts)}],
                max_tokens=4000,
                tracking={"x-rok-agent-name": AgentNames.REPLY_LANGUAGE})
        except Exception:                                  # noqa: BLE001
            log.warning("reply translation failed; keeping the English text", exc_info=True)
            return texts
        parts = [p.strip() for p in reply.split("=====")]
        return parts if len(parts) == len(texts) else texts
