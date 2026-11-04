"""Repository quality and release checks used by CI and local release gates."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = (
    "database",
    "graph",
    "harness",
    "legacy",
    "ops",
    "prompts",
    "tests",
    "tools",
    "ui",
)
TEXT_SUFFIXES = {".py", ".yml", ".yaml", ".toml", ".md", ".sh"}
SECRET_PATTERNS = (
    re.compile(r"\bgsk_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def source_files() -> list[Path]:
    paths = [ROOT / "app.py", ROOT / "cli.py"]
    for directory in SOURCE_DIRS:
        paths.extend((ROOT / directory).rglob("*"))
    return [path for path in paths if path.is_file() and path.suffix in TEXT_SUFFIXES]


def check_format() -> list[str]:
    errors = []
    for path in source_files():
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            errors.append(f"{path.relative_to(ROOT)}: missing final newline")
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.rstrip("\r\n").endswith((" ", "\t")):
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: trailing whitespace")
    return errors


def check_static() -> list[str]:
    errors = []
    for path in source_files():
        if path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as error:
                errors.append(f"{path.relative_to(ROOT)}:{error.lineno}: {error.msg}")
    return errors


def check_release_manifest() -> list[str]:
    required = (
        ROOT / ".env.example",
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / "deploy" / "Dockerfile",
        ROOT / "deploy" / "docker-compose.yml",
        ROOT / "docs" / "operations.md",
    )
    errors = [f"missing release file: {path.relative_to(ROOT)}" for path in required if not path.is_file()]
    release_inputs = source_files() + [
        ROOT / ".env.example",
        ROOT / "deploy" / "Dockerfile",
        ROOT / "deploy" / "docker-compose.yml",
        ROOT / "deploy" / "entrypoint.sh",
    ]
    for path in release_inputs:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            errors.append(f"possible secret in release input: {path.relative_to(ROOT)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    errors = check_format() + check_static()
    if args.release:
        errors.extend(check_release_manifest())
    if errors:
        print("\n".join(errors))
        return 1
    print("CI checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
