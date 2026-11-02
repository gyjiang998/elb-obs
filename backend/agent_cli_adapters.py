"""Vendor-neutral adapters for interactive coding-agent CLIs.

An adapter describes how to launch one fixed agent executable inside a durable
Platform Run workspace.  Commands are always passed to ``subprocess`` as an
argv list; manifests cannot inject a login shell or arbitrary command string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Mapping


ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ALLOWED_PLACEHOLDERS = {
    "workspace",
    "run_id",
    "agent_session_id",
    "mcp_config",
    "mcp_descriptor",
    "skill_root",
}
SUBMIT_MODES = {"bracketed_paste_cr", "bracketed_paste_lf", "plain_text_cr", "plain_text_lf"}
SESSION_STRATEGIES = {"native_session", "workspace_continue", "stateless"}
MCP_FORMATS = {"none", "kimi", "opencode", "neutral"}
MODEL_CONFIG_MODES = {"inherit", "openai_compatible", "environment"}
_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]+)\}")
_ENV_REFERENCE_PATTERN = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
DEFAULT_USER_SKILL_SOURCE_ROOTS = ("~/.agents/skills",)


class AgentCliAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class AgentCliLaunchSpec:
    adapter_id: str
    display_name: str
    command: tuple[str, ...]
    workspace: Path
    environment: dict[str, str]
    submit_mode: str
    startup_input_delay_ms: int
    submit_delay_ms: int
    submit_fallback_ms: int
    agent_session_id: str
    agent_workspace_id: str
    workspace_contract_sha256: str
    usage_kind: str


@dataclass(frozen=True)
class AgentCliAdapter:
    adapter_id: str
    display_name: str
    description: str
    executable: str
    args: tuple[str, ...] = ()
    resume_args: tuple[str, ...] = ()
    session_strategy: str = "stateless"
    submit_mode: str = "bracketed_paste_cr"
    startup_input_delay_ms: int = 0
    submit_delay_ms: int = 35
    submit_fallback_ms: int = 0
    environment: Mapping[str, str] = field(default_factory=dict)
    model_config: Mapping[str, str] = field(default_factory=dict)
    skill_roots: tuple[str, ...] = (".agents/skills",)
    skill_source_roots: tuple[str, ...] = ()
    mcp_format: str = "none"
    mcp_path: str = ""
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    usage_kind: str = "unavailable"
    source: str = "built_in"
    environment_factory: Callable[[], dict[str, str]] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not ADAPTER_ID_PATTERN.fullmatch(self.adapter_id):
            raise AgentCliAdapterError("AGENT_CLI_ADAPTER_ID_INVALID", f"Agent CLI adapter id 不合法：{self.adapter_id}")
        if not self.display_name.strip():
            raise AgentCliAdapterError("AGENT_CLI_ADAPTER_NAME_INVALID", "Agent CLI adapter 显示名称不能为空。")
        if not self.executable.strip():
            raise AgentCliAdapterError("AGENT_CLI_EXECUTABLE_INVALID", "Agent CLI executable 不能为空。")
        if self.submit_mode not in SUBMIT_MODES:
            raise AgentCliAdapterError("AGENT_CLI_SUBMIT_MODE_INVALID", f"不支持的终端提交模式：{self.submit_mode}")
        if not isinstance(self.startup_input_delay_ms, int) or not 0 <= self.startup_input_delay_ms <= 5000:
            raise AgentCliAdapterError(
                "AGENT_CLI_STARTUP_INPUT_DELAY_INVALID",
                "Agent CLI startup_input_delay_ms 必须是 0 到 5000 的整数。",
            )
        if not isinstance(self.submit_delay_ms, int) or not 0 <= self.submit_delay_ms <= 5000:
            raise AgentCliAdapterError("AGENT_CLI_SUBMIT_DELAY_INVALID", "Agent CLI submit_delay_ms 必须是 0 到 5000 的整数。")
        if not isinstance(self.submit_fallback_ms, int) or not 0 <= self.submit_fallback_ms <= 1000:
            raise AgentCliAdapterError("AGENT_CLI_SUBMIT_FALLBACK_INVALID", "Agent CLI submit_fallback_ms 必须是 0 到 1000 的整数。")
        if self.session_strategy not in SESSION_STRATEGIES:
            raise AgentCliAdapterError("AGENT_CLI_SESSION_STRATEGY_INVALID", f"不支持的 Session 策略：{self.session_strategy}")
        if self.mcp_format not in MCP_FORMATS:
            raise AgentCliAdapterError("AGENT_CLI_MCP_FORMAT_INVALID", f"不支持的 MCP 配置格式：{self.mcp_format}")
        model_mode = str(self.model_config.get("mode") or "inherit")
        if model_mode not in MODEL_CONFIG_MODES:
            raise AgentCliAdapterError("AGENT_CLI_MODEL_CONFIG_INVALID", f"不支持的模型配置模式：{model_mode}")
        for value in (*self.args, *self.resume_args):
            self._validate_template(value)
        if any("{mcp_config}" in value for value in (*self.args, *self.resume_args)) and (
            self.mcp_format == "none" or not self.mcp_path
        ):
            raise AgentCliAdapterError(
                "AGENT_CLI_MCP_CONFIG_MISSING",
                "Agent CLI argv 使用了 {mcp_config}，但 workspace.mcp_format/mcp_path 未配置。",
            )
        for relative in (*self.skill_roots, *((self.mcp_path,) if self.mcp_path else ())):
            _safe_relative_path(relative)
        if any(not value.strip() for value in self.skill_source_roots):
            raise AgentCliAdapterError(
                "AGENT_CLI_SKILL_SOURCE_INVALID",
                "workspace.skill_source_roots 不能包含空路径。",
            )

    @staticmethod
    def _validate_template(value: str) -> None:
        unknown = set(_PLACEHOLDER_PATTERN.findall(value)) - ALLOWED_PLACEHOLDERS
        if unknown:
            raise AgentCliAdapterError(
                "AGENT_CLI_PLACEHOLDER_INVALID",
                f"Agent CLI argv 含有未知占位符：{', '.join(sorted(unknown))}",
            )

    def resolve_executable(self) -> Path | None:
        expanded = Path(os.path.expanduser(os.path.expandvars(self.executable)))
        if expanded.is_absolute() or "/" in self.executable or "\\" in self.executable:
            return expanded.resolve() if expanded.is_file() else None
        resolved = shutil.which(self.executable)
        return Path(resolved).resolve() if resolved else None

    def public(self, *, default: bool = False) -> dict[str, Any]:
        executable = self.resolve_executable()
        model_mode = str(self.model_config.get("mode") or "inherit")
        explicit_mcp_arg = any("{mcp_config}" in value for value in (*self.args, *self.resume_args))
        mcp_declared = bool(self.capabilities.get("mcp"))
        mcp_configured = self.mcp_format != "none" and bool(self.mcp_path)
        integration_warning = None
        if mcp_declared and not mcp_configured:
            integration_warning = "Adapter 声明支持 MCP，但没有配置原生 MCP 投影。"
        elif mcp_declared and self.source.startswith("manifest:") and not explicit_mcp_arg:
            integration_warning = (
                "MCP 配置已生成，但自定义 CLI 未通过 argv 显式接收；"
                "请确认框架会读取 HETERO_AGENT_MCP_CONFIG。"
            )
        return {
            "schema_version": "agent_cli_adapter/v1",
            "adapter_id": self.adapter_id,
            "display_name": self.display_name,
            "description": self.description,
            "available": executable is not None and os.access(executable, os.X_OK),
            "diagnostic": None if executable is not None else f"未找到可执行文件：{self.executable}",
            "session_strategy": self.session_strategy,
            "submit_mode": self.submit_mode,
            "startup_input_delay_ms": self.startup_input_delay_ms,
            "submit_delay_ms": self.submit_delay_ms,
            "submit_fallback_ms": self.submit_fallback_ms,
            "capabilities": dict(self.capabilities),
            "mcp_integration": {
                "declared": mcp_declared,
                "configured": mcp_configured,
                "format": self.mcp_format,
                "path": self.mcp_path or None,
                "delivery": "argv_and_environment" if explicit_mcp_arg else "environment_and_workspace",
                "warning": integration_warning,
            },
            "skill_integration": {
                "projection_roots": list(self.skill_roots),
                "source_roots": self._skill_source_root_statuses(),
            },
            "browser_model_override_supported": bool(
                self.capabilities.get("browser_model_override", model_mode != "inherit")
            ),
            "usage_kind": self.usage_kind,
            "source": self.source,
            "default": default,
        }

    def build_launch(
        self,
        *,
        workspace: Path,
        run_id: str,
        agent_session_id: str,
        agent_workspace_id: str,
        resume: bool,
        credential: Mapping[str, Any] | None = None,
    ) -> AgentCliLaunchSpec:
        executable = self.resolve_executable()
        if executable is None or not os.access(executable, os.X_OK):
            raise AgentCliAdapterError(
                "AGENT_CLI_BINARY_MISSING",
                f"未找到 {self.display_name} 可执行文件：{self.executable}",
                details={"adapter_id": self.adapter_id, "executable": self.executable},
            )
        resolved_workspace = workspace.resolve()
        mcp_descriptor = resolved_workspace / ".agent-platform" / "mcp.json"
        mcp_config = resolved_workspace / self.mcp_path if self.mcp_format != "none" and self.mcp_path else mcp_descriptor
        skill_root = resolved_workspace / ".agents" / "skills"
        values = {
            "workspace": str(resolved_workspace),
            "run_id": run_id,
            "agent_session_id": agent_session_id,
            "mcp_config": str(mcp_config.resolve()),
            "mcp_descriptor": str(mcp_descriptor.resolve()),
            "skill_root": str(skill_root.resolve()),
        }
        argv = [str(executable), *(_expand_template(item, values) for item in self.args)]
        if resume:
            argv.extend(_expand_template(item, values) for item in self.resume_args)
        environment = dict(self.environment_factory() if self.environment_factory is not None else os.environ)
        for name, value in self.environment.items():
            match = _ENV_REFERENCE_PATTERN.fullmatch(value)
            if match:
                inherited = os.environ.get(match.group(1))
                if inherited is not None:
                    environment[name] = inherited
                continue
            environment[name] = _expand_template(value, values)
        environment.update({
            "HETERO_AGENT_WORKSPACE": str(resolved_workspace),
            "HETERO_AGENT_RUN_ID": run_id,
            "HETERO_AGENT_SESSION_ID": agent_session_id,
            "HETERO_AGENT_SKILL_ROOT": str(skill_root.resolve()),
            "HETERO_AGENT_MCP_DESCRIPTOR": str(mcp_descriptor.resolve()),
            "HETERO_AGENT_MCP_FORMAT": self.mcp_format,
        })
        if self.usage_kind == "standard_jsonl":
            usage_journal = resolved_workspace / ".agent-platform" / "token-usage.jsonl"
            usage_journal.parent.mkdir(parents=True, exist_ok=True)
            environment["HETERO_AGENT_USAGE_JSONL"] = str(usage_journal.resolve())
        if self.mcp_format != "none" and self.mcp_path:
            environment["HETERO_AGENT_MCP_CONFIG"] = str(mcp_config.resolve())
        self._apply_model_configuration(environment, credential)
        projection_marker = resolved_workspace / ".agent-platform" / f"adapter-{self.adapter_id}-projection.json"
        tool_catalog = resolved_workspace / ".agent-platform" / "mcp-tools.json"
        workspace_contract_sha256 = hashlib.sha256(json.dumps({
            "adapter_id": self.adapter_id,
            "mcp_config_sha256": _file_sha256(mcp_config),
            "mcp_tool_catalog_sha256": _file_sha256(tool_catalog),
            "adapter_projection_sha256": _file_sha256(projection_marker),
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return AgentCliLaunchSpec(
            adapter_id=self.adapter_id,
            display_name=self.display_name,
            command=tuple(argv),
            workspace=resolved_workspace,
            environment=environment,
            submit_mode=self.submit_mode,
            startup_input_delay_ms=self.startup_input_delay_ms,
            submit_delay_ms=self.submit_delay_ms,
            submit_fallback_ms=self.submit_fallback_ms,
            agent_session_id=agent_session_id,
            agent_workspace_id=agent_workspace_id,
            workspace_contract_sha256=workspace_contract_sha256,
            usage_kind=self.usage_kind,
        )

    def _apply_model_configuration(self, environment: dict[str, str], credential: Mapping[str, Any] | None) -> None:
        if not credential:
            return
        mode = str(self.model_config.get("mode") or "inherit")
        base_url = str(credential.get("base_url") or credential.get("baseUrl") or "").strip().rstrip("/")
        api_key = str(credential.get("api_key") or credential.get("apiKey") or "")
        model = str(credential.get("model") or "").strip()
        context = _positive_int(credential.get("context_window_tokens") or credential.get("contextWindowTokens"), 256 * 1024)
        if not base_url or not api_key or not model or mode == "inherit":
            return
        if mode == "environment":
            mapping = {
                "base_url_env": base_url,
                "api_key_env": api_key,
                "model_env": model,
                "context_window_env": str(context),
            }
            for config_key, value in mapping.items():
                env_name = str(self.model_config.get(config_key) or "").strip()
                if env_name:
                    environment[env_name] = value
            return
        environment["HETERO_AGENT_MODEL_API_KEY"] = api_key
        inline = _json_object(environment.get("OPENCODE_CONFIG_CONTENT"))
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Heterogeneous Platform browser model",
            "options": {
                "baseURL": base_url,
                "apiKey": "{env:HETERO_AGENT_MODEL_API_KEY}",
            },
            "models": {
                model: {
                    "name": model,
                    "limit": {"context": context, "output": min(64 * 1024, max(4096, context // 4))},
                }
            },
        }
        providers = inline.get("provider") if isinstance(inline.get("provider"), dict) else {}
        inline["provider"] = {**providers, "hetero-browser": provider}
        inline["model"] = f"hetero-browser/{model}"
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline, ensure_ascii=True, separators=(",", ":"))

    def materialize_workspace(self, workspace: Path) -> None:
        """Project adapter-neutral Skills and MCP data into CLI-native files."""

        resolved = workspace.resolve()
        source_skills = resolved / ".agents" / "skills"
        neutral_path = resolved / ".agent-platform" / "mcp.json"
        projection_marker = resolved / ".agent-platform" / f"adapter-{self.adapter_id}-projection.json"
        skill_imports = self._import_external_skills(resolved, source_skills)
        source_files = []
        if source_skills.is_dir():
            for path in sorted(source_skills.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    source_files.append({
                        "path": path.relative_to(source_skills).as_posix(),
                        "size": stat.st_size,
                        "sha256": _file_sha256(path),
                    })
        neutral_stat = neutral_path.stat() if neutral_path.is_file() else None
        projection = {
            "schema_version": "agent_adapter_projection/v1",
            "adapter_id": self.adapter_id,
            "skill_roots": list(self.skill_roots),
            "skill_imports": skill_imports,
            "mcp_format": self.mcp_format,
            "mcp_path": self.mcp_path,
            "source_files": source_files,
            "neutral_mcp": {
                "size": neutral_stat.st_size,
                "sha256": _file_sha256(neutral_path),
            } if neutral_stat is not None else None,
        }
        skill_targets = [resolved / _safe_relative_path(relative) for relative in self.skill_roots]
        native_mcp_target = resolved / _safe_relative_path(self.mcp_path) if self.mcp_format != "none" and self.mcp_path else None
        targets_ready = all(target.is_dir() for target in skill_targets) and (
            native_mcp_target is None or native_mcp_target.is_file()
        )
        if projection_marker.is_file() and targets_ready:
            try:
                if _json_object(projection_marker.read_text(encoding="utf-8")) == projection:
                    return
            except OSError:
                pass
        for relative in self.skill_roots:
            target = resolved / _safe_relative_path(relative)
            if target.resolve() == source_skills.resolve() or not source_skills.is_dir():
                continue
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_skills, target)
        adapter_metadata = {
            "schema_version": "agent_workspace_adapter/v1",
            "adapter_id": self.adapter_id,
            "skill_root": ".agents/skills",
            "skills": {
                "projection_roots": list(self.skill_roots),
                "source_roots": skill_imports.get("source_roots", []),
                "imported": skill_imports.get("imported", []),
                "collisions": skill_imports.get("collisions", []),
            },
            "mcp": {
                "declared": bool(self.capabilities.get("mcp")),
                "format": self.mcp_format,
                "neutral_descriptor": ".agent-platform/mcp.json",
                "native_config": self.mcp_path or None,
                "delivery_environment": "HETERO_AGENT_MCP_CONFIG",
            },
        }
        metadata_path = resolved / ".agent-platform" / "adapter.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(adapter_metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if self.mcp_format == "none" or not self.mcp_path:
            projection_marker.write_text(json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return
        neutral = _json_object(neutral_path.read_text(encoding="utf-8") if neutral_path.is_file() else "")
        servers = neutral.get("servers") if isinstance(neutral.get("servers"), dict) else {}
        if self.mcp_format == "kimi":
            native = {
                "mcpServers": {
                    server_id: {
                        "command": record.get("command", [""])[0],
                        "args": list(record.get("command", []))[1:],
                        "cwd": record.get("cwd"),
                        "env": record.get("environment", {}),
                        "startupTimeoutMs": record.get("startup_timeout_ms", 30000),
                        "toolTimeoutMs": record.get("tool_timeout_ms", 900000),
                    }
                    for server_id, record in servers.items()
                    if isinstance(record, dict) and isinstance(record.get("command"), list) and record.get("command")
                }
            }
        elif self.mcp_format == "opencode":
            native = {
                "$schema": "https://opencode.ai/config.json",
                "permission": {"*": "allow", "webfetch": "deny", "websearch": "deny"},
                "mcp": {
                    server_id: {
                        "type": "local",
                        "command": list(record.get("command", [])),
                        "environment": record.get("environment", {}),
                        "enabled": True,
                        "timeout": record.get("tool_timeout_ms", 900000),
                    }
                    for server_id, record in servers.items()
                    if isinstance(record, dict) and isinstance(record.get("command"), list) and record.get("command")
                },
            }
        else:
            native = neutral
        target = resolved / _safe_relative_path(self.mcp_path)
        existing = _json_object(target.read_text(encoding="utf-8") if target.is_file() else "")
        server_key = {"kimi": "mcpServers", "opencode": "mcp"}.get(self.mcp_format, "servers")
        existing_servers = existing.get(server_key)
        if isinstance(existing_servers, dict):
            existing[server_key] = {
                server_id: record
                for server_id, record in existing_servers.items()
                if not str(server_id).startswith("heterogeneous_platform_")
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_deep_merge(existing, native), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        projection_marker.write_text(json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _skill_source_root_statuses(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for configured in self.skill_source_roots:
            resolved, diagnostic = _resolve_skill_source_root(configured)
            statuses.append({
                "configured": configured,
                "resolved": str(resolved) if resolved is not None else None,
                "available": bool(resolved is not None and resolved.is_dir() and diagnostic is None),
                "diagnostic": diagnostic,
            })
        return statuses

    def _import_external_skills(self, workspace: Path, target_root: Path) -> dict[str, Any]:
        """Import adapter-owned Skills into the neutral per-Run Skill store.

        The platform/user profile remains authoritative on collisions. External
        roots only fill missing Skill IDs, which makes framework-installed
        Skills available inside an isolated Run without changing their source.
        """

        marker_path = workspace / ".agent-platform" / "external-skill-imports.json"
        previous = _json_object(marker_path.read_text(encoding="utf-8") if marker_path.is_file() else "")
        previous_records = {
            str(item.get("skill_id")): item
            for item in previous.get("imported", [])
            if isinstance(item, dict) and isinstance(item.get("skill_id"), str)
        }
        existing_ids = {
            path.name
            for path in target_root.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        } if target_root.is_dir() else set()
        platform_owned_ids = existing_ids - set(previous_records)

        source_statuses = self._skill_source_root_statuses()
        candidates: dict[str, dict[str, Any]] = {}
        duplicates: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for status in source_statuses:
            source_root_text = status.get("resolved")
            if not status.get("available") or not isinstance(source_root_text, str):
                continue
            source_root = Path(source_root_text).resolve()
            try:
                source_root.relative_to(workspace)
            except ValueError:
                pass
            else:
                status["available"] = False
                status["diagnostic"] = "来源目录位于当前 Run 工作区内，已拒绝递归导入。"
                continue
            skill_dirs = [source_root] if (source_root / "SKILL.md").is_file() else [
                child
                for child in sorted(source_root.iterdir(), key=lambda item: item.name)
                if child.is_dir() and (child / "SKILL.md").is_file()
            ]
            for skill_dir in skill_dirs:
                skill_id = skill_dir.name
                if not SKILL_ID_PATTERN.fullmatch(skill_id):
                    duplicates.append({
                        "skill_id": skill_id,
                        "source": str(skill_dir),
                        "reason": "invalid_skill_id",
                    })
                    continue
                try:
                    tree_fingerprint_sha256 = _skill_tree_fingerprint(skill_dir)
                except OSError as exc:
                    errors.append({
                        "skill_id": skill_id,
                        "source": str(skill_dir),
                        "reason": "source_read_failed",
                        "detail": str(exc),
                    })
                    continue
                record = {
                    "skill_id": skill_id,
                    "source_root": str(source_root),
                    "source": str(skill_dir.resolve()),
                    "tree_fingerprint_sha256": tree_fingerprint_sha256,
                }
                if skill_id in candidates:
                    duplicates.append({
                        "skill_id": skill_id,
                        "source": record["source"],
                        "selected_source": candidates[skill_id]["source"],
                        "reason": "earlier_source_root_wins",
                    })
                    continue
                candidates[skill_id] = record

        collisions: list[dict[str, Any]] = []
        imported: list[dict[str, Any]] = []
        target_root.mkdir(parents=True, exist_ok=True)
        for skill_id, record in sorted(candidates.items()):
            target = target_root / skill_id
            if skill_id in platform_owned_ids:
                collisions.append({
                    "skill_id": skill_id,
                    "source": record["source"],
                    "resolution": "platform_or_user_version_kept",
                })
                continue
            prior = previous_records.get(skill_id, {})
            try:
                target_sha256 = _skill_tree_fingerprint(target) if target.is_dir() else None
            except OSError:
                target_sha256 = None
            unchanged = (
                target.is_dir()
                and prior.get("source") == record["source"]
                and prior.get("tree_fingerprint_sha256") == record["tree_fingerprint_sha256"]
                and target_sha256 == record["tree_fingerprint_sha256"]
            )
            if not unchanged:
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(Path(record["source"]), target)
                except OSError as exc:
                    errors.append({
                        "skill_id": skill_id,
                        "source": record["source"],
                        "reason": "projection_failed",
                        "detail": str(exc),
                    })
                    continue
            imported.append({**record, "target": target.relative_to(workspace).as_posix()})

        for skill_id, prior in sorted(previous_records.items()):
            if skill_id in candidates or skill_id in platform_owned_ids:
                continue
            target = target_root / skill_id
            if target.is_dir():
                shutil.rmtree(target)

        result = {
            "schema_version": "agent_external_skill_import/v1",
            "adapter_id": self.adapter_id,
            "source_roots": source_statuses,
            "imported": imported,
            "collisions": collisions,
            "duplicates": duplicates,
            "errors": errors,
        }
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result


class AgentCliAdapterRegistry:
    def __init__(
        self,
        *,
        kimi_binary: Path,
        kimi_environment_factory: Callable[[], dict[str, str]],
        custom_manifest_path: Path | None = None,
        default_adapter_id: str | None = None,
        tolerate_manifest_errors: bool = False,
        user_skill_source_roots: tuple[str, ...] | None = None,
    ) -> None:
        self.custom_manifest_path = custom_manifest_path.resolve() if custom_manifest_path else None
        self.default_adapter_id = (default_adapter_id or os.environ.get("HETERO_AGENT_CLI_ADAPTER") or "kimi").strip()
        self.tolerate_manifest_errors = tolerate_manifest_errors
        self.user_skill_source_roots = (
            DEFAULT_USER_SKILL_SOURCE_ROOTS
            if user_skill_source_roots is None
            else tuple(user_skill_source_roots)
        )
        self._builtins = {
            "kimi": AgentCliAdapter(
                adapter_id="kimi",
                display_name="Kimi CLI",
                description="平台内置 Kimi Coding Agent，复用原生持久 Session。",
                executable=str(kimi_binary),
                args=("--auto", "--skills-dir", "{skill_root}", "--session", "{agent_session_id}"),
                session_strategy="native_session",
                submit_mode="bracketed_paste_cr",
                startup_input_delay_ms=0,
                submit_delay_ms=0,
                submit_fallback_ms=0,
                environment={"KIMI_DISABLE_TELEMETRY": "1"},
                model_config={"mode": "inherit"},
                skill_roots=(".kimi-code/skills", ".agents/skills"),
                skill_source_roots=self.user_skill_source_roots,
                mcp_format="kimi",
                mcp_path=".kimi-code/mcp.json",
                capabilities=_default_capabilities(token_usage=True, browser_model_override=True),
                usage_kind="kimi_wire",
                environment_factory=kimi_environment_factory,
            ),
            "opencode": AgentCliAdapter(
                adapter_id="opencode",
                display_name="OpenCode",
                description="OpenCode TUI，通过独立 Run 工作区复用 Skill、MCP 与页面 Session。",
                executable=os.environ.get("HETERO_OPENCODE_BIN", "opencode"),
                args=("{workspace}",),
                resume_args=("--continue",),
                session_strategy="workspace_continue",
                submit_mode="bracketed_paste_cr",
                startup_input_delay_ms=0,
                submit_delay_ms=0,
                submit_fallback_ms=0,
                model_config={"mode": "openai_compatible"},
                skill_roots=(".agents/skills", ".opencode/skills"),
                skill_source_roots=self.user_skill_source_roots,
                mcp_format="opencode",
                mcp_path="opencode.json",
                capabilities=_default_capabilities(token_usage=False, browser_model_override=True),
            ),
        }
        self._custom: dict[str, AgentCliAdapter] = {}
        self._manifest_mtime_ns: int | None = None
        self._manifest_error: dict[str, Any] | None = None
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> None:
        path = self.custom_manifest_path
        if path is None:
            self._custom = {}
            self._manifest_error = None
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            self._custom = {}
            self._manifest_mtime_ns = None
            return
        if not force and self._manifest_mtime_ns == mtime_ns:
            return
        try:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", f"无法读取 Agent CLI manifest：{path}") from exc
            records = payload.get("adapters") if isinstance(payload, dict) else payload
            if not isinstance(records, list):
                raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "Agent CLI manifest 必须包含 adapters 数组。")
            loaded: dict[str, AgentCliAdapter] = {}
            for raw in records:
                adapter = _adapter_from_manifest(
                    raw,
                    path,
                    user_skill_source_roots=self.user_skill_source_roots,
                )
                if adapter.adapter_id in self._builtins or adapter.adapter_id in loaded:
                    raise AgentCliAdapterError("AGENT_CLI_ADAPTER_DUPLICATE", f"Agent CLI adapter id 重复：{adapter.adapter_id}")
                loaded[adapter.adapter_id] = adapter
        except AgentCliAdapterError as exc:
            if not self.tolerate_manifest_errors:
                raise
            self._manifest_mtime_ns = mtime_ns
            self._manifest_error = {
                "code": exc.code,
                "message": exc.message,
                "path": str(path),
            }
            return
        self._custom = loaded
        self._manifest_mtime_ns = mtime_ns
        self._manifest_error = None

    def get(self, adapter_id: str | None = None) -> AgentCliAdapter:
        self.refresh()
        selected = (adapter_id or self.default_adapter_id).strip()
        adapter = {**self._builtins, **self._custom}.get(selected)
        if adapter is None:
            raise AgentCliAdapterError(
                "AGENT_CLI_ADAPTER_NOT_FOUND",
                f"未登记 Agent CLI adapter：{selected}",
                details={"available_adapter_ids": sorted((*self._builtins, *self._custom))},
            )
        return adapter

    def catalog(self) -> dict[str, Any]:
        self.refresh()
        adapters = [
            adapter.public(default=adapter.adapter_id == self.default_adapter_id)
            for adapter in sorted({**self._builtins, **self._custom}.values(), key=lambda item: item.adapter_id)
        ]
        if not any(item["default"] for item in adapters) and adapters:
            adapters[0]["default"] = True
        return {
            "schema_version": "agent_cli_adapter_catalog/v1",
            "default_adapter_id": next((item["adapter_id"] for item in adapters if item["default"]), ""),
            "adapters": adapters,
            "manifest_error": dict(self._manifest_error) if self._manifest_error else None,
        }


def _adapter_from_manifest(
    value: Any,
    source_path: Path,
    *,
    user_skill_source_roots: tuple[str, ...] = DEFAULT_USER_SKILL_SOURCE_ROOTS,
) -> AgentCliAdapter:
    if not isinstance(value, dict):
        raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "每个 Agent CLI adapter 必须是 JSON object。")
    workspace = value.get("workspace") if isinstance(value.get("workspace"), dict) else {}
    model = value.get("model_config") if isinstance(value.get("model_config"), dict) else {}
    capabilities = value.get("capabilities") if isinstance(value.get("capabilities"), dict) else {}
    args = value.get("args", [])
    resume_args = value.get("resume_args", [])
    skill_roots = workspace.get("skill_roots", [".agents/skills"])
    skill_source_roots = workspace.get("skill_source_roots", [])
    environment = value.get("environment", {})
    if not all(isinstance(item, str) for item in args) or not isinstance(args, list):
        raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "adapter.args 必须是字符串数组。")
    if not all(isinstance(item, str) for item in resume_args) or not isinstance(resume_args, list):
        raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "adapter.resume_args 必须是字符串数组。")
    if not isinstance(skill_roots, list) or not all(isinstance(item, str) for item in skill_roots):
        raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "workspace.skill_roots 必须是相对路径字符串数组。")
    if not isinstance(skill_source_roots, list) or not all(isinstance(item, str) for item in skill_source_roots):
        raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "workspace.skill_source_roots 必须是路径字符串数组。")
    if not isinstance(environment, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in environment.items()):
        raise AgentCliAdapterError("AGENT_CLI_MANIFEST_INVALID", "adapter.environment 必须是字符串键值对象。")
    return AgentCliAdapter(
        adapter_id=str(value.get("adapter_id") or ""),
        display_name=str(value.get("display_name") or ""),
        description=str(value.get("description") or "自定义 Agent CLI adapter。"),
        executable=str(value.get("executable") or ""),
        args=tuple(args),
        resume_args=tuple(resume_args),
        session_strategy=str(value.get("session_strategy") or "stateless"),
        submit_mode=str(value.get("submit_mode") or "bracketed_paste_cr"),
        startup_input_delay_ms=_bounded_nonnegative_int(value.get("startup_input_delay_ms"), 600, maximum=5000),
        submit_delay_ms=_bounded_nonnegative_int(value.get("submit_delay_ms"), 35, maximum=5000),
        submit_fallback_ms=_bounded_nonnegative_int(value.get("submit_fallback_ms"), 0, maximum=1000),
        environment=environment,
        model_config={str(key): str(item) for key, item in model.items()},
        skill_roots=tuple(skill_roots),
        skill_source_roots=_merge_skill_source_roots(
            tuple(
                _normalize_manifest_source_root(item, source_path.parent)
                for item in skill_source_roots
            ),
            user_skill_source_roots,
        ),
        mcp_format=str(workspace.get("mcp_format") or "none"),
        mcp_path=str(workspace.get("mcp_path") or ""),
        capabilities={str(key): bool(item) for key, item in capabilities.items()},
        usage_kind=str(value.get("usage_kind") or "unavailable"),
        source=f"manifest:{source_path}",
    )


def _default_capabilities(*, token_usage: bool, browser_model_override: bool) -> dict[str, bool]:
    return {
        "interactive_pty": True,
        "persistent_session": True,
        "native_filesystem": True,
        "native_shell": True,
        "mcp": True,
        "skills": True,
        "structured_page_actions": True,
        "token_usage": token_usage,
        "browser_model_override": browser_model_override,
    }


def _expand_template(value: str, replacements: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in replacements:
            raise AgentCliAdapterError("AGENT_CLI_PLACEHOLDER_INVALID", f"未提供 Agent CLI 占位符：{name}")
        return replacements[name]

    return _PLACEHOLDER_PATTERN.sub(replace, value)


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise AgentCliAdapterError("AGENT_CLI_WORKSPACE_PATH_INVALID", f"Agent workspace 路径必须是安全相对路径：{value}")
    return path


def _normalize_manifest_source_root(value: str, manifest_root: Path) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    if _ENV_REFERENCE_PATTERN.fullmatch(stripped) or stripped.startswith("~") or "$" in stripped:
        return stripped
    path = Path(stripped)
    if not path.is_absolute():
        path = manifest_root / path
    return str(path.resolve())


def _merge_skill_source_roots(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Merge source roots while preserving configured priority."""

    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _resolve_skill_source_root(value: str) -> tuple[Path | None, str | None]:
    match = _ENV_REFERENCE_PATTERN.fullmatch(value)
    if match:
        inherited = os.environ.get(match.group(1), "").strip()
        if not inherited:
            return None, f"环境变量 {match.group(1)} 未设置。"
        value = inherited
    expanded = os.path.expanduser(os.path.expandvars(value))
    if "$" in expanded:
        return None, "来源路径包含未解析的环境变量。"
    path = Path(expanded).resolve()
    try:
        if not path.is_dir():
            return path, "来源目录不存在或不可读。"
        if (path / "SKILL.md").is_file():
            return path, None
        if any(child.is_dir() and (child / "SKILL.md").is_file() for child in path.iterdir()):
            return path, None
    except OSError as exc:
        return path, f"无法读取来源目录：{exc}"
    return path, "目录中没有可导入的 SKILL.md。"


def _skill_tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        relative_text = path.relative_to(root).as_posix()
        stat = path.stat()
        identity = f"{relative_text}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
        digest.update(len(identity).to_bytes(8, "big"))
        digest.update(identity)
        if relative_text == "SKILL.md":
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _bounded_nonnegative_int(value: Any, fallback: int, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if 0 <= parsed <= maximum else fallback


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
