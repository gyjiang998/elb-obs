"""Source compatibility contracts for the Python 3.10 analysis server.

The Desktop can use a newer embedded Python, while ``desktop_platform_rpc``
runs with the interpreter installed on the analysis server.  Keep this test
source based so a newer developer interpreter cannot hide an import-time
failure caused by a newer standard-library API.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"


def _datetime_utc_references(tree: ast.AST) -> list[tuple[int, str]]:
    """Return uses of ``datetime.UTC``, including aliased module imports."""
    module_aliases: set[str] = set()
    references: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "datetime":
                    module_aliases.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for imported in node.names:
                if imported.name == "UTC":
                    references.append((node.lineno, "from datetime import UTC"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "UTC"
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ):
            references.append((node.lineno, f"{node.value.id}.UTC"))
    return sorted(set(references))


class Python310CompatibilityTests(unittest.TestCase):
    def test_backend_production_sources_use_python310_grammar_and_utc_api(self) -> None:
        failures: list[str] = []
        sources = sorted(
            path for path in BACKEND_ROOT.rglob("*.py")
            if "tests" not in path.relative_to(BACKEND_ROOT).parts
        )
        self.assertIn(BACKEND_ROOT / "tools" / "desktop_platform_rpc.py", sources)
        self.assertIn(BACKEND_ROOT / "core.py", sources)

        for path in sources:
            relative = path.relative_to(REPOSITORY_ROOT)
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(relative),
                    feature_version=10,
                )
            except SyntaxError as exc:
                failures.append(
                    f"{relative}:{exc.lineno or 1}: syntax requires Python newer than 3.10: {exc.msg}"
                )
                continue
            for line, expression in _datetime_utc_references(tree):
                failures.append(
                    f"{relative}:{line}: {expression} requires Python 3.11; use timezone.utc"
                )

        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_datetime_utc_guard_detects_import_and_module_attribute_forms(self) -> None:
        direct = ast.parse("from datetime import UTC as utc\n")
        qualified = ast.parse("import datetime as clock\nvalue = clock.UTC\n")

        self.assertEqual(_datetime_utc_references(direct), [(1, "from datetime import UTC")])
        self.assertEqual(_datetime_utc_references(qualified), [(2, "clock.UTC")])


if __name__ == "__main__":
    unittest.main()
