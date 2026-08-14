from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .config import Settings


class CheckpointerManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.backend = settings.langgraph_checkpoint_backend.lower().strip()
        self.saver: Any = InMemorySaver()
        self._context: Any = None

    async def start(self) -> Any:
        if self.backend == "memory":
            return self.saver
        if self.backend == "sqlite":
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            path = Path(self.settings.langgraph_checkpoint_path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._context = AsyncSqliteSaver.from_conn_string(str(path))
        elif self.backend == "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            if not self.settings.langgraph_checkpoint_postgres_url:
                raise ValueError("LANGGRAPH_CHECKPOINT_POSTGRES_URL is required for postgres checkpoints")
            self._context = AsyncPostgresSaver.from_conn_string(
                self.settings.langgraph_checkpoint_postgres_url.removeprefix("jdbc:"), pipeline=True)
        else:
            raise ValueError(f"Unsupported LangGraph checkpoint backend: {self.backend}")
        self.saver = await self._context.__aenter__()
        await self.saver.setup()
        return self.saver

    async def close(self) -> None:
        if self._context:
            await self._context.__aexit__(None, None, None)
            self._context = None

    def summary(self) -> dict[str, Any]:
        return {"backend": self.backend, "persistent": self.backend in {"sqlite", "postgres"},
                "path": str(self.settings.langgraph_checkpoint_path) if self.backend == "sqlite" else "",
                "postgresConfigured": bool(self.settings.langgraph_checkpoint_postgres_url)}
