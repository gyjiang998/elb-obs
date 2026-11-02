from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from backend.agent_runtime import AgentRunStore


class AgentRunStoreAsyncWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_wait_wakes_when_event_is_appended(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AgentRunStore(Path(temporary) / "runs")
            run = store.create(
                project_id="local-default",
                goal="异步事件等待",
                initial_input="等待",
                mode="page_agent",
                context={},
                budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
                action_catalog_version="test",
                skill_versions=[],
            )
            run_id = str(run["run_id"])

            async def append_later() -> None:
                await asyncio.sleep(0.02)
                store.append_event(run_id, "test.async", "running", {"ready": True})

            writer = asyncio.create_task(append_later())
            await store.wait_for_events_async(run_id, 1, 1)
            await writer

            events = store.events(run_id, after_sequence=1, limit=10)
            self.assertEqual([event["event_type"] for event in events], ["test.async"])


if __name__ == "__main__":
    unittest.main()
