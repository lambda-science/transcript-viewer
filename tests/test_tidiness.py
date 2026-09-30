"""Code nothing reaches.

Refactors leave this behind: a rename left a .tnode rule styling nothing, a
removed guessing function took three helpers with it, and a cut range swallowed
drawCrumb. It is checked rather than remembered.

The page's JavaScript and CSS are covered in test_style.py; this is the Python.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(
    p for p in (ROOT / "src").rglob("*.py") if "__pycache__" not in p.parts
)
EVERYTHING = "\n".join(
    p.read_text() for p in SOURCES + sorted((ROOT / "tests").rglob("*.py"))
)


def _defined(path: Path):
    """Every function and class a module defines, with its line."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_nothing_is_defined_and_never_used(path):
    orphans = [
        f"{name} (line {line})"
        for name, line in _defined(path)
        # A dunder is called by the language; main is called by the entry point.
        # http.server calls do_GET and log_message; nothing here does.
        if not name.startswith(("__", "do_"))
        and name not in ("main", "log_message")
        and len(re.findall(rf"\b{re.escape(name)}\b", EVERYTHING)) <= 1
    ]
    assert not orphans, f"{path.name} defines what nothing calls: {', '.join(orphans)}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_import_is_left_behind(path):
    text = path.read_text()
    body = "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().startswith(("import ", "from "))
    )
    unused = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        # __future__ is a compiler directive, not a name anything reads.
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        for alias in node.names:
            bound = (alias.asname or alias.name).split(".")[0]
            if bound != "*" and not re.search(rf"\b{re.escape(bound)}\b", body):
                unused.append(f"{bound} (line {node.lineno})")
    assert not unused, f"{path.name} imports what it never uses: {', '.join(unused)}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_constant_is_left_behind(path):
    dead = [
        target.id
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id.isupper()
        and len(re.findall(rf"\b{re.escape(target.id)}\b", EVERYTHING)) <= 1
    ]
    assert not dead, f"{path.name} defines constants nothing reads: {', '.join(dead)}"


def test_what_is_published_still_depends_on_a_version_not_a_path():
    """Remote installs need a version, not a checkout-relative path."""
    project = (ROOT / "pyproject.toml").read_text()
    declared = project.split("[project.optional-dependencies]")[0]

    assert 'dependencies = ["atif-make>=' in declared, (
        "the published dependency is the version range, not the local path"
    )
