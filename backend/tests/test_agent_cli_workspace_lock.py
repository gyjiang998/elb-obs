from __future__ import annotations

import threading
import time
import unittest

from backend import app as platform_app


class AgentCliWorkspaceLockTests(unittest.TestCase):
    def test_same_run_workspace_projection_is_serialized(self) -> None:
        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()
        start = threading.Barrier(3)

        def project() -> None:
            nonlocal active, maximum_active
            start.wait()
            with platform_app._agent_cli_workspace_lock("run-concurrent-restore"):
                with counter_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.03)
                with counter_lock:
                    active -= 1

        workers = [threading.Thread(target=project) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(timeout=1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(maximum_active, 1)

    def test_different_runs_receive_different_locks(self) -> None:
        self.assertIsNot(
            platform_app._agent_cli_workspace_lock("run-a"),
            platform_app._agent_cli_workspace_lock("run-b"),
        )


if __name__ == "__main__":
    unittest.main()
