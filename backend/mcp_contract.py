"""Stable identity and executable catalog for the platform MCP facade."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


PLATFORM_MCP_ID = "platform_mcp"
MCP_FACADE_RELATIVE_PATH = Path("backend") / "kimi_mcp_server.py"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decorated_mcp_tool_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "tool"
            and isinstance(target.value, ast.Name)
            and target.value.id == "mcp"
        ):
            continue
        if call is not None:
            for keyword in call.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    return keyword.value.value
        return node.name
    return None


def platform_mcp_contract(app_root: Path) -> dict[str, Any]:
    """Read the executable facade and derive one deterministic tool contract."""

    facade_path = app_root.resolve() / MCP_FACADE_RELATIVE_PATH
    source = facade_path.read_bytes()
    tree = ast.parse(source, filename=str(facade_path))
    tool_names = [
        tool_name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for tool_name in [_decorated_mcp_tool_name(node)]
        if tool_name is not None
    ]
    catalog_payload = json.dumps(tool_names, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": "platform_mcp_contract/v1",
        "platform_mcp": PLATFORM_MCP_ID,
        "facade_path": MCP_FACADE_RELATIVE_PATH.as_posix(),
        "facade_sha256": _sha256_bytes(source),
        "tool_catalog_sha256": _sha256_bytes(catalog_payload),
        "tool_count": len(tool_names),
        "tool_names": tool_names,
    }


def platform_mcp_server_id(run_id: str, app_root: Path) -> str:
    # Keep one stable MCP identity for the lifetime of a durable Agent Run.
    # Tool/schema changes are tracked by the separate contract hashes and make
    # the PTY restart; putting a source hash in the server name breaks resumed
    # Agent sessions because they retain references to the previous identity.
    del app_root
    return f"heterogeneous_platform_{run_id[-8:]}"
