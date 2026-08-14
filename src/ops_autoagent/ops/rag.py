from __future__ import annotations

import json
import hashlib
import math
import re
import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings


@dataclass
class RunbookChunk:
    chunk_id: str
    document: str
    heading: str
    content: str
    index: int
    document_hash: str = ""
    chunk_hash: str = ""
    count: int = 0
    title: str = ""
    category: str = "general"
    document_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"chunkId": self.chunk_id, "document": self.document, "heading": self.heading,
                "sectionTitle": self.heading, "content": self.content, "index": self.index,
                "chunkIndex": self.index, "chunkCount": self.count, "title": self.title,
                "category": self.category, "documentVersion": self.document_version,
                "documentHash": self.document_hash, "chunkHash": self.chunk_hash,
                "source": "ops-runbook", "ingestPipeline": "markdown-section-chunker-v2"}


class MarkdownChunker:
    def __init__(self, size: int = 1200, overlap: int = 150):
        self.size, self.overlap = max(800, size), max(0, min(overlap, size // 2))

    def chunk(self, path: Path) -> list[RunbookChunk]:
        text = path.read_text(encoding="utf-8", errors="replace")
        document_hash = hashlib.sha256(text.encode()).hexdigest()
        version = document_hash[:12]
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        category = self._category(path.name, text)
        pending: list[tuple[str, str]] = []
        heading, current = "overview", ""
        for line in text.splitlines():
            if line.startswith("## ") and current:
                pending.append((heading, current.strip()))
                current, heading = "", line[3:].strip()
            elif line.startswith("# "):
                heading = line[2:].strip()
            elif line.startswith("## "):
                heading = line[3:].strip()
            current += line + "\n"
            if len(current) >= self.size:
                pending.append((heading, current.strip()))
                current = ""
        if current:
            pending.append((heading, current.strip()))
        count = len(pending)
        return [RunbookChunk(f"{path.stem}-v{version}-chunk-{index}", path.name, section, content, index,
                             document_hash, hashlib.sha256(content.encode()).hexdigest(), count, title,
                             category, version)
                for index, (section, content) in enumerate(pending) if content]

    @staticmethod
    def _category(file_name: str, content: str) -> str:
        name = file_name.lower()
        filename_rules = [
            (("connection-pool", "slow-sql", "deadlock"), "database"), (("redis", "cache"), "cache"),
            (("jvm",), "jvm"), (("rpc",), "downstream"), (("mq",), "mq"), (("gateway",), "gateway"),
            (("thread-pool",), "thread_pool"), (("cpu",), "system"),
            (("kubernetes", "crashloop"), "kubernetes"), (("payment",), "payment"),
            (("release", "gray"), "release"), (("observability",), "observability"),
            (("sufficiency",), "policy"), (("500",), "application"),
        ]
        for markers, category in filename_rules:
            if any(marker in name for marker in markers):
                return category
        text = f"{file_name}\n{content}".lower()
        content_rules = [
            (("hikari", "jdbc", "database"), "database"), (("redis", "cache"), "cache"),
            (("full gc", "outofmemory"), "jvm"), (("dubbo", "rpc", "downstream"), "downstream"),
            (("mq", "kafka", "rocketmq"), "mq"), (("payment", "callback"), "payment"),
            (("500", "exception"), "application"),
        ]
        return next((category for markers, category in content_rules if any(marker in text for marker in markers)),
                    "general")


class RunbookRagService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.chunker = MarkdownChunker(settings.ops_runbook_chunk_size, settings.ops_runbook_chunk_overlap)

    async def search(self, query: str, limit: int = 4, mode: str = "HYBRID_RAG") -> list[dict[str, Any]]:
        keyword = self._keyword_search(query, max(limit * 4, 20))
        vector_enabled = self.settings.ops_runbook_vector_enabled and self.settings.ops_runbook_hybrid_enabled
        vector = await self._vector_search(query, max(limit * 4, 20)) if vector_enabled and mode != "KEYWORD_ONLY" else []
        combined = keyword if mode == "KEYWORD_ONLY" else (vector if mode == "VECTOR_ONLY" else self._rrf(keyword, vector))
        candidates = combined[:max(limit, self.settings.ops_runbook_rerank_candidate_top_n)]
        endpoint_requires_key = "api.pie-xian.com" in self.settings.ops_runbook_rerank_endpoint
        rerank_ready = (self.settings.ops_runbook_rerank_enabled and self.settings.ops_runbook_rerank_endpoint
                        and (self.settings.ops_runbook_rerank_api_key or not endpoint_requires_key))
        if rerank_ready:
            try:
                candidates = await self._rerank(query, candidates, limit)
            except (httpx.HTTPError, KeyError, IndexError, ValueError):
                candidates = candidates[:limit]
        return candidates[:limit]

    def governance(self) -> dict[str, Any]:
        documents = list(self.settings.ops_runbook_path.glob("*.md")) if self.settings.ops_runbook_path.exists() else []
        details = []
        for path in sorted(documents):
            chunks = self.chunker.chunk(path)
            content = path.read_bytes()
            details.append({"runbookId": path.stem, "title": path.stem, "category": path.stem.split("-")[0],
                            "path": str(path), "documentVersion": hashlib.sha256(content).hexdigest()[:12],
                            "documentHash": hashlib.sha256(content).hexdigest(), "chunks": len(chunks), "bytes": len(content),
                            "sections": [{"chunkId": item.chunk_id, "chunkIndex": item.index, "sectionTitle": item.heading,
                                          "chunkHash": item.chunk_hash, "chars": len(item.content)} for item in chunks]})
        chunk_count = sum(item["chunks"] for item in details)
        categories: dict[str, int] = {}
        for item in details:
            categories[item["category"]] = categories.get(item["category"], 0) + 1
        return {"status": "READY" if self.settings.ops_runbook_path.exists() else "RUNBOOK_PATH_MISSING",
                "basePath": str(self.settings.ops_runbook_path.resolve()), "chunkPipeline": "markdown-section-chunker-v2",
                "maxChunkChars": self.settings.ops_runbook_chunk_size, "documentCount": len(documents),
                "documents": len(documents), "chunkCount": chunk_count, "chunks": chunk_count,
                "totalBytes": sum(item["bytes"] for item in details), "categoryDistribution": categories,
                "thinDocuments": [item for item in details if item["chunks"] < 3 or item["bytes"] < 2500],
                "documentVersions": details,
                "hybridEnabled": self.settings.ops_runbook_hybrid_enabled, "vectorConfigured": bool(self.settings.pgvector_url),
                "rerankEnabled": self.settings.ops_runbook_rerank_enabled}

    async def index(self, rebuild: bool | None = None) -> dict[str, Any]:
        rebuild = self.settings.ops_runbook_vector_rebuild_on_startup if rebuild is None else rebuild
        if not self.settings.pgvector_url:
            return {"status": "SKIPPED", "reason": "PGVECTOR_URL is blank", "documents": 0, "chunksIndexed": 0}
        table = self.settings.pgvector_table
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("Unsafe PGVector table name")
        paths = sorted(self.settings.ops_runbook_path.glob("*.md")) if self.settings.ops_runbook_path.exists() else []
        chunks = [chunk for path in paths for chunk in self.chunker.chunk(path)]
        import asyncpg
        connection = await asyncpg.connect(self.settings.pgvector_url, user=self.settings.pgvector_username,
                                           password=self.settings.pgvector_password)
        indexed = skipped = 0
        try:
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            if rebuild:
                await connection.execute(f"DROP TABLE IF EXISTS {table}")
            await connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id UUID PRIMARY KEY, content TEXT NOT NULL, metadata JSONB, embedding VECTOR({self.settings.ops_embedding_dimensions}))")
            if self.settings.ops_runbook_vector_schema_check_on_startup:
                declared = await connection.fetchval(
                    "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid=a.attrelid WHERE c.relname=$1 AND a.attname='embedding' "
                    "AND a.attnum > 0 AND NOT a.attisdropped", table)
                expected = f"vector({self.settings.ops_embedding_dimensions})"
                if declared and str(declared).lower() != expected:
                    raise RuntimeError(f"PGVector dimension mismatch: table={declared}, configured={expected}")
            if rebuild:
                await connection.execute(f"DELETE FROM {table} WHERE metadata ->> 'source' = 'ops-runbook'")
            batch_size = max(1, self.settings.ops_runbook_vector_index_batch_size)
            for offset in range(0, len(chunks), batch_size):
                for chunk in chunks[offset:offset + batch_size]:
                    chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.chunk_id))
                    exists = await connection.fetchval(
                        f"SELECT COUNT(1) FROM {table} WHERE metadata ->> 'chunkId'=$1 AND metadata ->> 'documentHash'=$2 AND metadata ->> 'chunkHash'=$3",
                        chunk.chunk_id, chunk.document_hash, chunk.chunk_hash)
                    if exists:
                        skipped += 1
                        continue
                    last_error: Exception | None = None
                    for attempt in range(max(1, self.settings.ops_runbook_vector_index_batch_retries)):
                        try:
                            embedding = await self._embedding(chunk.content)
                            if not embedding:
                                raise RuntimeError("Embedding provider is not configured or returned no vector")
                            if len(embedding) != self.settings.ops_embedding_dimensions:
                                raise RuntimeError(
                                    f"Embedding dimension mismatch: provider={len(embedding)}, "
                                    f"configured={self.settings.ops_embedding_dimensions}")
                            metadata = {"source": "ops-runbook", "runbookId": Path(chunk.document).stem,
                                        "title": chunk.title, "category": chunk.category,
                                        "path": chunk.document, "documentVersion": chunk.document_version,
                                        "documentHash": chunk.document_hash,
                                        "ingestPipeline": "markdown-section-chunker-v2",
                                        "chunkId": chunk.chunk_id, "chunkIndex": chunk.index,
                                        "chunkCount": chunk.count, "chunkSection": chunk.heading,
                                        "chunkHash": chunk.chunk_hash}
                            await connection.execute(
                                f"INSERT INTO {table}(id,content,metadata,embedding) VALUES($1,$2,$3::jsonb,$4::vector) "
                                "ON CONFLICT(id) DO UPDATE SET content=EXCLUDED.content,metadata=EXCLUDED.metadata,embedding=EXCLUDED.embedding",
                                uuid.UUID(chunk_id), chunk.content, json.dumps(metadata), json.dumps(embedding))
                            indexed += 1
                            last_error = None
                            break
                        except (httpx.HTTPError, RuntimeError, KeyError, IndexError) as exc:
                            last_error = exc
                            if attempt + 1 < max(1, self.settings.ops_runbook_vector_index_batch_retries):
                                await asyncio.sleep(min(2 ** attempt, 5))
                    if last_error:
                        raise last_error
        finally:
            await connection.close()
        return {"status": "COMPLETED", "documents": len(paths), "chunksTotal": len(chunks),
                "chunksIndexed": indexed, "chunksSkippedExistingVersion": skipped, "vectorTableName": table,
                "ingestPipeline": "markdown-section-chunker-v2", "rebuild": rebuild}

    def _keyword_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        terms = list(dict.fromkeys(self._tokens(query)))
        chunks: list[RunbookChunk] = []
        if self.settings.ops_runbook_path.exists():
            for path in self.settings.ops_runbook_path.glob("*.md"):
                chunks.extend(self.chunker.chunk(path))
        frequencies = []
        for chunk in chunks:
            weighted = "\n".join((Path(chunk.document).stem, chunk.title, chunk.category,
                                    chunk.heading, chunk.content))
            tokens = self._tokens(weighted)
            frequencies.append({token: tokens.count(token) for token in set(tokens)})
        document_frequency = {term: sum(term in frequency for frequency in frequencies) for term in terms}
        average_length = sum(sum(frequency.values()) for frequency in frequencies) / max(1, len(frequencies))
        results = []
        for chunk, frequency in zip(chunks, frequencies, strict=True):
            score = 0.0
            hit_terms = []
            document_length = sum(frequency.values())
            for term in terms:
                tf = frequency.get(term, 0)
                if tf:
                    df = document_frequency[term]
                    inverse = math.log(1 + (len(chunks) - df + 0.5) / (df + 0.5))
                    denominator = tf + 1.5 * (1 - 0.75 + 0.75 * document_length / max(1, average_length))
                    score += inverse * (tf * 2.5) / denominator
                    hit_terms.append(term)
            if score:
                normalized = min(100, max(1, round(score * 12)))
                results.append({**chunk.to_dict(), "score": normalized, "keywordScore": normalized,
                                "bm25Score": normalized, "hybridScore": normalized,
                                "bm25RawScore": round(score, 8),
                                "retrievalMode": "BM25_CHUNK", "lexicalBoostScore": 0,
                                "rankExplanation": f"bm25Score={normalized}, rawBm25={score:.4f}, "
                                                   f"section={chunk.heading}, hitTerms={hit_terms[:12]}",
                                "source": "FILE_KEYWORD"})
        return sorted(results, key=lambda item: (-item["bm25RawScore"], item["chunkId"]))[:limit]

    @staticmethod
    def _tokens(text: str) -> list[str]:
        expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
        return [token for token in re.findall(r"[a-z0-9_\u4e00-\u9fff]+", expanded.lower())
                if len(token) >= 2]

    async def _embedding(self, text: str) -> list[float] | None:
        base_url = (self.settings.ops_embedding_base_url or self.settings.openai_base_url).rstrip("/")
        api_key = self.settings.ops_embedding_api_key or self.settings.openai_api_key
        if not base_url or not api_key:
            return None
        async with httpx.AsyncClient(timeout=self.settings.integration_timeout_seconds) as client:
            response = await client.post(f"{base_url}/embeddings", headers={"Authorization": f"Bearer {api_key}"},
                                         json={"model": self.settings.ops_embedding_model, "input": text})
            response.raise_for_status()
            return response.json()["data"][0]["embedding"]

    async def _vector_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not self.settings.pgvector_url:
            return []
        embedding = await self._embedding(query)
        if not embedding:
            return []
        table = self.settings.pgvector_table
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("Unsafe PGVector table name")
        try:
            import asyncpg
            connection = await asyncpg.connect(self.settings.pgvector_url, user=self.settings.pgvector_username,
                                               password=self.settings.pgvector_password)
            try:
                rows = await connection.fetch(
                    f"SELECT id, content, metadata, 1 - (embedding <=> $1::vector) AS score FROM {table} ORDER BY embedding <=> $1::vector LIMIT $2",
                    json.dumps(embedding), limit,
                )
            finally:
                await connection.close()
            return [{"chunkId": str(row["id"]), "content": row["content"], "metadata": row["metadata"],
                     "vectorScore": float(row["score"]), "source": "PGVECTOR"} for row in rows]
        except Exception:
            if self.settings.ops_runbook_vector_fallback_to_file:
                return []
            raise

    def _rrf(self, keyword: list[dict[str, Any]], vector: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        values: dict[str, dict[str, Any]] = {}
        k = self.settings.ops_runbook_hybrid_rrf_k
        for weight, results in ((self.settings.ops_runbook_hybrid_keyword_weight, keyword),
                                (self.settings.ops_runbook_hybrid_vector_weight, vector)):
            for rank, item in enumerate(results, 1):
                key = item.get("chunkId") or item.get("id")
                scores[key] = scores.get(key, 0.0) + weight / (k + rank)
                values[key] = {**values.get(key, {}), **item}
        return [{**values[key], "hybridScore": score} for key, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))]

    async def _rerank(self, query: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.settings.ops_runbook_rerank_api_key}"} if self.settings.ops_runbook_rerank_api_key else {}
        async with httpx.AsyncClient(timeout=self.settings.ops_runbook_rerank_timeout_ms / 1000) as client:
            response = await client.post(self.settings.ops_runbook_rerank_endpoint, headers=headers,
                                         json={"model": self.settings.ops_runbook_rerank_model, "query": query,
                                               "documents": [item.get("content", "") for item in candidates], "top_n": limit})
            response.raise_for_status()
            data = response.json().get("results", [])
        return [{**candidates[item["index"]], "rerankScore": item.get("relevance_score", 0)} for item in data]
