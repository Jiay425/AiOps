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

    async def complete(self, prompt: str, *, system: str = "", model: str | None = None,
                       max_tokens: int | None = None) -> str:
        if not self.available:
            raise RuntimeError("OPENAI_API_KEY or OPENAI_BASE_URL is not configured")
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        root = self.settings.openai_base_url.rstrip("/")
        timeout_seconds = float(getattr(self.settings, "codeops_llm_timeout_seconds",
                                       self.settings.integration_timeout_seconds) or
                                self.settings.integration_timeout_seconds)
        empty_retries = max(0, int(getattr(self.settings, "codeops_llm_empty_content_retries", 1) or 0))
        selected_model = model or self.settings.openai_model
        thinking_type = str(getattr(self.settings, "codeops_llm_thinking_type", "disabled") or "").strip().lower()
        request_payload = {"model": selected_model, "messages": messages, "temperature": 0.1,
                           "max_tokens": int(max_tokens or getattr(self.settings, "codeops_llm_max_output_tokens", 2048) or 2048)}
        if selected_model.lower().startswith("deepseek-v4") and thinking_type in {"enabled", "disabled"}:
            request_payload["thinking"] = {"type": thinking_type}
        last_error: Exception | None = None
        data: dict[str, Any] = {}
        choice: dict[str, Any] = {}
        for attempt in range(empty_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.post(
                        f"{root}/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                        json=request_payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                last_error = None
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt >= empty_retries:
                    raise
                continue
            choices = data.get("choices") or []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content", "")
            if str(content).strip():
                return str(content)
        if last_error is not None:
            raise last_error
        usage = data.get("usage") if isinstance(data, dict) else {}
        details = {"finishReason": choice.get("finish_reason"),
                   "completionTokens": (usage or {}).get("completion_tokens"),
                   "reasoningTokens": ((usage or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")}
        raise RuntimeError(f"EXTERNAL_LLM_EMPTY_CONTENT after {empty_retries} provider retries; "
                           f"providerMeta={details}")
