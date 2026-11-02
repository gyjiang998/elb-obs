"""Reclaim platform-owned Agent CLI process groups after an API crash.

The normal FastAPI shutdown path stops PTYs itself. This Linux-only fallback
signals only same-user process-group leaders that are rooted in this runtime's
projected Agent workspaces, plus orphaned platform MCP servers whose command
and working directory identify this application exactly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import time


@dataclass(frozen=True)
class OwnedProcessGroup:
    pid: int
    command: str
    cwd: Path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def discover_owned_process_groups(
    state_root: Path,
    *,
    app_root: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> list[OwnedProcessGroup]:
    workspace_root = (state_root / "kimi-engine" / "workspaces").resolve()
    normalized_app_root = app_root.resolve() if app_root is not None else None
    if not proc_root.is_dir():
        return []
    discovered: list[OwnedProcessGroup] = []
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid == os.getpid():
            continue
        try:
            if process_dir.stat().st_uid != os.getuid():
                continue
            stat_fields = (process_dir / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            parent_pid = int(stat_fields[1])
            process_group_id = int(stat_fields[2])
            cwd = Path(os.readlink(process_dir / "cwd")).resolve()
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        except (IndexError, OSError, ValueError):
            continue
        platform_workspace_leader = _is_relative_to(cwd, workspace_root)
        orphan_platform_mcp = (
            normalized_app_root is not None
            and parent_pid == 1
            and cwd == normalized_app_root
            and "backend.kimi_mcp_server" in command
        )
        if process_group_id != pid or not (platform_workspace_leader or orphan_platform_mcp):
            continue
        discovered.append(OwnedProcessGroup(pid=pid, command=command[:400], cwd=cwd))
    return sorted(discovered, key=lambda item: item.pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_owned_process_groups(groups: list[OwnedProcessGroup], *, grace_seconds: float = 3.0) -> tuple[int, int, list[int]]:
    terminated = 0
    killed = 0
    for item in groups:
        try:
            os.killpg(item.pid, signal.SIGTERM)
            terminated += 1
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while any(_alive(item.pid) for item in groups) and time.monotonic() < deadline:
        time.sleep(0.05)
    for item in groups:
        if not _alive(item.pid):
            continue
        try:
            os.killpg(item.pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1.0
    while any(_alive(item.pid) for item in groups) and time.monotonic() < deadline:
        time.sleep(0.05)
    return terminated, killed, [item.pid for item in groups if _alive(item.pid)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--terminate", action="store_true")
    args = parser.parse_args()
    groups = discover_owned_process_groups(args.state_root, app_root=args.app_root, proc_root=args.proc_root)
    if not args.terminate:
        print(f"agent-cli cleanup: found={len(groups)}")
        return 0
    terminated, killed, remaining = terminate_owned_process_groups(groups)
    print(
        "agent-cli cleanup: "
        f"found={len(groups)} term_signals={terminated} kill_signals={killed} remaining={len(remaining)}"
    )
    return 1 if remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
