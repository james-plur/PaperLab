#!/usr/bin/env python3
"""Fail when either independently deployable project imports the other."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = {
    "paperlab_local": ROOT / "paperlab_local",
    "paperlab_publish": ROOT / "paperlab_publish",
}


def imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, item.name) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def main() -> int:
    failures: list[str] = []
    for name, project in PROJECTS.items():
        forbidden = (set(PROJECTS) - {name}).pop()
        for path in project.rglob("*.py"):
            for line, module in imported_modules(path):
                if module == forbidden or module.startswith(forbidden + "."):
                    failures.append(f"{path.relative_to(ROOT)}:{line}: imports {module}")
    if failures:
        print("Cross-project runtime dependencies are forbidden:")
        print("\n".join(failures))
        return 1
    print("Boundary check passed: Local and Publish have no cross-project imports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
