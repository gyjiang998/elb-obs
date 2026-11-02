"""Durable hardware topology profiles used by conditional parallel search.

A hardware profile records topology and supported OPPs.  It is deliberately
separate from a calibrated performance model: profiles imported from HDC make
the search space concrete, but do not turn inherited proxy coefficients into
device measurements.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from backend.core import PlatformError, atomic_write_json, utc_now


KINDS = ("big", "middle", "small")
MAX_CORE_GROUPS = 64
GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
PROFILE_SCHEMA = "parallel_hardware_profile/v1"
PROFILE_ID = re.compile(r"hp_[0-9a-f]{24}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _error(message: str, code: str = "parallel_hardware_profile_invalid", *, status_code: int = 422, **details: Any) -> None:
    raise PlatformError(code, message, status_code=status_code, details=details)


def _string(value: Any, label: str, *, required: bool = True, maximum: int = 200) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        _error(f"{label} 应为 1 至 {maximum} 个字符。")
    return value.strip()


def _positive_integer(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        _error(f"{label} 应为 1 至 {maximum} 的整数。")
    return value


def _integer_list(value: Any, label: str, *, maximum_items: int, minimum: int = 0, maximum: int = 100_000_000) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum_items:
        _error(f"{label} 应为包含 1 至 {maximum_items} 个整数的数组。")
    if any(isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum for item in value):
        _error(f"{label} 包含无效整数。")
    if len(set(value)) != len(value):
        _error(f"{label} 不能包含重复值。")
    return sorted(value)


def _safe_metadata(value: Any, label: str, *, depth: int = 0) -> Any:
    """Bound optional extraction metadata and reject unsafe/raw identifiers."""
    if depth > 3:
        _error(f"{label} 嵌套层级过深。")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _error(f"{label} 包含非有限数值。")
        return value
    if isinstance(value, str):
        if len(value) > 500:
            _error(f"{label} 字符串过长。")
        return value
    if isinstance(value, list):
        if len(value) > 64:
            _error(f"{label} 数组过长。")
        return [_safe_metadata(item, label, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 64:
            _error(f"{label} 字段过多。")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                _error(f"{label} 包含无效字段名。")
            if key in {"device_serial", "serial", "udid"}:
                _error("硬件画像不得保存原始设备序列号；请只提供 device_serial_sha256。")
            result[key] = _safe_metadata(item, f"{label}.{key}", depth=depth + 1)
        return result
    _error(f"{label} 包含不支持的数据类型。")


def _proxy_kind(index: int, count: int) -> str:
    """Map an arbitrary real CPU domain to the legacy three-class proxy.

    The real domain remains independently addressable.  This mapping only
    selects inherited coefficients until that domain has its own calibration.
    """
    if count == 1:
        return "big"
    if count == 2:
        return "small" if index == 0 else "big"
    ratio = index / (count - 1)
    if ratio < 1 / 3:
        return "small"
    if ratio > 2 / 3:
        return "big"
    return "middle"


class HardwareProfileStore:
    """Content-addressed custom profiles plus one read-only built-in profile."""

    def __init__(self, modeling_root: Path, builtin_profile: dict[str, Any]):
        self.root = Path(modeling_root).resolve() / "hardware-profiles"
        self.root.mkdir(parents=True, exist_ok=True)
        self.builtin = copy.deepcopy(builtin_profile)
        self.builtin.update(
            schema_version=PROFILE_SCHEMA,
            source="builtin_archive",
            status="active",
            read_only=True,
            archived_at=None,
        )
        self.builtin["cpu_groups"] = copy.deepcopy(self.builtin["core_groups"])
        self.builtin["profile_sha256"] = _hash(self._integrity_content(self.builtin))

    @staticmethod
    def _integrity_content(profile: dict[str, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in profile.items()
                if key not in {"id", "created_at", "profile_sha256", "cpu_groups", "status", "archived_at", "read_only"}}

    def _normalize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            _error("硬件画像应为 JSON 对象。")
        name = _string(payload.get("name"), "name")
        groups_input = payload.get("core_groups", payload.get("cpu_groups"))
        if not isinstance(groups_input, list) or not 1 <= len(groups_input) <= MAX_CORE_GROUPS:
            _error(f"core_groups/cpu_groups 应包含 1 至 {MAX_CORE_GROUPS} 个 CPU 频率域。")
        groups: list[dict[str, Any]] = []
        used_cpus: set[int] = set()
        seen: set[str] = set()
        seen_frequency_domains: set[str] = set()
        for index, raw in enumerate(groups_input):
            if not isinstance(raw, dict):
                _error("每个核组应为 JSON 对象。")
            group_id = raw.get("id", raw.get("kind"))
            if not isinstance(group_id, str) or not GROUP_ID.fullmatch(group_id) or group_id in seen:
                _error("核组 id 应为唯一的 1 至 100 位安全标识符。")
            seen.add(group_id)
            frequency_domain_id = raw.get("frequency_domain_id", group_id)
            if (not isinstance(frequency_domain_id, str) or not GROUP_ID.fullmatch(frequency_domain_id)
                    or frequency_domain_id in seen_frequency_domains):
                _error("每个核组的 frequency_domain_id 应为唯一安全标识符。")
            seen_frequency_domains.add(frequency_domain_id)
            count = _positive_integer(raw.get("physical_core_count"), f"{group_id}.physical_core_count", maximum=256)
            cpus = _integer_list(raw.get("logical_cpu_ids"), f"{group_id}.logical_cpu_ids", maximum_items=512, maximum=4095)
            if used_cpus.intersection(cpus):
                _error("不同核组的 logical_cpu_ids 不能重叠。")
            used_cpus.update(cpus)
            if len(cpus) < count:
                _error(f"{group_id}.logical_cpu_ids 数量不能少于 physical_core_count。")
            topology_input = raw.get("physical_core_topology")
            physical_core_topology = None
            topology_representatives = None
            if topology_input is not None:
                if not isinstance(topology_input, list) or len(topology_input) != count:
                    _error(f"{group_id}.physical_core_topology 应为每个物理核提供一条映射。")
                physical_core_topology = []
                topology_cpus: set[int] = set()
                topology_ids: set[str] = set()
                for core_index, topology_raw in enumerate(topology_input):
                    if not isinstance(topology_raw, dict):
                        _error(f"{group_id}.physical_core_topology[{core_index}] 应为对象。")
                    topology_id = topology_raw.get("id", f"core_{core_index}")
                    if (not isinstance(topology_id, str) or not GROUP_ID.fullmatch(topology_id)
                            or topology_id in topology_ids):
                        _error(f"{group_id}.physical_core_topology 的物理核 id 应为唯一安全标识符。")
                    topology_ids.add(topology_id)
                    logical_members = _integer_list(topology_raw.get("logical_cpu_ids"),
                        f"{group_id}.{topology_id}.logical_cpu_ids", maximum_items=512, maximum=4095)
                    if any(cpu not in cpus for cpu in logical_members) or topology_cpus.intersection(logical_members):
                        _error(f"{group_id}.physical_core_topology 必须把核组内逻辑 CPU 无重叠地映射到物理核。")
                    topology_cpus.update(logical_members)
                    representative = topology_raw.get("representative_logical_cpu_id", logical_members[0])
                    if not isinstance(representative, int) or isinstance(representative, bool) or representative not in logical_members:
                        _error(f"{group_id}.{topology_id}.representative_logical_cpu_id 应属于该物理核的逻辑 CPU。")
                    topology_source = topology_raw.get("topology_source")
                    row = {
                        "id": topology_id,
                        "logical_cpu_ids": logical_members,
                        "representative_logical_cpu_id": representative,
                    }
                    if isinstance(topology_source, str) and topology_source.strip():
                        row["topology_source"] = topology_source.strip()[:100]
                    physical_core_topology.append(row)
                if topology_cpus != set(cpus):
                    _error(f"{group_id}.physical_core_topology 必须覆盖核组内全部逻辑 CPU。")
                topology_representatives = [row["representative_logical_cpu_id"]
                                            for row in physical_core_topology]
            representatives_input = raw.get("physical_core_representative_cpu_ids")
            if representatives_input is None:
                representatives = topology_representatives or [cpus[index * len(cpus) // count] for index in range(count)]
            else:
                representatives = _integer_list(representatives_input,
                    f"{group_id}.physical_core_representative_cpu_ids", maximum_items=256, maximum=4095)
            if len(representatives) != count or any(cpu not in cpus for cpu in representatives):
                _error(f"{group_id}.physical_core_representative_cpu_ids 应为每个物理核各一个、且属于 logical_cpu_ids 的 CPU。")
            if topology_representatives is not None and representatives != topology_representatives:
                _error(f"{group_id}.physical_core_representative_cpu_ids 应与 physical_core_topology 的代表 CPU 一致。")
            opps = _integer_list(raw.get("opps_khz"), f"{group_id}.opps_khz", maximum_items=256, minimum=1, maximum=100_000_000)
            maximum = raw.get("max_khz", opps[-1])
            minimum = raw.get("min_khz", opps[0])
            if maximum != opps[-1] or minimum != opps[0]:
                _error(f"{group_id} 的 min_khz/max_khz 必须与 OPP 列表边界一致。")
            explicit_proxy = raw.get("proxy_kind")
            if explicit_proxy is not None and explicit_proxy not in KINDS:
                _error(f"{group_id}.proxy_kind 应来自 big、middle、small。")
            groups.append({
                "id": group_id,
                "name": raw.get("name") or ({"big": "大核组", "middle": "中核组", "small": "小核组"}.get(group_id) or f"CPU 核组 {index + 1}"),
                "kind": group_id,
                "proxy_kind": explicit_proxy,
                "physical_core_count": count,
                "logical_cpu_count": len(cpus),
                "logical_cpu_ids": cpus,
                "physical_core_representative_cpu_ids": representatives,
                "frequency_domain_id": frequency_domain_id,
                "opps_khz": opps,
                "min_khz": minimum,
                "max_khz": maximum,
            })
            if physical_core_topology is not None:
                groups[-1]["physical_core_topology"] = physical_core_topology
            for field in ("physical_core_count_source", "source_policy_id"):
                value = raw.get(field)
                if isinstance(value, str) and value.strip():
                    groups[-1][field] = value.strip()[:100]
        ranked = sorted(range(len(groups)), key=lambda offset: (groups[offset]["max_khz"], groups[offset]["min_khz"], groups[offset]["id"]))
        rank_by_index = {offset: rank for rank, offset in enumerate(ranked)}
        for index, group in enumerate(groups):
            if group["proxy_kind"] is None:
                group["proxy_kind"] = group["id"] if group["id"] in KINDS else _proxy_kind(rank_by_index[index], len(groups))

        source = payload.get("source", "manual")
        if isinstance(source, dict):
            source_metadata = _safe_metadata(source, "source")
            source_kind = str(source_metadata.get("kind") or "manual")[:100]
        else:
            source_kind = _string(source, "source", maximum=100)
            source_metadata = {"kind": source_kind}
        evidence = _safe_metadata(payload.get("evidence", {}), "evidence")
        serial_hash = evidence.get("device_serial_sha256") if isinstance(evidence, dict) else None
        if serial_hash is not None and (not isinstance(serial_hash, str) or not SHA256.fullmatch(serial_hash)):
            _error("evidence.device_serial_sha256 必须为 64 位小写 SHA-256。")

        optional = {}
        for field in ("vendor", "model", "chipset", "os_build", "description"):
            value = _string(payload.get(field), field, required=False, maximum=500 if field == "description" else 200)
            if value is not None:
                optional[field] = value
        normalized = {
            "schema_version": PROFILE_SCHEMA,
            "name": name,
            **optional,
            "source": source_kind,
            "source_metadata": source_metadata,
            "evidence": evidence,
            "core_groups": groups,
            "frequency_domains": [{
                "id": group["frequency_domain_id"],
                "core_group_ids": [group["id"]],
                "shared_opp": True,
                "opps_khz": group["opps_khz"],
            } for group in groups],
        }
        return normalized

    def _verify(self, profile: dict[str, Any]) -> None:
        expected = _hash(self._integrity_content(profile))
        identity = profile.get("id")
        if profile.get("profile_sha256") != expected or (isinstance(identity, str) and identity.startswith("hp_")
                                                           and identity != "hp_" + expected[:24]):
            _error("已保存硬件画像内容校验失败。", "parallel_hardware_profile_corrupt", status_code=500,
                   hardware_profile_id=profile.get("id"))

    def create(self, payload: Any) -> dict[str, Any]:
        normalized = self._normalize(payload)
        identity = "hp_" + _hash(normalized)[:24]
        path = self.root / f"{identity}.json"
        if path.is_file():
            existing = self.get(identity)
            if existing["status"] == "archived":
                _error("相同内容的硬件画像已归档；请保留其历史记录或修改画像名称后新建。",
                       "parallel_hardware_profile_archived", status_code=409, hardware_profile_id=identity)
            return existing
        record = {
            **normalized,
            "id": identity,
            "created_at": utc_now(),
            "status": "active",
            "read_only": False,
            "archived_at": None,
        }
        record["profile_sha256"] = _hash(self._integrity_content(record))
        atomic_write_json(path, record)
        return self.get(identity)

    def install_portable(self, profile: Any) -> dict[str, Any]:
        """Materialize one exported profile without reinterpreting its identity.

        A saved profile stores ``source`` as the short kind and retains richer
        extraction attributes in ``source_metadata``. Passing that record back
        through ``create`` as-is would discard those attributes and produce a
        different content-addressed ID. Reconstructing the original creation
        payload and comparing its normalized form makes the import exact while
        retaining all validation performed for an ordinary profile.
        """
        if not isinstance(profile, dict):
            _error("离线硬件画像应为 JSON 对象。", "parallel_hardware_profile_corrupt", status_code=422)
        profile = copy.deepcopy(profile)
        profile.pop("cpu_groups", None)
        identity = profile.get("id")
        if not isinstance(identity, str) or not PROFILE_ID.fullmatch(identity):
            _error("离线硬件画像 ID 无效。", "parallel_hardware_profile_corrupt", status_code=422)
        self._verify(profile)
        source_metadata = profile.get("source_metadata")
        if not isinstance(source_metadata, dict):
            _error("离线硬件画像缺少来源元数据。", "parallel_hardware_profile_corrupt", status_code=422,
                   hardware_profile_id=identity)
        payload = copy.deepcopy(profile)
        payload["source"] = copy.deepcopy(source_metadata)
        normalized = self._normalize(payload)
        expected = self._integrity_content(profile)
        # Packages published before logical_cpu_count became explicit derive
        # the same fact from logical_cpu_ids.  Preserve their content-addressed
        # identity instead of retroactively adding a field to signed content.
        expected_groups = expected.get("core_groups", [])
        normalized_groups = normalized.get("core_groups", [])
        if len(expected_groups) == len(normalized_groups):
            for expected_group, normalized_group in zip(expected_groups, normalized_groups):
                if "logical_cpu_count" not in expected_group:
                    normalized_group.pop("logical_cpu_count", None)
        if normalized != expected:
            _error("离线硬件画像的规范化内容与已发布身份不一致。",
                   "parallel_hardware_profile_corrupt", status_code=422,
                   hardware_profile_id=identity)
        if (profile.get("status") != "active" or profile.get("read_only") is not False
                or profile.get("archived_at") is not None):
            _error("离线硬件画像必须是活动的可用画像。",
                   "parallel_hardware_profile_corrupt", status_code=422,
                   hardware_profile_id=identity)
        path = self.root / f"{identity}.json"
        if path.is_file():
            existing = self.get(identity)
            if existing.get("profile_sha256") != profile.get("profile_sha256"):
                _error("离线硬件画像与本地同 ID 记录冲突。",
                       "parallel_hardware_profile_corrupt", status_code=409,
                       hardware_profile_id=identity)
            return existing
        record = {
            **normalized,
            "id": identity,
            "created_at": profile.get("created_at") or utc_now(),
            "status": "active",
            "read_only": False,
            "archived_at": None,
            "profile_sha256": profile["profile_sha256"],
        }
        atomic_write_json(path, record)
        return self.get(identity)

    def get(self, identity: str, *, require_active: bool = False) -> dict[str, Any]:
        if identity == self.builtin["id"]:
            result = copy.deepcopy(self.builtin)
        else:
            if not isinstance(identity, str) or not PROFILE_ID.fullmatch(identity):
                _error("硬件画像不存在。", "parallel_hardware_profile_not_found", status_code=404)
            path = self.root / f"{identity}.json"
            if not path.is_file():
                _error("硬件画像不存在。", "parallel_hardware_profile_not_found", status_code=404,
                       hardware_profile_id=identity)
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _error("已保存硬件画像无法读取。", "parallel_hardware_profile_corrupt", status_code=500,
                       hardware_profile_id=identity, reason=str(exc))
            self._verify(result)
            result["cpu_groups"] = copy.deepcopy(result["core_groups"])
        if require_active and result.get("status") != "active":
            _error("硬件画像已归档，不能用于新的导入或寻优。", "parallel_hardware_profile_archived",
                   status_code=409, hardware_profile_id=identity)
        return result

    def list(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        rows = [copy.deepcopy(self.builtin)]
        for path in self.root.glob("hp_*.json"):
            try:
                row = self.get(path.stem)
            except PlatformError:
                raise
            if include_archived or row.get("status") == "active":
                rows.append(row)
        return sorted(rows, key=lambda row: (row["id"] != self.builtin["id"], row.get("created_at", ""), row["id"]))

    def archive(self, identity: str) -> dict[str, Any]:
        if identity == self.builtin["id"]:
            _error("内置参考硬件画像为只读，不能归档。", "parallel_hardware_profile_read_only", status_code=409)
        profile = self.get(identity)
        if profile["status"] == "archived":
            return profile
        profile.pop("cpu_groups", None)
        profile["status"] = "archived"
        profile["archived_at"] = utc_now()
        # status is lifecycle metadata and intentionally excluded from the
        # immutable profile content hash.
        atomic_write_json(self.root / f"{identity}.json", profile)
        return self.get(identity)

    def restore(self, identity: str) -> dict[str, Any]:
        if identity == self.builtin["id"]:
            _error("内置参考硬件画像为只读，无需恢复。", "parallel_hardware_profile_read_only", status_code=409)
        profile = self.get(identity)
        if profile["status"] == "active":
            return profile
        profile.pop("cpu_groups", None)
        profile["status"] = "active"
        profile["archived_at"] = None
        atomic_write_json(self.root / f"{identity}.json", profile)
        return self.get(identity)

