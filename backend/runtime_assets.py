"""Runtime assets derived from registered source connectors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import JsonRegistry, PlatformError, SourceConnectorManager, utc_now
from .tools.provision_openharmony_sdk_bundle import inspect_sdk_bundle, provision


class OpenHarmonySdkAssetManager:
    """Provision the compact build SDK without accepting a request path."""

    def __init__(
        self,
        state_root: Path,
        connectors: SourceConnectorManager,
        *,
        sdk_root: Path,
    ) -> None:
        self.connectors = connectors
        self.sdk_root = sdk_root.expanduser().resolve()
        self.registry = JsonRegistry(
            state_root.resolve() / "runtime-assets" / "openharmony-sdk.json",
            {"schema_version": "openharmony_sdk_asset_selection/v1", "selection": None},
        )

    def status(self) -> dict[str, Any]:
        state = self.registry.read()
        selection = state.get("selection") if isinstance(state.get("selection"), dict) else None
        source: dict[str, Any] | None = None
        if selection:
            connector_id = str(selection.get("source_connector_id") or "")
            try:
                connector = self.connectors.get(connector_id)
                source = {
                    "status": "available" if Path(str(connector["source_root"])).is_dir() else "unavailable",
                    "source_connector_id": connector_id,
                    "display_name": connector.get("display_name"),
                    "source_root": connector.get("source_root"),
                    "selected_snapshot_id": selection.get("source_snapshot_id"),
                    "current_snapshot_id": (connector.get("snapshot") or {}).get("snapshot_id"),
                }
            except PlatformError:
                source = {
                    "status": "unavailable",
                    "source_connector_id": connector_id,
                    "display_name": selection.get("display_name"),
                    "source_root": None,
                    "selected_snapshot_id": selection.get("source_snapshot_id"),
                    "current_snapshot_id": None,
                }
        return {
            "schema_version": "openharmony_sdk_runtime_asset/v1",
            "bundle": inspect_sdk_bundle(self.sdk_root),
            "selection": selection,
            "source": source,
        }

    def provision_from_connector(self, source_connector_id: str) -> dict[str, Any]:
        connector = self.connectors.get(source_connector_id)
        source_root = Path(str(connector["source_root"])).resolve()
        if not source_root.is_dir():
            raise PlatformError(
                "OPENHARMONY_SDK_SOURCE_UNAVAILABLE",
                "已登记代码仓当前不可访问，请在代码仓页面刷新或重新登记。",
                status_code=409,
                details={"source_connector_id": source_connector_id},
            )
        try:
            result = provision(source_root=source_root, destination=self.sdk_root)
        except ValueError as exc:
            raise PlatformError(
                "OPENHARMONY_SDK_SOURCE_INCOMPLETE",
                "所选代码仓不是完整的 OpenHarmony checkout，无法生成真机构建 SDK。",
                status_code=422,
                details={"source_connector_id": source_connector_id, "diagnostic": str(exc)},
            ) from exc
        snapshot = connector.get("snapshot") if isinstance(connector.get("snapshot"), dict) else {}
        selection = {
            "source_connector_id": source_connector_id,
            "display_name": connector.get("display_name"),
            "source_snapshot_id": snapshot.get("snapshot_id"),
            "provisioned_at": utc_now(),
            "manifest_sha256": result["manifest_sha256"],
        }
        self.registry.write({"schema_version": "openharmony_sdk_asset_selection/v1", "selection": selection})
        return {
            "schema_version": "openharmony_sdk_provision_result/v1",
            "status": "ready",
            "selection": selection,
            "source": {
                "source_connector_id": source_connector_id,
                "display_name": connector.get("display_name"),
                "source_root": str(source_root),
            },
            "bundle": inspect_sdk_bundle(self.sdk_root),
            "provision": result,
        }
