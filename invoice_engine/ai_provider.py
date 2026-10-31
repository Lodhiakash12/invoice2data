"""AI providers: the one place that talks to a language model.

The rest of the engine calls `provider.complete(system, prompt)` and gets text back. Which
service answers is set in config/settings.yml (section `ai`), so changing from NVIDIA's free
API to Claude later means adding one class here and changing the setting:

    ai:
      provider: openai_compatible           # NVIDIA build.nvidia.com, a local Ollama server, ...
      base_url: https://integrate.api.nvidia.com/v1
      model: <model id from build.nvidia.com>
      api_key_env: NVIDIA_API_KEY           # the key is read from this environment variable

The key is never written to a file by the engine. Only the standard library is used.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class AIError(RuntimeError):
    """The model could not be reached or answered with an error."""


@dataclass
class AISettings:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = "https://integrate.api.nvidia.com/v1"
    model: str = ""
    api_key_env: str = "NVIDIA_API_KEY"
    timeout_seconds: int = 180
    max_attempts: int = 3                 # template drafts per document (first + corrections)
    send_unanonymised_text: bool = False  # must be true until anonymisation is added
    temperature: float = 0.0
    max_tokens: int = 32000               # reasoning models think before they answer
    extra_body: dict | None = None        # extra request fields the service supports, as is
    second_opinion: int = 3               # first documents of each template the AI reads again (0 = off)
    max_minutes: float = 10               # time for the AI step on one document (all its drafts)
    batch_size: int = 3                   # documents of one layout collected before the AI works on them
                                          # (0 = at once, while the document is read)
    max_wait_days: float = 7              # a group with fewer documents goes to the AI after this long

    @classmethod
    def from_settings(cls, settings: dict) -> "AISettings":
        block = (settings or {}).get("ai") or {}
        known = {k: v for k, v in block.items() if k in cls.__dataclass_fields__ and v is not None}
        return cls(**known)

    def problems(self) -> list[str]:
        """Why the AI step cannot run with these settings (empty = it can)."""
        found = []
        if not self.model:
            found.append("ai.model is empty in config/settings.yml (pick a model id, see "
                         "python process_invoice.py --ai-models)")
        if self.provider == "openai_compatible" and not os.environ.get(self.api_key_env):
            found.append(f"the environment variable {self.api_key_env} with the API key is not set")
        if not self.send_unanonymised_text:
            found.append("ai.send_unanonymised_text is false: the page text would go to the AI service "
                         "without anonymisation (set it to true only for test documents, until "
                         "anonymisation is added)")
        return found


class OpenAICompatibleProvider:
    """Any service with the OpenAI chat-completions API: NVIDIA build.nvidia.com, Ollama, vLLM."""

    def __init__(self, settings: AISettings):
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        self.last_meta: dict = {}                     # finish reason and answer sizes, for the log

    def _request(self, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = os.environ.get(self.settings.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
        delay = 5.0
        for attempt in range(4):                      # retries on rate limits and server errors
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise AIError(f"{self.base_url}{path}: HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise AIError(f"{self.base_url}{path}: {exc}") from None
        raise AIError("unreachable")

    def complete(self, system: str, prompt: str) -> str:
        body = {"model": self.settings.model, "temperature": self.settings.temperature,
                "max_tokens": self.settings.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if isinstance(self.settings.extra_body, dict):
            body.update(self.settings.extra_body)
        answer = self._request("/chat/completions", body)
        try:
            choice = answer["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise AIError(f"unexpected answer: {json.dumps(answer)[:300]}") from None
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        finish = choice.get("finish_reason")
        self.last_meta = {"finish_reason": finish, "content_chars": len(content),
                          "reasoning_chars": len(reasoning)}
        # reasoning models: the thinking is in <think>...</think> or in a separate field
        visible = re.sub(r"(?s)<think>.*?(</think>|$)", "", content).strip()
        if not visible and finish == "length":
            # the model was still thinking when the limit came: its notes are not an answer
            raise AIError(f"the answer was cut off at ai.max_tokens ({self.settings.max_tokens}) while the model "
                          f"was still thinking ({len(reasoning)} characters of thinking, no answer): raise "
                          "ai.max_tokens or use a model that thinks less")
        if not visible and reasoning:
            visible = reasoning                       # some models put the whole answer there
        if not visible:
            raise AIError(f"the model returned an empty answer (finish_reason: {finish})")
        return visible

    def models(self) -> list[str]:
        answer = self._request("/models")
        return sorted(m.get("id", "") for m in answer.get("data", []))


def make_provider(settings: AISettings):
    if settings.provider == "openai_compatible":
        return OpenAICompatibleProvider(settings)
    raise AIError(f"unknown ai.provider {settings.provider!r} (supported: openai_compatible)")
