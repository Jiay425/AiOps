"""Compare the legacy Spring controller surface with the FastAPI application."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ops_autoagent.api import app


ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS = ROOT / "legacy-spring-ai" / "ops-autoagent-trigger" / "src" / "main" / "java"


def normalized(path: str) -> str:
    value = re.sub(r"/+", "/", "/" + path.strip("/"))
    return re.sub(r"\{[^}]+}", "{}", value)


def legacy_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    class_pattern = re.compile(r'@RequestMapping\("([^"]+)"\).*?public\s+class', re.DOTALL)
    annotation = re.compile(r"@(GetMapping|PostMapping|RequestMapping)\s*\(([^)]*)\)")
    for path in CONTROLLERS.rglob("*Controller.java"):
        text = path.read_text(encoding="utf-8", errors="replace")
        class_match = class_pattern.search(text)
        if not class_match:
            continue
        base = class_match.group(1)
        for match in annotation.finditer(text, class_match.end()):
            kind, arguments = match.groups()
            route_match = re.search(r'(?:value\s*=\s*)?"([^"]*)"', arguments)
            relative = route_match.group(1) if route_match else ""
            if kind == "GetMapping":
                method = "GET"
            elif kind == "PostMapping":
                method = "POST"
            else:
                method_match = re.search(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)", arguments)
                if not method_match:
                    continue
                method = method_match.group(1)
            routes.add((method, normalized(base + "/" + relative)))
    return routes


def python_routes() -> set[tuple[str, str]]:
    return {(method, normalized(route.path)) for route in app.routes
            for method in (getattr(route, "methods", None) or set())
            if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}}


def main() -> int:
    legacy, current = legacy_routes(), python_routes()
    missing, extra = sorted(legacy - current), sorted(current - legacy)
    print(json.dumps({"legacyRoutes": len(legacy), "pythonRoutes": len(current), "missing": missing,
                      "extra": extra}, ensure_ascii=False, indent=2))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
