"""Validate the configured Agent CLI adapter catalog without starting FastAPI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from ..agent_cli_adapters import AgentCliAdapterError, AgentCliAdapterRegistry


def validate_catalog(
    *,
    manifest: Path | None,
    default_adapter: str,
    kimi_bin: Path,
    allow_missing_kimi: bool = False,
) -> dict[str, object]:
    if manifest is not None and not manifest.is_file():
        raise AgentCliAdapterError(
            "AGENT_CLI_MANIFEST_MISSING",
            f"Agent CLI manifest 不存在：{manifest}",
        )
    registry = AgentCliAdapterRegistry(
        kimi_binary=kimi_bin,
        kimi_environment_factory=lambda: os.environ.copy(),
        custom_manifest_path=manifest,
        default_adapter_id=default_adapter,
        tolerate_manifest_errors=False,
    )
    catalog = registry.catalog()
    selected = next(
        (item for item in catalog["adapters"] if item["adapter_id"] == default_adapter),
        None,
    )
    if selected is None:
        available_ids = [str(item["adapter_id"]) for item in catalog["adapters"]]
        raise AgentCliAdapterError(
            "AGENT_CLI_ADAPTER_NOT_FOUND",
            f"默认 Agent Adapter 未登记：{default_adapter}",
            details={"available_adapter_ids": available_ids},
        )
    if not selected["available"] and not (allow_missing_kimi and default_adapter == "kimi"):
        raise AgentCliAdapterError(
            "AGENT_CLI_BINARY_MISSING",
            str(selected.get("diagnostic") or f"默认 Agent Adapter 不可用：{default_adapter}"),
            details={"adapter_id": default_adapter},
        )
    return catalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--default-adapter", default="kimi")
    parser.add_argument("--kimi-bin", type=Path, required=True)
    parser.add_argument(
        "--allow-missing-kimi",
        action="store_true",
        help="Keep the core platform usable when the optional built-in Kimi CLI is absent.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = args.manifest.expanduser().resolve() if args.manifest else None
    try:
        catalog = validate_catalog(
            manifest=manifest,
            default_adapter=args.default_adapter,
            kimi_bin=args.kimi_bin.expanduser().resolve(),
            allow_missing_kimi=args.allow_missing_kimi,
        )
    except AgentCliAdapterError as exc:
        payload = {
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "manifest": str(manifest) if manifest else None,
            "default_adapter_id": args.default_adapter,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[FAIL] {exc.code}: {exc.message}", file=sys.stderr)
            if exc.details:
                print(f"       details={json.dumps(exc.details, ensure_ascii=False)}", file=sys.stderr)
        return 1

    adapters = list(catalog["adapters"])
    payload = {
        "status": "ready",
        "manifest": str(manifest) if manifest else None,
        "default_adapter_id": catalog["default_adapter_id"],
        "adapters": adapters,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Agent Adapter manifest: {manifest if manifest else '(built-in only)'}")
    print(f"Default Agent Adapter: {catalog['default_adapter_id']}")
    for adapter in adapters:
        state = "READY" if adapter["available"] else "UNAVAILABLE"
        marker = "default" if adapter["adapter_id"] == catalog["default_adapter_id"] else "registered"
        print(f"[{state}] {adapter['adapter_id']} ({adapter['display_name']}) - {marker}")
        if adapter.get("diagnostic"):
            print(f"        {adapter['diagnostic']}")
        warning = adapter.get("mcp_integration", {}).get("warning")
        if warning:
            print(f"        MCP: {warning}")
    custom_adapters = [adapter for adapter in adapters if str(adapter.get("source", "")).startswith("manifest:")]
    if manifest is not None and custom_adapters and catalog["default_adapter_id"] == "kimi":
        custom_ids = ", ".join(str(adapter["adapter_id"]) for adapter in custom_adapters)
        print(
            "[HINT] 自定义 Adapter 已登记，但后端默认项仍是 kimi。"
            f"如需切换，请设置 HETERO_AGENT_CLI_ADAPTER（可选：{custom_ids}）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
