"""LLM client: NVIDIA Nemotron on Nebius Token Factory. Every model call goes through here."""

import logging
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

    def complete(self, messages: list[dict[str, str]], json_mode: bool = False) -> str:
        """Return the model's text reply. json_mode asks for a JSON object."""
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        start = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=self.model, messages=messages, temperature=0, **extra
            )
        except openai.AuthenticationError as e:
            raise LLMError("Nebius rejected NEBIUS_API_KEY.") from e
        except openai.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' not found on Nebius.") from e
        except openai.APITimeoutError as e:
            raise LLMError("Nebius timed out.") from e
        except openai.APIError as e:
            raise LLMError(f"Nebius request failed: {type(e).__name__}") from e

        ms = int((time.perf_counter() - start) * 1000)
        log.info("llm_call model=%s ms=%d json=%s", self.model, ms, json_mode)
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise LLMError("Nebius returned an empty reply.")
        return content
