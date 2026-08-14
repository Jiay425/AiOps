"""Audit that every legacy public Java type has an explicit Python migration mapping."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy-spring-ai"
MAP = ROOT / "docs" / "migration-symbol-map.json"
EVIDENCE = ROOT / "docs" / "migration-symbol-evidence.json"


def java_types() -> set[str]:
    result = set()
    pattern = re.compile(r"public\s+(?:abstract\s+)?(?:class|interface|enum|record)\s+(\w+)")
    for path in LEGACY.rglob("*.java"):
        match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            result.add(match.group(1))
    return result


def python_symbols() -> set[str]:
    result = set()
    for path in (ROOT / "src" / "ops_autoagent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(ROOT / "src").with_suffix("").as_posix().replace("/", ".")
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                result.add(f"{module}:{node.name}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    mapping = json.loads(MAP.read_text(encoding="utf-8")) if MAP.exists() else {}
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    legacy, symbols = java_types(), python_symbols()
    missing = sorted(legacy - mapping.keys())
    invalid = sorted(f"{java} -> {target}" for java, target in mapping.items() if target not in symbols)
    missing_evidence = sorted(name for name in legacy if name not in evidence)
    invalid_evidence = sorted(name for name, item in evidence.items()
                              if not (ROOT / item.get("legacyFile", "")).is_file()
                              or not (ROOT / item.get("verification", "")).is_file()
                              or item.get("target") != mapping.get(name))
    print(json.dumps({"legacyTypes": len(legacy), "mappedTypes": len(mapping), "missing": missing,
                      "invalidTargets": invalid, "missingEvidence": missing_evidence,
                      "invalidEvidence": invalid_evidence}, ensure_ascii=False, indent=2))
    return 0 if (not missing and not invalid and not missing_evidence and not invalid_evidence) or args.allow_incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
