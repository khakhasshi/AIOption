#!/usr/bin/env python3
"""Fail when a FastAPI endpoint is absent from the checked-in API reference."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ai_option_scanner"
REFERENCE = ROOT / "docs" / "backend-api-reference.md"


def discover_routes() -> list[tuple[str, str, Path, int]]:
    routes: list[tuple[str, str, Path, int]] = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                owner = decorator.func.value
                if not isinstance(owner, ast.Name) or owner.id != "app":
                    continue
                if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                    continue
                route = str(decorator.args[0].value)
                method = decorator.func.attr.upper()
                if route.startswith("/api/") and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    routes.append((method, route, path.relative_to(ROOT), node.lineno))
    return sorted(routes)


def main() -> int:
    reference = REFERENCE.read_text(encoding="utf-8")
    routes = discover_routes()
    missing = [route for route in routes if f"### `{route[0]} {route[1]}`" not in reference]
    print(f"API routes: {len(routes)}; undocumented: {len(missing)}")
    for method, route, path, line in missing:
        print(f"  {method} {route} ({path}:{line})")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
