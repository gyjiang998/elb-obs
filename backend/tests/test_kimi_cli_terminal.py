from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.kimi_cli_terminal import JsonlCliUsageLedger, KimiCliTerminalManager, KimiCliUsageLedger


class KimiCliUsageLedgerTests(unittest.TestCase):
    def test_aggregates_main_subagent_and_appended_usage_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_path = Path(temporary) / "session_test"
            main_wire = session_path / "agents" / "main" / "wire.jsonl"
            child_wire = session_path / "agents" / "agent-0" / "wire.jsonl"
            main_wire.parent.mkdir(parents=True)
            child_wire.parent.mkdir(parents=True)
            main_wire.write_text(
                json.dumps({
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 20, "inputCacheRead": 80, "inputCacheCreation": 0, "output": 10},
                }) + "\n" + json.dumps({"type": "context.append_loop_event", "usage": {"inputOther": 999}}) + "\n",
                encoding="utf-8",
            )
            child_wire.write_text(
                json.dumps({
                    "type": "usage.record",
                    "usageScope": "session",
                    "usage": {"inputOther": 5, "inputCacheRead": 5, "inputCacheCreation": 10, "output": 2},
                }) + "\n",
                encoding="utf-8",
            )
            ledger = KimiCliUsageLedger(session_path)

            first = ledger.public()
            self.assertEqual(first["input_tokens"], 120)
            self.assertEqual(first["output_tokens"], 12)
            self.assertEqual(first["total_tokens"], 132)
            self.assertEqual(first["cached_input_tokens"], 85)
            self.assertEqual(first["model_calls"], 2)
            self.assertAlmostEqual(first["cache_hit_rate"], 85 / 120)

            with main_wire.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 10, "inputCacheRead": 90, "inputCacheCreation": 0, "output": 8},
                }) + "\n")
            second = ledger.public()
            self.assertEqual(second["input_tokens"], 220)
            self.assertEqual(second["output_tokens"], 20)
            self.assertEqual(second["model_calls"], 3)


class JsonlCliUsageLedgerTests(unittest.TestCase):
    def test_aggregates_canonical_and_openai_compatible_usage_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "token-usage.jsonl"
            journal.write_text(
                json.dumps({
                    "schema_version": "agent_cli_usage_event/v1",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cached_input_tokens": 60,
                    },
                }) + "\n" + json.dumps({
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 8,
                        "prompt_tokens_details": {"cached_tokens": 10},
                    },
                }) + "\n",
                encoding="utf-8",
            )
            ledger = JsonlCliUsageLedger(journal)

            first = ledger.public()
            self.assertEqual(first["input_tokens"], 150)
            self.assertEqual(first["output_tokens"], 28)
            self.assertEqual(first["cached_input_tokens"], 70)
            self.assertEqual(first["model_calls"], 2)
            self.assertAlmostEqual(first["cache_hit_rate"], 70 / 150)

            with journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"usage": {"inputTokens": 25, "outputTokens": 5}}) + "\n")
            second = ledger.public()
            self.assertEqual(second["total_tokens"], 208)
            self.assertEqual(second["model_calls"], 3)
            self.assertEqual(second["source"], "standard_jsonl_usage_events")


