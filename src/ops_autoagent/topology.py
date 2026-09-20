"""Neo4j-backed service topology and evidence correlation.

This module intentionally has no in-memory topology fallback.  A production
topology decision must come from Neo4j, while demo mode reports that topology
evidence is unavailable rather than silently inventing a graph.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from neo4j import AsyncGraphDatabase


SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT service_name_unique IF NOT EXISTS FOR (node:Service) REQUIRE node.name IS UNIQUE",
    "CREATE CONSTRAINT workload_name_unique IF NOT EXISTS FOR (node:Workload) REQUIRE node.uid IS UNIQUE",
    "CREATE CONSTRAINT pod_name_unique IF NOT EXISTS FOR (node:Pod) REQUIRE node.uid IS UNIQUE",
    "CREATE CONSTRAINT node_name_unique IF NOT EXISTS FOR (node:Node) REQUIRE node.name IS UNIQUE",
    "CREATE CONSTRAINT database_name_unique IF NOT EXISTS FOR (node:Database) REQUIRE node.name IS UNIQUE",
    "CREATE CONSTRAINT cache_name_unique IF NOT EXISTS FOR (node:Cache) REQUIRE node.name IS UNIQUE",
    "CREATE CONSTRAINT topic_name_unique IF NOT EXISTS FOR (node:Topic) REQUIRE node.name IS UNIQUE",
    "CREATE CONSTRAINT repository_url_unique IF NOT EXISTS FOR (node:Repository) REQUIRE node.url IS UNIQUE",
    "CREATE CONSTRAINT module_key_unique IF NOT EXISTS FOR (node:Module) REQUIRE node.key IS UNIQUE",
    "CREATE CONSTRAINT change_id_unique IF NOT EXISTS FOR (node:Change) REQUIRE node.changeId IS UNIQUE",
    "CREATE CONSTRAINT alert_id_unique IF NOT EXISTS FOR (node:Alert) REQUIRE node.alertId IS UNIQUE",
    "CREATE CONSTRAINT runbook_id_unique IF NOT EXISTS FOR (node:Runbook) REQUIRE node.runbookId IS UNIQUE",
    "CREATE INDEX change_timestamp IF NOT EXISTS FOR (node:Change) ON (node.timestamp)",
    "CREATE INDEX service_namespace IF NOT EXISTS FOR (node:Service) ON (node.namespace)",
)

# Labels and identifying properties are fixed in code rather than interpolated
# from a CMDB payload.  Neo4j parameters cannot represent labels safely, and
# this allow-list prevents a topology import from becoming arbitrary Cypher.
_NODE_KINDS: dict[str, tuple[str, str, str]] = {
    "service": ("Service", "name", "services"),
    "workload": ("Workload", "uid", "workloads"),
    "pod": ("Pod", "uid", "pods"),
    "node": ("Node", "name", "nodes"),
    "database": ("Database", "name", "databases"),
    "cache": ("Cache", "name", "caches"),
    "topic": ("Topic", "name", "topics"),
    "repository": ("Repository", "url", "repositories"),
    "module": ("Module", "key", "modules"),
    "runbook": ("Runbook", "runbookId", "runbooks"),
}
_RCA_CANDIDATE_LABELS = "candidate:Service OR candidate:Database OR candidate:Cache OR candidate:Topic"


class TopologyUnavailable(RuntimeError):
    """Raised when an operation explicitly requires a live Neo4j graph."""


class Neo4jTopologyService:
    """Owns schema setup, topology synchronization and read-only RCA queries."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.uri = str(getattr(settings, "neo4j_uri", "") or "").strip()
        self.username = str(getattr(settings, "neo4j_username", "neo4j") or "neo4j")
        self.password = str(getattr(settings, "neo4j_password", "") or "")
        self.database = str(getattr(settings, "neo4j_database", "neo4j") or "neo4j")
        self.enabled = bool(getattr(settings, "ops_topology_enabled", True))
        self.max_depth = max(1, min(int(getattr(settings, "ops_topology_max_depth", 5) or 5), 8))
        self.change_window_hours = max(1, min(int(getattr(settings, "ops_topology_change_window_hours", 24) or 24), 720))
        self.driver: Any | None = None

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.uri and self.password)

    async def start(self, *, required: bool = False) -> bool:
        if not self.configured:
            if required:
                raise TopologyUnavailable("Neo4j is required in production: configure NEO4J_URI and NEO4J_PASSWORD")
            return False
        self.driver = AsyncGraphDatabase.driver(
            self.uri, auth=(self.username, self.password),
            max_connection_pool_size=max(1, int(getattr(self.settings, "neo4j_max_connection_pool_size", 20) or 20)),
        )
        try:
            await self.driver.verify_connectivity()
            await self.ensure_schema()
            return True
        except Exception:
            await self.close()
            if required:
                raise
            return False

    async def close(self) -> None:
        if self.driver is not None:
            await self.driver.close()
            self.driver = None

    async def ensure_schema(self) -> None:
        driver = self._driver()
        async with driver.session(database=self.database) as session:
            for statement in SCHEMA_STATEMENTS:
                await (await session.run(statement)).consume()

    async def sync_snapshot(self, snapshot: dict[str, Any]) -> dict[str, int]:
        """Synchronize CMDB/deploy/Git data into Neo4j outside the agent execution path."""
        dependencies = [item for item in snapshot.get("dependencies", []) if isinstance(item, dict)]
        changes = [item for item in snapshot.get("changes", []) if isinstance(item, dict)]
        counts = {collection: 0 for _, _, collection in _NODE_KINDS.values()}
        driver = self._driver()
        async with driver.session(database=self.database) as session:
            for _, (label, identifier, collection) in _NODE_KINDS.items():
                for node in (item for item in snapshot.get(collection, []) if isinstance(item, dict)):
                    identity = str(node.get(identifier) or "").strip()
                    if not identity:
                        continue
                    props = {key: value for key, value in node.items() if key != identifier and value is not None}
                    await (await session.run(
                        f"MERGE (node:{label} {{{identifier}:$identity}}) "
                        "SET node += $props, node.updatedAt=datetime()", identity=identity, props=props)).consume()
                    counts[collection] += 1
            for dependency in dependencies:
                source, target = str(dependency.get("source") or "").strip(), str(dependency.get("target") or "").strip()
                if not source or not target:
                    continue
                source_kind = self._node_kind(dependency.get("sourceKind", "service"))
                target_kind = self._node_kind(dependency.get("targetKind", "service"))
                source_label, source_identifier, _ = _NODE_KINDS[source_kind]
                target_label, target_identifier, _ = _NODE_KINDS[target_kind]
                props = dependency.get("properties") if isinstance(dependency.get("properties"), dict) else {}
                await (await session.run(
                    f"MATCH (source:{source_label} {{{source_identifier}:$source}}) "
                    f"MATCH (target:{target_label} {{{target_identifier}:$target}}) "
                    "MERGE (source)-[edge:DEPENDS_ON]->(target) SET edge += $props, edge.updatedAt=datetime()",
                    source=source, target=target, props=props)).consume()
            for change in changes:
                change_id, target = str(change.get("changeId") or "").strip(), str(change.get("service") or change.get("target") or "").strip()
                if not change_id or not target:
                    continue
                target_kind = self._node_kind(change.get("targetKind", "service"))
                target_label, target_identifier, _ = _NODE_KINDS[target_kind]
                props = {key: value for key, value in change.items()
                         if key not in {"changeId", "service", "target", "targetKind"} and value is not None}
                timestamp = props.pop("timestamp", None) or datetime.now(timezone.utc).isoformat()
                await (await session.run(
                    f"MATCH (target:{target_label} {{{target_identifier}:$target}}) "
                    "MERGE (change:Change {changeId:$changeId}) SET change += $props, change.timestamp=datetime($timestamp) "
                    "MERGE (change)-[:AFFECTS]->(target)",
                    changeId=change_id, target=target, props=props, timestamp=str(timestamp))).consume()
        return {**counts, "dependencies": len(dependencies), "changes": len(changes)}

    async def topology(self, service: str) -> dict[str, Any]:
        service = self._service_name(service)
        driver = self._driver()
        query = """
        MATCH (service:Service {name:$service})
        OPTIONAL MATCH (service)-[:DEPENDS_ON]->(dependency)
        OPTIONAL MATCH (dependent:Service)-[:DEPENDS_ON]->(service)
        OPTIONAL MATCH path=(service)-[:DEPENDS_ON*1..MAX_DEPTH]->(leaf)
        WHERE NOT (leaf)-[:DEPENDS_ON]->()
        RETURN service { .name, .namespace, .tier, .criticality } AS service,
               collect(DISTINCT dependency.name) AS dependencies,
               collect(DISTINCT dependent.name) AS dependents,
               collect(DISTINCT [node IN nodes(path) | node.name])[0..20] AS dependencyPaths
        """.replace("MAX_DEPTH", str(self.max_depth))
        async with driver.session(database=self.database) as session:
            record = await (await session.run(query, service=service)).single()
        if not record or not record.get("service"):
            return {"status": "NOT_FOUND", "service": service, "directDependencies": [], "dependents": [], "dependencyPaths": []}
        return {"status": "READY", "service": record["service"],
                "directDependencies": [item for item in record["dependencies"] if item],
                "dependents": [item for item in record["dependents"] if item],
                "dependencyPaths": [item for item in record["dependencyPaths"] if item]}

    async def correlate(self, service: str, anomaly_signals: list[dict[str, Any]], trace_spans: list[str]) -> dict[str, Any]:
        """Build evidence-backed candidates using graph paths and change-time correlation."""
        service = self._service_name(service)
        since = datetime.now(timezone.utc) - timedelta(hours=self.change_window_hours)
        driver = self._driver()
        query = """
        MATCH path=(origin:Service {name:$service})-[:DEPENDS_ON*0..MAX_DEPTH]->(candidate)
        WHERE RCA_CANDIDATES
        WITH candidate, min(length(path)) AS distance,
             collect(DISTINCT [node IN nodes(path) | node.name])[0..10] AS paths
        OPTIONAL MATCH (change:Change)-[:AFFECTS]->(candidate)
        WHERE change.timestamp >= datetime($since)
        WITH candidate, distance, paths, collect(DISTINCT change { .changeId, .timestamp, .description, .type, .source }) AS changes
        OPTIONAL MATCH (upstream:Service)-[:DEPENDS_ON]->(origin)
        RETURN candidate.name AS candidate, candidate.criticality AS criticality, distance, paths, changes,
               collect(DISTINCT upstream.name) AS upstreamServices
        ORDER BY distance ASC, size(changes) DESC
        LIMIT 20
        """.replace("MAX_DEPTH", str(self.max_depth)).replace("RCA_CANDIDATES", _RCA_CANDIDATE_LABELS)
        async with driver.session(database=self.database) as session:
            rows = [record async for record in await session.run(
                query, service=service, since=since.isoformat())]
        candidates: list[dict[str, Any]] = []
        trace_text = " ".join(trace_spans).lower()
        anomaly_count = len([item for item in anomaly_signals if item.get("status") == "ANOMALY"])
        for row in rows:
            name = str(row["candidate"])
            trace_match = bool(re.search(rf"(?<![a-z0-9_-]){re.escape(name.lower())}(?![a-z0-9_-])", trace_text))
            changes = [self._json_safe(item) for item in row["changes"] if item and item.get("changeId")]
            distance = int(row["distance"] or 0)
            score = min(1.0, round(0.35 / (distance + 1) + (0.35 if changes else 0) +
                                   (0.20 if trace_match else 0) + min(0.10, anomaly_count * 0.02), 3))
            candidates.append({"service": name, "score": score, "distance": distance,
                               "graphPaths": row["paths"], "recentChanges": changes,
                               "traceCorrelated": trace_match, "rankingMethod": "graph_path+temporal_change+trace"})
        candidates.sort(key=lambda item: (-item["score"], item["distance"], item["service"]))
        topology = await self.topology(service)
        return {"status": "READY", "serviceName": service,
                "suspectedFaultDomain": candidates[0]["service"] if candidates else None,
                "impactedDependencies": [item["service"] for item in candidates[:5]],
                "upstreamServices": topology.get("dependents", []),
                "dependencyPaths": topology.get("dependencyPaths", []),
                "recentChanges": [change for item in candidates for change in item["recentChanges"]][:20],
                "rootCauseCandidates": candidates[:10],
                "topologyEvidence": {"anomalySignalCount": anomaly_count, "traceSpanCount": len(trace_spans),
                                     "changeWindowHours": self.change_window_hours, "source": "neo4j"}}

    def _driver(self) -> Any:
        if self.driver is None:
            raise TopologyUnavailable("Neo4j topology service is not connected")
        return self.driver

    @staticmethod
    def _node_kind(value: Any) -> str:
        normalized = str(value or "service").strip().lower()
        if normalized not in _NODE_KINDS:
            raise ValueError(f"unsupported topology node kind: {normalized}")
        return normalized

    @staticmethod
    def _service_name(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("service name is required")
        return normalized

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._json_safe(item) for item in value]
        if hasattr(value, "iso_format"):
            return value.iso_format()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value
