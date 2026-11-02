"""Real local PTY transport tests with a harmless stdin parser, never a user Agent."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from backend.kimi_cli_terminal import KimiCliTerminalError, KimiCliTerminalManager
from backend.scenario_capture import gallery_steps

REPO = Path(__file__).resolve().parents[2]

PARSER = r'''
import hashlib, json, os, sys, tty
tty.setraw(0)
os.write(1, b"READY\n")
buffer = b""
payload = None
while True:
    part = os.read(0, 997)
    if not part: break
    buffer += part
    while True:
        if payload is None:
            if not buffer.startswith(b"\x1b[200~"): break
            end = buffer.find(b"\x1b[201~")
            if end < 0: break
            payload = buffer[6:end]
            buffer = buffer[end + 6:]
        if not buffer: break
        key, buffer = buffer[:1], buffer[1:]
        if key == os.environ["EXPECTED_KEY"].encode():
            # This receipt proves both full UTF-8 paste and declared submit key.
            text = payload.decode("utf-8")
            result = {"sha256": hashlib.sha256(payload).hexdigest(), "characters": len(text), "lines": len(text.splitlines())}
            os.write(1, ("SUBMITTED:" + json.dumps(result) + "\n").encode())
            payload = None
        else:
            os.write(1, b"WRONG_SUBMIT\n")
'''

# Some editor input callbacks treat a completed paste as one event, returning
# before interpreting a key delivered in the same batch. Model that real byte
# stream boundary explicitly instead of assuming os.write == os.read.
BATCHED_EDITOR = r'''
import hashlib, os, select, threading, time, tty
tty.setraw(0)
os.write(1, b"READY\n")
pending = b""
paste = b""
batch_delay = float(os.environ.get("BATCH_DELAY", ".02"))
render_delay = float(os.environ.get("RENDER_DELAY", "0"))
expected_key = os.environ.get("EXPECTED_KEY", "\r").encode()
def progress():
    while True:
        os.write(1, b"TICK\n")
        time.sleep(.02)
while True:
    select.select([0], [], [])
    time.sleep(batch_delay)
    batch = os.read(0, 65536)
    if not batch: break
    if paste or batch.startswith(b"\x1b[200~"):
        paste += batch
        end = paste.find(b"\x1b[201~")
        if end < 0: continue
        pending = paste[6:end]
        paste = b""
        pending.decode("utf-8")
        if os.environ.get("EXIT_ON_PASTE") == "1":
            os._exit(0)
        if os.environ.get("EARLY_RENDER_OUTPUT") == "1":
            os.write(1, b"EDITOR_RENDER_STARTED\n")
        # Model an asynchronous editor whose state has not committed: keys
        # arriving before the final render are consumed without submission.
        render_end = time.monotonic() + render_delay
        while time.monotonic() < render_end:
            available, _, _ = select.select([0], [], [], max(0, render_end - time.monotonic()))
            if available: os.read(0, 65536)
        if os.environ.get("ECHO_PASTE", "1") == "1":
            os.write(1, b"PASTED_ONLY:" + hashlib.sha256(pending).hexdigest().encode() + b"\n")
        if os.environ.get("CONTINUOUS_OUTPUT") == "1":
            threading.Thread(target=progress, daemon=True).start()
        continue
    os.write(1, b"KEY_RECEIVED:" + batch.hex().encode() + b"\n")
    if batch == expected_key and pending:
        os.write(1, b"SUBMITTED:" + hashlib.sha256(pending).hexdigest().encode() + b"\n")
        pending = b""
'''


@unittest.skipUnless(os.name == 'posix', 'PTY requires POSIX')
class AgentCliPageSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / 'fake_cli.py'
        self.script.write_text(PARSER)
        self.manager = KimiCliTerminalManager(state_root=self.root / 'state')

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def session(self, mode='bracketed_paste_cr'):
        terminal = self.manager.create(run_id='run_' + mode, command=[sys.executable, str(self.script)], workspace=self.root,
            environment={**os.environ, 'EXPECTED_KEY': '\n' if mode.endswith('_lf') else '\r'}, cols=100, rows=30,
            adapter_id='internal-team-agent', agent_display_name='Internal Team Agent', agent_session_id='session_' + mode,
            agent_workspace_id='workspace_test', submit_mode=mode, submit_delay_ms=35, submit_fallback_ms=0)
        self.output_until(terminal, b'READY', 0)
        return terminal

    def output_until(self, terminal, marker, cursor, timeout=3):
        output = b''
        deadline = time.monotonic() + timeout
        while marker not in output and time.monotonic() < deadline:
            chunks, _ = terminal.events(cursor, wait_seconds=.1)
            if chunks:
                cursor = chunks[-1].sequence
                output += b''.join(chunk.data for chunk in chunks)
        self.assertIn(marker, output)
        return output, cursor

    def test_long_chinese_multiline_reaches_cli_and_submits_once_for_cr_and_lf(self):
        message = '请分析第七步的负载特征。\n' + ('中文字段：内存、工具、trace，🙂。\n' * 1800)
        expected = message.strip().encode()
        for mode in ('bracketed_paste_cr', 'bracketed_paste_lf'):
            with self.subTest(mode=mode):
                terminal = self.session(mode)
                self.assertTrue(terminal.send_command(message, command_id='page-1'))
                self.assertFalse(terminal.send_command(message, command_id='page-1'))
                output, cursor = self.output_until(terminal, b'SUBMITTED:', 0)
                receipt = json.loads(output.split(b'SUBMITTED:', 1)[1].split(b'\n', 1)[0])
                self.assertEqual(receipt['sha256'], hashlib.sha256(expected).hexdigest())
                self.assertEqual(receipt['characters'], len(message.strip()))
                self.assertGreater(receipt['lines'], 1000)
                more, _ = terminal.events(cursor, wait_seconds=.1)
                self.assertNotIn(b'SUBMITTED:', b''.join(row.data for row in more))

    def test_duplicate_ack_waits_for_original_submit_and_failure_can_retry(self):
        terminal = self.session()
        original_write = terminal.write
        entered, release, retry_finished = threading.Event(), threading.Event(), threading.Event()
        results, failures = [], []
        first = True

        def delayed_write(data):
            nonlocal first
            if first:
                first = False
                entered.set()
                if not release.wait(2): raise RuntimeError('test release timed out')
                raise RuntimeError('simulated first paste failure')
            return original_write(data)

        terminal.write = delayed_write
        def deliver():
            try: results.append(terminal.send_command('重试任务', command_id='same-id'))
            except RuntimeError as error: failures.append(str(error))
        initial = threading.Thread(target=deliver)
        retry = threading.Thread(target=lambda: (deliver(), retry_finished.set()))
        try:
            initial.start()
            self.assertTrue(entered.wait(1))
            retry.start()
            self.assertFalse(retry_finished.wait(.05), 'duplicate must not ack an unfinished paste')
            release.set()
            initial.join(2); retry.join(2)
            self.assertEqual(results, [True])
            self.assertEqual(failures, ['simulated first paste failure'])
            self.output_until(terminal, b'SUBMITTED:', 0)
        finally:
            release.set()
            initial.join(2); retry.join(2)
            terminal.write = original_write

    def test_concurrent_messages_remain_complete_and_serialized(self):
        terminal = self.session()
        prompts = ['并发任务甲。\n' * 70, '并发任务乙。\n' * 80]
        failures = []
        barrier = threading.Barrier(3)

        def deliver(index):
            barrier.wait()
            try:
                terminal.send_command(prompts[index], command_id=f'parallel-{index}')
            except Exception as error:
                failures.append(error)

        workers = [threading.Thread(target=deliver, args=(index,)) for index in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(4)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        output, cursor = self.output_until(terminal, b'SUBMITTED:', 0)
        deadline = time.monotonic() + 2
        while output.count(b'SUBMITTED:') < 2 and time.monotonic() < deadline:
            chunks, _ = terminal.events(cursor, wait_seconds=.1)
            if chunks:
                cursor = chunks[-1].sequence
                output += b''.join(chunk.data for chunk in chunks)
        receipts = [json.loads(line.split(b'SUBMITTED:', 1)[1])
                    for line in output.splitlines() if b'SUBMITTED:' in line]
        self.assertEqual(len(receipts), 2)
        self.assertEqual({receipt['sha256'] for receipt in receipts},
                         {hashlib.sha256(prompt.strip().encode()).hexdigest() for prompt in prompts})

    def scenario_prompt(self):
        draft = {"template_id": "gallery_17_steps_v1", "title": "图库 17 步", "natural_language_steps": "",
                 "steps": gallery_steps(), "profile": json.loads((REPO / "tools/scenario_capture/gallery_17_profile.json").read_text()),
                 "capture": {"selected_step_ids": ["step_07"], "collectors": ["trace", "hilog", "pmu"], "preparation_mode": "auto",
                             "pmu": {"binary_path": "/data/local/tmp/gallery_thread_pmu_focus", "period_us": 100,
                                     "duration_ms": 10000, "group": "cache", "reader_cpu": 0}}}
        result = subprocess.run(["node", str(REPO / "tools/tests/scenario-capture-task-fixture.mjs")],
            input=json.dumps({"draft": draft}), text=True, capture_output=True, check=True, timeout=10)
        task = json.loads(result.stdout)
        self.assertEqual(task["kind"], "scenario_capture")
        self.assertEqual(task["context"]["plan_draft"], draft)
        self.assertGreater(len(task["message"].encode()), 4500)
        return task["message"]

    def editor_session(self, suffix, *, mode="bracketed_paste_cr", environment=None, delay=0):
        self.script.write_text(BATCHED_EDITOR)
        terminal = self.manager.create(run_id='run_editor_' + suffix,
            command=[sys.executable, str(self.script)], workspace=self.root,
            environment={**os.environ, "EXPECTED_KEY": "\n" if mode.endswith("_lf") else "\r", **(environment or {})},
            cols=100, rows=30, adapter_id='any-bracketed-agent', agent_display_name='Fixture Editor',
            agent_session_id='session_' + suffix, agent_workspace_id='fixture',
            submit_mode=mode, submit_delay_ms=delay, submit_fallback_ms=0)
        self.output_until(terminal, b'READY', 0)
        return terminal

    def assert_submitted_once(self, terminal, prompt, *, timeout=3):
        digest = hashlib.sha256(prompt.encode()).hexdigest().encode()
        output, cursor = self.output_until(terminal, b'SUBMITTED:' + digest, 0, timeout=timeout)
        more, _ = terminal.events(cursor, wait_seconds=.15)
        output += b''.join(item.data for item in more)
        self.assertEqual(output.count(b'SUBMITTED:'), 1)
        self.assertEqual(output.count(b'KEY_RECEIVED:'), 1)
        diagnostics = terminal.public()['last_submission']
        self.assertEqual(diagnostics['delivery_state'], 'delivered')
        self.assertEqual(diagnostics['submit_key_count'], 1)
        self.assertNotIn(prompt, json.dumps(diagnostics, ensure_ascii=False))
        return diagnostics

    def test_real_scenario_button_payload_has_distinct_paste_and_submit_events(self):
        message = self.scenario_prompt()
        # Skill metadata changes the prefix, not the paste/submit protocol.
        for prefix in ('', '请加载 scenario-step-capture Skill。加载完成后执行当前阶段任务：\n'):
            prompt = prefix + message
            for delay in ('.02', '.12', '.30'):
                with self.subTest(prefix=bool(prefix), batch_delay=delay):
                    terminal = self.editor_session(f'batch_{bool(prefix)}_{delay}', environment={'BATCH_DELAY': delay})
                    self.assertEqual(terminal.public()['effective_submit_delay_ms'], 1000)
                    terminal.send_command(prompt, command_id='scenario-button')
                    diagnostics = self.assert_submitted_once(terminal, prompt)
                    self.assertEqual(diagnostics['strategy_version'], 'pty_output_settle/v1')
                    self.assertEqual(diagnostics['wait_reason'], 'output_settled')
                    self.assertTrue(diagnostics['output_observed'])
                    self.assertGreaterEqual(diagnostics['actual_wait_ms'], 1000)

    def test_old_35ms_submit_can_be_swallowed_by_slow_final_paste_batch(self):
        prompt = self.scenario_prompt()
        terminal = self.editor_session('old_35ms', environment={'BATCH_DELAY': '.30'})
        # Reproduce the former transport rather than weakening the new waiter.
        terminal._wait_for_paste_settle = lambda _: time.sleep(.035)
        terminal.send_command(prompt, command_id='old-scenario-button')
        digest = hashlib.sha256(prompt.encode()).hexdigest().encode()
        output, cursor = self.output_until(terminal, b'PASTED_ONLY:' + digest, 0)
        more, _ = terminal.events(cursor, wait_seconds=.5)
        self.assertNotIn(b'SUBMITTED:', output + b''.join(item.data for item in more))

    def test_delayed_editor_render_waits_before_sending_cr_and_lf(self):
        prompt = self.scenario_prompt()
        for mode in ('bracketed_paste_cr', 'bracketed_paste_lf'):
            with self.subTest(mode=mode):
                terminal = self.editor_session('render_' + mode, mode=mode,
                    environment={'RENDER_DELAY': '.55', 'EARLY_RENDER_OUTPUT': '1'})
                terminal.send_command(prompt, command_id='delayed-render')
                diagnostics = self.assert_submitted_once(terminal, prompt)
                self.assertEqual(diagnostics['wait_reason'], 'output_settled')
                self.assertGreaterEqual(diagnostics['actual_wait_ms'], 1000)
                self.assertEqual(diagnostics['submit_key'], mode.rsplit('_', 1)[1])

    def test_early_render_output_is_not_input_ready_at_350ms(self):
        prompt = self.scenario_prompt()
        terminal = self.editor_session('early_echo_350',
            environment={'RENDER_DELAY': '.55', 'EARLY_RENDER_OUTPUT': '1'})
        # The preceding settling heuristic incorrectly treated an early paint
        # as sufficient evidence. Reproduce its former minimum as a control.
        terminal.effective_submit_delay_ms = 350
        terminal.send_command(prompt, command_id='early-echo-old-minimum')
        digest = hashlib.sha256(prompt.encode()).hexdigest().encode()
        output, cursor = self.output_until(terminal, b'PASTED_ONLY:' + digest, 0)
        more, _ = terminal.events(cursor, wait_seconds=.15)
        self.assertNotIn(b'SUBMITTED:', output + b''.join(item.data for item in more))
        diagnostics = terminal.public()['last_submission']
        self.assertEqual(diagnostics['wait_reason'], 'output_settled')
        self.assertTrue(diagnostics['output_observed'])
        self.assertLess(diagnostics['actual_wait_ms'], 550)
        # Matches the reported symptom: one manual Enter now submits the same
        # intact prompt, without repasting or invoking an external Agent.
        terminal.write(b'\r')
        self.output_until(terminal, b'SUBMITTED:' + digest, cursor)

    def test_editor_without_echo_uses_bounded_conservative_wait(self):
        prompt = '无回显编辑器也要完整提交。\n第二行。'
        terminal = self.editor_session('no_echo', environment={'ECHO_PASTE': '0', 'RENDER_DELAY': '.55'})
        terminal.send_command(prompt, command_id='no-echo')
        diagnostics = self.assert_submitted_once(terminal, prompt)
        self.assertEqual(diagnostics['wait_reason'], 'no_output_wait_elapsed')
        self.assertFalse(diagnostics['output_observed'])
        self.assertGreaterEqual(diagnostics['actual_wait_ms'], 1000)
        self.assertLess(diagnostics['actual_wait_ms'], 1600)

    def test_continuous_status_output_cannot_block_submission_forever(self):
        prompt = '持续进度输出仍需有界等待。'
        terminal = self.editor_session('continuous', environment={'CONTINUOUS_OUTPUT': '1'})
        terminal.send_command(prompt, command_id='continuous')
        diagnostics = self.assert_submitted_once(terminal, prompt)
        self.assertEqual(diagnostics['wait_reason'], 'maximum_wait_reached')
        self.assertTrue(diagnostics['output_observed'])
        self.assertGreaterEqual(diagnostics['actual_wait_ms'], 3000)
        self.assertLess(diagnostics['actual_wait_ms'], 3600)

    def test_exit_during_paste_wait_interrupts_before_submit(self):
        terminal = self.editor_session('exits', environment={'EXIT_ON_PASTE': '1'})
        started = time.monotonic()
        with self.assertRaises(KimiCliTerminalError) as failure:
            terminal.send_command('这条消息不会执行。', command_id='exit-before-enter')
        self.assertEqual(failure.exception.code, 'AGENT_CLI_SESSION_EXITED')
        self.assertLess(time.monotonic() - started, .8)
        diagnostics = terminal.public()['last_submission']
        self.assertEqual(diagnostics['wait_reason'], 'session_exited')
        self.assertEqual(diagnostics['delivery_state'], 'failed')
        self.assertEqual(diagnostics['submit_key_count'], 0)
        self.assertNotIn('exit-before-enter', terminal._command_id_set)

    def test_larger_adapter_delay_remains_a_minimum(self):
        prompt = '适配器配置的较长延迟继续生效。'
        terminal = self.editor_session('long_delay', delay=1600)
        terminal.send_command(prompt, command_id='long-delay')
        diagnostics = self.assert_submitted_once(terminal, prompt)
        self.assertEqual(terminal.public()['effective_submit_delay_ms'], 1600)
        self.assertGreaterEqual(diagnostics['actual_wait_ms'], 1600)

    def test_plain_text_mode_retains_configured_delay(self):
        terminal = self.session('plain_text_lf')
        writes = []
        terminal.write = writes.append
        terminal.send_command('plain input', command_id='plain')
        self.assertEqual(writes, [b'plain input', b'\n'])
        diagnostics = terminal.public()['last_submission']
        self.assertEqual(diagnostics['strategy_version'], 'plain_delay/v1')
        self.assertEqual(diagnostics['wait_reason'], 'configured_delay_elapsed')
        self.assertEqual(diagnostics['submit_key_count'], 1)
        self.assertGreaterEqual(diagnostics['actual_wait_ms'], 35)
        self.assertLess(diagnostics['actual_wait_ms'], 350)


if __name__ == '__main__': unittest.main()