@unittest.skipUnless(os.name == "posix", "PTY terminal tests require a POSIX backend")
class KimiCliTerminalManagerTests(unittest.TestCase):
    def test_command_paste_and_submit_are_separate_pty_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "fake-agent"
            binary.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n", encoding="utf-8")
            binary.chmod(0o755)
            manager = KimiCliTerminalManager(state_root=root / "state")
            session = manager.create(
                run_id="run_split_submit",
                binary=binary,
                workspace=root,
                environment=os.environ.copy(),
                cols=80,
                rows=24,
                adapter_id="custom-agent",
                agent_display_name="Custom Agent",
                agent_session_id="session_split_submit",
                agent_workspace_id="workspace_split_submit",
                submit_mode="bracketed_paste_cr",
                submit_delay_ms=0,
                submit_fallback_ms=1,
            )
            captured: list[bytes] = []
            original_write = session.write
            try:
                session.write = captured.append  # type: ignore[method-assign]
                self.assertTrue(session.send_command("route this code", command_id="split-1"))
                self.assertEqual(captured, [b"\x1b[200~route this code\x1b[201~", b"\r", b"\r"])
                first_delivery = session.public()["last_submission"]
                self.assertEqual(first_delivery["command_id"], "split-1")
                self.assertEqual(first_delivery["delivery_state"], "delivered")
                self.assertEqual(first_delivery["submit_key_count"], 2)
                self.assertFalse(session.send_command("route this code", command_id="split-1"))
                self.assertEqual(session.public()["last_submission"], first_delivery)
                self.assertEqual(len(captured), 3)
                session.submit_mode = "bracketed_paste_lf"
                captured.clear()
                self.assertTrue(session.send_command("run analysis", command_id="split-2"))
                self.assertEqual(captured, [b"\x1b[200~run analysis\x1b[201~", b"\n", b"\n"])
                self.assertEqual(session.public()["last_submission"]["submit_key"], "lf")
            finally:
                session.write = original_write  # type: ignore[method-assign]
                manager.close()

    def test_real_pty_stream_input_resize_and_idempotent_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "fake-kimi"
            binary.write_text(
                "#!/bin/sh\n"
                "printf 'FAKE_KIMI_READY\\r\\n'\n"
                "while IFS= read -r line; do printf 'RX:%s\\r\\n' \"$line\"; done\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            manager = KimiCliTerminalManager(state_root=root / "state")
            session = manager.create(
                run_id="run_test",
                kimi_session_id="session_test",
                kimi_workspace_id="workspace_test",
                binary=binary,
                workspace=root,
                environment=os.environ.copy(),
                cols=80,
                rows=24,
            )
            try:
                chunks, _ = session.events(0, wait_seconds=2)
                self.assertIn(b"FAKE_KIMI_READY", b"".join(item.data for item in chunks))
                self.assertFalse(session.wait_for_exit(0.01))
                cursor = chunks[-1].sequence

                session.write(b"hello\r")
                chunks, _ = session.events(cursor, wait_seconds=2)
                output = b"".join(item.data for item in chunks)
                self.assertIn(b"hello", output)
                cursor = chunks[-1].sequence

                self.assertTrue(session.send_command("first\nsecond", command_id="command-1"))
                self.assertFalse(session.send_command("first\nsecond", command_id="command-1"))
                command_output = b""
                deadline = time.monotonic() + 2
                while b"first" not in command_output and time.monotonic() < deadline:
                    chunks, _ = session.events(cursor, wait_seconds=0.2)
                    if chunks:
                        cursor = chunks[-1].sequence
                        command_output += b"".join(item.data for item in chunks)
                self.assertIn(b"first", command_output)

                session.resize(120, 40)
                self.assertEqual(session.public()["cols"], 120)
                self.assertEqual(session.public()["rows"], 40)
                self.assertEqual(session.public()["transport"], "pty")
                self.assertIsNone(session.public()["idle_timeout_seconds"])
                self.assertEqual(session.public()["lifetime_policy"], "until_explicit_stop_or_backend_shutdown")
                self.assertTrue((root / "state" / session.terminal_session_id / "terminal.ansi").is_file())
            finally:
                manager.close()
                deadline = time.monotonic() + 2
                while session.public()["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(session.public()["state"], "exited")

    def test_failed_explicit_retry_does_not_replay_already_delivered_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "fake-agent"
            binary.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n", encoding="utf-8")
            binary.chmod(0o755)
            manager = KimiCliTerminalManager(state_root=root / "state")
            session = manager.create(
                run_id="run_failed_retry", binary=binary, workspace=root,
                environment=os.environ.copy(), cols=80, rows=24,
                adapter_id="custom-agent", agent_session_id="retry", agent_workspace_id="retry",
                submit_mode="plain_text_cr", submit_fallback_ms=1,
            )
            writes = []

            def fail_retry(data):
                if len(writes) == 2:
                    raise RuntimeError("retry write failed")
                writes.append(data)

            try:
                session.write = fail_retry
                with self.assertRaisesRegex(RuntimeError, "retry write failed"):
                    session.send_command("already sent", command_id="retry-key")
                diagnostics = session.public()["last_submission"]
                self.assertEqual(diagnostics["delivery_state"], "failed")
                self.assertEqual(diagnostics["submit_key_count"], 1)
                self.assertFalse(session.send_command("already sent", command_id="retry-key"))
                self.assertEqual(writes, [b"already sent", b"\r"])
            finally:
                manager.close()


@unittest.skipUnless(os.name == "posix", "PTY terminal tests require a POSIX backend")
class KimiCliTerminalAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_output_wait_wakes_without_blocking_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "fake-agent"
            binary.write_text("#!/bin/sh\nwhile IFS= read -r line; do printf 'RX:%s\\r\\n' \"$line\"; done\n", encoding="utf-8")
            binary.chmod(0o755)
            manager = KimiCliTerminalManager(state_root=root / "state")
            session = manager.create(
                run_id="run_async_wait",
                binary=binary,
                workspace=root,
                environment=os.environ.copy(),
                cols=80,
                rows=24,
                adapter_id="custom-agent",
                agent_display_name="Custom Agent",
                agent_session_id="session_async_wait",
                agent_workspace_id="workspace_async_wait",
            )
            try:
                initial, _ = await session.events_async(0, wait_seconds=0.05)
                cursor = initial[-1].sequence if initial else 0

                async def write_later() -> None:
                    await asyncio.sleep(0.02)
                    session.write(b"hello\r")

                writer = asyncio.create_task(write_later())
                chunks, _ = await session.events_async(cursor, wait_seconds=1)
                await writer
                self.assertIn(b"hello", b"".join(chunk.data for chunk in chunks))
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
