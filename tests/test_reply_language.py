"""Chat replies follow the user's language; the deck stays English."""

from app.genai.agents import reply_language as rl
from app.genai.agents.reply_language import ReplyLanguage

policy = ReplyLanguage()


class _Client:
    def __init__(self, reply="", fail=False):
        self.reply, self.fail, self.calls = reply, fail, 0

    def chat_text(self, system, messages, max_tokens=0, tracking=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return self.reply


def _use(monkeypatch, client):
    monkeypatch.setattr(rl, "get_apim_llm_client", lambda: client)


def test_english_and_missing_language_are_default():
    assert policy.is_default("English") and policy.is_default("english")
    assert policy.is_default(None) and policy.is_default("")
    assert not policy.is_default("Portuguese")


def test_instruction_only_for_other_languages():
    assert policy.instruction("English") == ""
    text = policy.instruction("Portuguese")
    assert "Portuguese" in text and "stay in English" in text


def test_english_makes_no_llm_call(monkeypatch):
    client = _Client()
    _use(monkeypatch, client)
    assert policy.translate(["Your deck is ready"], "English") == ["Your deck is ready"]
    assert client.calls == 0


def test_translates_segments_in_one_call(monkeypatch):
    client = _Client("O seu deck esta pronto\n=====\nTexto dos slides:")
    _use(monkeypatch, client)
    out = policy.translate(["Your deck is ready", "Slide text:"], "Portuguese")
    assert out == ["O seu deck esta pronto", "Texto dos slides:"] and client.calls == 1


def test_mismatched_segment_count_keeps_original(monkeypatch):
    _use(monkeypatch, _Client("only one part"))
    texts = ["a", "b"]
    assert policy.translate(texts, "French") == texts


def test_llm_failure_keeps_original(monkeypatch):
    _use(monkeypatch, _Client(fail=True))
    assert policy.translate(["a"], "French") == ["a"]


def test_whole_composed_reply_follows_the_user_language(monkeypatch):
    """The slide outline in the reply is chat too, so it is localised with the rest."""
    from app.genai.workflows import deck_workflow as wf

    draft = {"title": "Deck", "slides": [{"slide_number": 1, "title": "What We Will Cover"}]}
    monkeypatch.setattr(wf.session_store, "get_draft", lambda sid: draft)
    seen = {}

    def fake_translate(texts, language):
        seen["texts"], seen["language"] = texts, language
        return [f"PT:{t}" for t in texts]

    monkeypatch.setattr(wf._reply_language, "translate", fake_translate)
    deck = type("D", (), {"deck_version": 1})()
    final = {"actions_taken": ["plan"], "deck_version": 1, "user_language": "Portuguese"}

    reply = wf._compose_reply("s", final, [deck])

    assert seen["language"] == "Portuguese" and len(seen["texts"]) == 2
    assert "What We Will Cover" in seen["texts"][1]
    assert all(part.startswith("PT:") for part in reply.split("\n\n")[:1])
