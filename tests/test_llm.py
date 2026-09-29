import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.llm import LLMClient, LLMError


class FakeCompletions:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_client(content: str | None, **overrides: Any) -> tuple[LLMClient, FakeCompletions]:
    completions = FakeCompletions(content)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    config = Settings(_env_file=None, nebius_model="test-model", **overrides)
    return LLMClient(config, client=client), completions


def test_missing_key_names_the_right_env_var() -> None:
    with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
        LLMClient(Settings(_env_file=None, nebius_api_key=SecretStr("")))
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        LLMClient(Settings(_env_file=None, llm_provider="deepseek", deepseek_api_key=SecretStr("")))


def test_provider_picks_its_model() -> None:
    nebius, _ = fake_client("x")
    assert (nebius.provider, nebius.model) == ("Nebius", "test-model")

    deepseek, _ = fake_client("x", llm_provider="deepseek", deepseek_model="ds-model")
    assert (deepseek.provider, deepseek.model) == ("DeepSeek", "ds-model")


def test_complete_sends_model_and_json_mode() -> None:
    llm, completions = fake_client('{"ok": true}')

    assert llm.complete([{"role": "user", "content": "hi"}], json_mode=True) == '{"ok": true}'
    call = completions.calls[0]
    assert call["model"] == "test-model"
    assert call["response_format"] == {"type": "json_object"}

    llm.complete([{"role": "user", "content": "hi"}])
    assert "response_format" not in completions.calls[1]


def test_empty_reply_is_an_error() -> None:
    llm, _ = fake_client(None)
    with pytest.raises(LLMError, match="empty"):
        llm.complete([{"role": "user", "content": "hi"}])


def _active_key() -> str:
    key = settings.deepseek_api_key if settings.llm_provider == "deepseek" else settings.nebius_api_key
    return key.get_secret_value()


@pytest.mark.live
@pytest.mark.skipif(not _active_key(), reason="API key for LLM_PROVIDER not set")
def test_live_llm_call() -> None:
    """Step 2 gate: a real call to the configured provider, plain and JSON mode."""
    llm = LLMClient(settings)

    reply = llm.complete([{"role": "user", "content": "Reply with the single word: pong"}])
    assert "pong" in reply.lower()

    raw = llm.complete(
        [{"role": "user", "content": 'Return JSON exactly like {"answer": 4} for 2+2.'}],
        json_mode=True,
    )
    assert json.loads(raw)["answer"] == 4
