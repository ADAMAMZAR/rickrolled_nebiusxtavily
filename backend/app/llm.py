"""LLM client: NVIDIA Nemotron on Nebius Token Factory. Every model call goes through here."""

import logging
import threading
import time
from typing import Any

import openai
from openai import OpenAI

from app.config import Settings

log = logging.getLogger("continuum.llm")


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.nebius_model
        api_key = settings.nebius_api_key.get_secret_value()
        if not api_key and client is None:
            raise LLMError("NEBIUS_API_KEY is not set. Add it to .env.")
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=settings.nebius_base_url,
            timeout=settings.llm_timeout,
            max_retries=2,
        )
        # Totals for cost estimates (troth_eval.md). A message check calls from several threads at once.
        self.calls = self.tokens_in = self.tokens_out = 0
        self._lock = threading.Lock()

    def complete(self, messages: list[dict[str, Any]], json_mode: bool = False, model: str | None = None) -> str:
        """Return the model's text reply. json_mode asks for a JSON object.
        `model` overrides NEBIUS_MODEL for one call (screenshots go to NEBIUS_VISION_MODEL)."""
        model = model or self.model
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        start = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=model, messages=messages, temperature=0, **extra
            )
        except openai.AuthenticationError as e:
            raise LLMError("Nebius rejected NEBIUS_API_KEY.") from e
        except openai.NotFoundError as e:
            raise LLMError(f"Model '{model}' not found on Nebius.") from e
        except openai.APITimeoutError as e:
            raise LLMError("Nebius timed out.") from e
        except openai.APIError as e:
            raise LLMError(f"Nebius request failed: {type(e).__name__}") from e

        ms = int((time.perf_counter() - start) * 1000)
        usage = getattr(response, "usage", None)
        tokens_in, tokens_out = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        with self._lock:
            self.calls += 1
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
        log.info("llm_call model=%s ms=%d json=%s tokens_in=%d tokens_out=%d", model, ms, json_mode, tokens_in, tokens_out)
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise LLMError("Nebius returned an empty reply.")
        return content
