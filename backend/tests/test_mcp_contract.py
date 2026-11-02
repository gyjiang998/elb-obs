from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from backend.mcp_contract import _decorated_mcp_tool_name, platform_mcp_contract, platform_mcp_server_id
from backend.skill_versions import resolve_skill_profile


def registered_parameter_names(app_root: Path) -> set[str]:
    """Exempt actual registered-tool parameters, not arbitrary device_* names."""
    tree = ast.parse((app_root / "backend/kimi_mcp_server.py").read_text(encoding="utf-8"))
    return {
        argument.arg
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _decorated_mcp_tool_name(node) is not None
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    }


class PlatformMcpContractTests(unittest.TestCase):
    def test_contract_reads_decorated_tool_names_in_registration_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            facade = root / "backend" / "kimi_mcp_server.py"
            facade.parent.mkdir(parents=True)
            facade.write_text(
                "@mcp.tool()\ndef first_tool():\n    pass\n\n"
                "@mcp.tool(name='public_second')\ndef second_tool():\n    pass\n",
                encoding="utf-8",
            )

            contract = platform_mcp_contract(root)

            self.assertEqual(contract["tool_names"], ["first_tool", "public_second"])
            self.assertEqual(contract["tool_count"], 2)

    def test_server_identity_stays_stable_when_the_executable_facade_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            facade = root / "backend" / "kimi_mcp_server.py"
            facade.parent.mkdir(parents=True)
            facade.write_text("@mcp.tool()\ndef one():\n    pass\n", encoding="utf-8")
            first = platform_mcp_server_id("run_1234567890", root)
            first_contract = platform_mcp_contract(root)
            facade.write_text("@mcp.tool()\ndef two():\n    pass\n", encoding="utf-8")
            second = platform_mcp_server_id("run_1234567890", root)
            second_contract = platform_mcp_contract(root)

            self.assertEqual(first, second)
            self.assertNotEqual(first_contract["facade_sha256"], second_contract["facade_sha256"])
            self.assertNotEqual(first_contract["tool_catalog_sha256"], second_contract["tool_catalog_sha256"])
            self.assertTrue(first.endswith("34567890"))

    def test_active_skill_tool_references_exist_in_the_mcp_catalog(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        profile = resolve_skill_profile(None)
        content = "\n".join(
            str(entry.get("prompt") or entry.get("addendum") or "")
            for entry in profile["skills"].values()
            if isinstance(entry, dict)
        )
        referenced = set(re.findall(
            r"\b(?:application_flow|experimental_flow|scenario_capture|parallel_modeling|parallel_job|code_graph|reasoning|device|build|platform|knowledge|skills|experience|ui)_[a-z0-9_]+\b",
            content,
        ))
        registered = set(platform_mcp_contract(app_root)["tool_names"])

        self.assertIn("application_flow_compile_mermaid", referenced)
        # Tool parameters share prefixes with tool names. Their exemptions come
        # from the executable facade, so a typo is still an unknown reference.
        parameter_names = registered_parameter_names(app_root)
        self.assertEqual(referenced - registered - parameter_names, set())

    def test_parameter_exemptions_are_derived_only_from_registered_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            facade = root / "backend/kimi_mcp_server.py"
            facade.parent.mkdir(parents=True)
            facade.write_text(
                "@mcp.tool(name='device_probe')\n"
                "def probe(device_serial, device_lease_id='', *, logical_device_id=''):\n    pass\n\n"
                "def private_helper(device_unknown_tool):\n    pass\n",
                encoding="utf-8",
            )
            registered = set(platform_mcp_contract(root)["tool_names"])
            parameters = registered_parameter_names(root)
            self.assertEqual(parameters, {"device_serial", "device_lease_id", "logical_device_id"})
            references = {"device_probe", "device_lease_id", "device_unknown_tool"}
            self.assertEqual(references - registered - parameters, {"device_unknown_tool"})


if __name__ == "__main__":
    unittest.main()
