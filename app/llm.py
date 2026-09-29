"""LLM client. Every model call goes through here.

Nebius (Nemotron) is the real target. DeepSeek is a dev stand-in, picked with LLM_PROVIDER.
Both speak the OpenAI API, so only the key, URL and model differ.
"""

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
        if settings.llm_provider == "deepseek":
            self.provider = "DeepSeek"
            key, base_url, self.model = (
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                settings.deepseek_model,
            )
        else:
            self.provider = "Nebius"
            key, base_url, self.model = (
                settings.nebius_api_key,
                settings.nebius_base_url,
                settings.nebius_model,
            )
        self._key_name = f"{self.provider.upper()}_API_KEY"
        api_key = key.get_secret_value()
        if not api_key and client is None:
            raise LLMError(f"{self._key_name} is not set. Add it to .env.")
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
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
            raise LLMError(f"{self.provider} rejected {self._key_name}.") from e
        except openai.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' not found on {self.provider}.") from e
        except openai.APITimeoutError as e:
            raise LLMError(f"{self.provider} timed out.") from e
        except openai.APIError as e:
            raise LLMError(f"{self.provider} request failed: {type(e).__name__}") from e

        ms = int((time.perf_counter() - start) * 1000)
        log.info("llm_call provider=%s model=%s ms=%d json=%s", self.provider, self.model, ms, json_mode)
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise LLMError(f"{self.provider} returned an empty reply.")
        return content
