from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class OpenAICompatibleClient:
    """Small provider-neutral client; LangGraph owns orchestration, not an AI SDK."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.openai_api_key and self.settings.openai_base_url)

    async def complete(self, prompt: str, *, system: str = "", model: str | None = None) -> str:
        if not self.available:
            raise RuntimeError("OPENAI_API_KEY or OPENAI_BASE_URL is not configured")
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        root = self.settings.openai_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=self.settings.integration_timeout_seconds) as client:
            response = await client.post(
                f"{root}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={"model": model or self.settings.openai_model, "messages": messages, "temperature": 0.1},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not str(content).strip():
            raise RuntimeError("LLM returned empty content")
        return str(content)

