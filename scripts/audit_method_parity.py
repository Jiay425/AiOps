"""Fail until every concrete legacy Java method has behavior evidence in Python.

The audit deliberately does not infer parity from a class/type mapping. Each entry in
docs/migration-method-map.json must identify a Python target and at least one test.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy-spring-ai"
METHOD_MAP = ROOT / "docs" / "migration-method-map.json"
DECLARATION = re.compile(
    r"(?m)^\s*(?:public|protected|private)\s+"
    r"(?:(?:static|final|synchronized|abstract|default|native)\s+)*"
    r"(?:<[^>{};]+>\s+)?(?:[\w.$<>?,\[\]\s]+\s+)?(\w+)\s*\(([^;{}()]*)\)"
    r"\s*(?:throws\s+[\w.$,\s]+)?\s*(?:\{|;)",
)


def _without_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\r\n]*", "", source)


def java_methods() -> dict[str, dict[str, Any]]:
    methods: dict[str, dict[str, Any]] = {}
    for path in LEGACY.rglob("*.java"):
        relative = path.relative_to(ROOT).as_posix()
        source = _without_comments(path.read_text(encoding="utf-8", errors="replace"))
        class_name = path.stem
        for match in DECLARATION.finditer(source):
            name, parameters = match.group(1), match.group(2).strip()
            # Exclude control-flow false positives and Lombok-style builder invocations.
            if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
                continue
            parameter_count = 0 if not parameters else len([part for part in parameters.split(",") if part.strip()])
            line = source.count("\n", 0, match.start()) + 1
            key = f"{relative}::{class_name}.{name}/{parameter_count}@{line}"
            methods[key] = {"legacyFile": relative, "class": class_name, "method": name,
                            "parameterCount": parameter_count, "line": line}
    return methods


def python_symbols() -> set[str]:
    symbols: set[str] = set()
    for path in (ROOT / "src" / "ops_autoagent").rglob("*.py"):
        module = path.relative_to(ROOT / "src").with_suffix("").as_posix().replace("/", ".")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(f"{module}:{node.name}")
    return symbols


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--show", type=int, default=30)
    args = parser.parse_args()
    methods = java_methods()
    mapping = json.loads(METHOD_MAP.read_text(encoding="utf-8")) if METHOD_MAP.exists() else {}
    symbols = python_symbols()
    missing = sorted(set(methods) - set(mapping))
    invalid: list[str] = []
    for key, record in mapping.items():
        targets = record.get("targets", []) if isinstance(record, dict) else []
        tests = record.get("tests", []) if isinstance(record, dict) else []
        if key not in methods or not targets or not tests:
            invalid.append(key)
            continue
        if any(target not in symbols for target in targets):
            invalid.append(key)
            continue
        if any(not (ROOT / test).is_file() for test in tests):
            invalid.append(key)
    result = {"legacyMethods": len(methods), "mappedMethods": len(mapping),
              "verifiedMethods": len(methods) - len(missing) - len(invalid),
              "missingCount": len(missing), "invalidCount": len(invalid),
              "missingSample": missing[:max(0, args.show)], "invalidSample": sorted(invalid)[:max(0, args.show)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    complete = not missing and not invalid
    return 0 if complete or args.allow_incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
