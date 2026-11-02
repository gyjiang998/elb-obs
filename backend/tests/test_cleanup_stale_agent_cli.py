from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from backend.tools.cleanup_stale_agent_cli import discover_owned_process_groups


class CleanupStaleAgentCliTests(unittest.TestCase):
    def _process(self, proc_root: Path, pid: int, *, pgrp: int, cwd: Path, command: str = "kimi-code") -> None:
        process = proc_root / str(pid)
        process.mkdir(parents=True)
        (process / "stat").write_text(
            f"{pid} ({command}) S 1 {pgrp} {pgrp} 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
            encoding="utf-8",
        )
        (process / "cmdline").write_bytes(command.encode("utf-8") + b"\0--auto\0")
        os.symlink(cwd, process / "cwd")

    def test_discovers_only_platform_workspace_process_group_leaders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "runtime" / "state"
            workspace = state_root / "kimi-engine" / "workspaces" / "run_owned"
            outside = root / "other"
            workspace.mkdir(parents=True)
            outside.mkdir()
            proc_root = root / "proc"
            proc_root.mkdir()
            self._process(proc_root, 1201, pgrp=1201, cwd=workspace)
            self._process(proc_root, 1202, pgrp=1201, cwd=workspace, command="mcp-child")
            self._process(proc_root, 1301, pgrp=1301, cwd=outside)
            self._process(proc_root, 1401, pgrp=1401, cwd=root, command="python -m backend.kimi_mcp_server")

            groups = discover_owned_process_groups(state_root, app_root=root, proc_root=proc_root)

            self.assertEqual([item.pid for item in groups], [1201, 1401])
            self.assertEqual(groups[0].cwd, workspace.resolve())


if __name__ == "__main__":
    unittest.main()
