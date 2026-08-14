from __future__ import annotations

import os
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..llm import OpenAICompatibleClient
from ..store import Store
from ..tools import mcp_client_from_config
from .rag import RunbookRagService


@dataclass
class OpsChatClientResolution:
    available: bool
    role: str
    client_id: str
    source: str
    fallback: bool
    config: dict[str, Any]
    message: str = ""


class OpsChatClientResolver:
    ROLE_IDS = {"PLANNER": "ops_agent_chat_planner_client_id",
                "EVIDENCE_REVIEWER": "ops_agent_chat_reviewer_client_id",
                "REPORT_WRITER": "ops_agent_chat_report_writer_client_id"}

    def __init__(self, settings: Settings, store: Store | None, fallback: OpenAICompatibleClient):
        self.settings, self.store, self.fallback = settings, store, fallback

    async def resolve(self, role: str) -> OpsChatClientResolution:
        attribute = self.ROLE_IDS.get(role)
        if not attribute:
            return OpsChatClientResolution(False, role, "", "UNAVAILABLE", False, {}, "Agent role is required.")
        client_id = str(getattr(self.settings, attribute))
        if self.settings.ops_agent_chat_use_configured_client and self.store:
            config = await self.store.load_ai_client(client_id)
            if config:
                config = {**config, "apiKey": self._placeholder(str(config.get("apiKey") or "")),
                          "baseUrl": self._placeholder(str(config.get("baseUrl") or ""))}
                return OpsChatClientResolution(True, role, client_id, "AI_CLIENT_DATABASE", False, config,
                                               "Resolved dynamic ai_client configuration.")
        if self.fallback.available:
            return OpsChatClientResolution(True, role, client_id, "OPEN_AI_CHAT_MODEL_FALLBACK", True, {},
                                           "Resolved the configured fallback OpenAI-compatible model.")
        return OpsChatClientResolution(False, role, client_id, "UNAVAILABLE", False, {},
                                       f"Configured ChatClient is unavailable: ai_client_{client_id}")

    @staticmethod
    def _placeholder(value: str) -> str:
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?}", value.strip())
        return os.getenv(match.group(1), match.group(2) or "") if match else value


class OpsMultiChatAgentService:
    def __init__(self, settings: Settings, store: Store | None, fallback: OpenAICompatibleClient):
        self.settings, self.fallback = settings, fallback
        self.resolver = OpsChatClientResolver(settings, store, fallback)
        self.rag = RunbookRagService(settings)
        self._memory: dict[str, list[dict[str, str]]] = {}

    async def call(self, role: str, prompt: str, default_system: str) -> dict[str, Any]:
        started = time.perf_counter()
        resolution = await self.resolver.resolve(role)
        if not resolution.available:
            return {"role": role, "success": False, "fallback": False, "content": "", "rawContent": "",
                    "clientBeanName": f"ai_client_{resolution.client_id}", "resolutionSource": resolution.source,
                    "costMillis": int((time.perf_counter() - started) * 1000), "errorMessage": resolution.message}
        try:
            if resolution.config:
                content = await self._configured_complete(resolution.config, prompt, default_system)
            else:
                content = await self.fallback.complete(prompt, system=default_system)
            return {"role": role, "success": bool(content.strip()), "fallback": resolution.fallback,
                    "content": content, "rawContent": content,
                    "clientBeanName": f"ai_client_{resolution.client_id}", "resolutionSource": resolution.source,
                    "costMillis": int((time.perf_counter() - started) * 1000), "errorMessage": ""}
        except Exception as exc:
            return {"role": role, "success": False, "fallback": True, "content": "", "rawContent": "",
                    "clientBeanName": f"ai_client_{resolution.client_id}", "resolutionSource": resolution.source,
                    "costMillis": int((time.perf_counter() - started) * 1000), "errorMessage": str(exc)}

    async def _configured_complete(self, config: dict[str, Any], prompt: str, default_system: str) -> str:
        root = str(config["baseUrl"]).rstrip("/")
        path = str(config.get("completionsPath") or "/v1/chat/completions")
        system = str(config.get("systemPrompt") or default_system)
        headers = {"Authorization": f"Bearer {config['apiKey']}"} if config.get("apiKey") else {}
        advisors = sorted(config.get("advisors") or [], key=lambda item: int(item.get("orderNum") or 0))
        rag_context: list[dict[str, Any]] = []
        for advisor in advisors:
            if advisor.get("advisorType") == "RagAnswer":
                parameters = advisor.get("extParam") or {}
                rag_context.extend(await self.rag.search(prompt, max(1, int(parameters.get("topK") or 4))))
        if rag_context:
            system += "\n\nRetrieved knowledge context (use only when relevant):\n" + json.dumps(
                rag_context, ensure_ascii=False)
        memory_limit = max((int((item.get("extParam") or {}).get("maxMessages") or 0)
                            for item in advisors if item.get("advisorType") == "ChatMemory"), default=0)
        memory_key = str(config.get("clientId") or "default")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if memory_limit:
            messages.extend(self._memory.get(memory_key, [])[-memory_limit:])
        messages.append({"role": "user", "content": prompt})
        mcp_tools, mcp_clients = await self._mcp_tool_definitions(config.get("mcpTools") or [])
        timeout = httpx.Timeout(self.settings.integration_timeout_seconds,
                                connect=self.settings.integration_connect_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            content = ""
            for _ in range(8):
                payload: dict[str, Any] = {"model": config["model"], "messages": messages, "temperature": 0.1}
                if mcp_tools:
                    payload["tools"] = mcp_tools
                    payload["tool_choice"] = "auto"
                response = await client.post(root + "/" + path.lstrip("/"), headers=headers, json=payload)
                response.raise_for_status()
                message = response.json().get("choices", [{}])[0].get("message", {})
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = str(message.get("content") or "")
                    break
                messages.append({"role": "assistant", "content": message.get("content"),
                                 "tool_calls": tool_calls})
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        result = await mcp_clients[name].call_tool(name, arguments)
                        tool_content = json.dumps(result, ensure_ascii=False, default=str)
                    except Exception as exc:
                        tool_content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                     "name": name, "content": tool_content})
        if not str(content).strip():
            raise RuntimeError("ChatClient returned blank content.")
        if memory_limit:
            history = self._memory.setdefault(memory_key, [])
            history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": str(content)}])
            del history[:-memory_limit]
        return str(content)

    @staticmethod
    async def _mcp_tool_definitions(configs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        clients: dict[str, Any] = {}
        for config in configs:
            client = mcp_client_from_config(config)
            for tool in await client.list_tools():
                name = str(tool.get("name") or "")
                if not name or name in clients:
                    continue
                clients[name] = client
                definitions.append({"type": "function", "function": {
                    "name": name, "description": tool.get("description") or "",
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                }})
        return definitions, clients
