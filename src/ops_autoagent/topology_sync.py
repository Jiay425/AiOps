"""CLI boundary for synchronizing a CMDB/deploy snapshot into Neo4j.

This runs outside a LangGraph task: graph agents may query topology but never
write it.  A production scheduler can invoke the same command with a signed,
validated CMDB export.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import get_settings
from .topology import Neo4jTopologyService, TopologyUnavailable


async def sync(snapshot_path: Path) -> dict[str, int]:
    value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("topology snapshot must be a JSON object")
    service = Neo4jTopologyService(get_settings())
    if not await service.start(required=True):
        raise TopologyUnavailable("Neo4j topology service is not available")
    try:
        return await service.sync_snapshot(value)
    finally:
        await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize a validated topology snapshot into Neo4j")
    parser.add_argument("snapshot", type=Path, help="JSON snapshot containing services, dependencies, and changes")
    args = parser.parse_args()
    if not args.snapshot.is_file():
        parser.error(f"snapshot does not exist: {args.snapshot}")
    print(json.dumps(asyncio.run(sync(args.snapshot)), ensure_ascii=False))


if __name__ == "__main__":
    main()
