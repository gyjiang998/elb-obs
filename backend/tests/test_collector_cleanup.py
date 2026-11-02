"""Execute recovery shell against isolated process/trace fixtures, never a phone."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools.scenario_capture.collector_cleanup import (
    CollectorSession, _PRIVATE_TRACE_PREFLIGHT_SHELL, step_shell_prelude,
)
from tools.scenario_capture.gallery import CapturePreparationError
from tools.scenario_capture.runner import cancel_owned_capture


class CaptureCancelled(Exception): pass


class ShellDevice:
    """Real POSIX shell and signals; /proc observations are fixture-scoped on macOS."""
    def __init__(self, root):
        self.root = root
        self.proc = root / 'proc'
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'sys/kernel/random/boot_id').write_text('fixture-boot\n')
        (self.proc / 'uptime').write_text('1000.00 0.00\n')
        self.calls = []
        self.cancelled = False
        self.bin = root / 'bin'; self.bin.mkdir()
        # Reproduce the device's limited utilities: neither tr nor awk exists.
        for tool in ('cat', 'sed', 'od', 'cut', 'grep', 'mkdir', 'rmdir', 'rm', 'mv', 'readlink', 'sleep', 'cp'):
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.trace = root / 'tracing'; self.trace.mkdir()
        (self.trace / 'tracing_on').write_text('0\n'); (self.trace / 'trace').write_text('')
        self.hitrace_calls = root / 'hitrace.calls'
        (self.bin / 'hitrace').write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_HITRACE_CALLS"\n'
            '[ "$1" = --trace_finish_nodump ] && exit 0\n'
            'cp "$TEST_TRACE/trace" "$3"\nprintf "0\\n" > "$TEST_TRACE/tracing_on"\n')
        (self.bin / 'hitrace').chmod(0o755)
        self.children = []

    def check_cancelled(self):
        if self.cancelled: raise CaptureCancelled('fixture cancelled')

    def shell(self, command, timeout=40, *, cleanup=False):
        self.calls.append((command, timeout, cleanup))
        if not cleanup: self.check_cancelled()
        command = command.replace('/proc/', str(self.proc) + '/')
        prefix = f'''mkdir -p {shlex.quote(str(self.proc))}/$$
printf '%s (fixture) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 888\\n' "$$" > {shlex.quote(str(self.proc))}/$$/stat
'''
        env = {**os.environ, 'PATH': str(self.bin), 'TEST_TRACE': str(self.trace),
               'TEST_HITRACE_CALLS': str(self.hitrace_calls)}
        process = subprocess.run(['/bin/sh', '-c', prefix + command], capture_output=True, text=True, env=env, timeout=timeout)
        if process.returncode: raise RuntimeError(process.stderr or process.stdout or str(process.returncode))
        return process.stdout

    def collect(self, remote, destination, *, timeout=600, cleanup=True):
        self.calls.append(('collect ' + remote, timeout, cleanup)); return {'collected': True}

    def spawn(self, commandline, *, stubborn=False):
        child = subprocess.Popen(['sh', '-c', "trap '' TERM; while :; do sleep .1; done"] if stubborn else ['sleep', '30'])
        directory = self.proc / str(child.pid); directory.mkdir()
        (directory / 'stat').write_text(f'{child.pid} (fixture child) S ' + ' '.join(['0'] * 18) + ' 1234\n')
        (directory / 'cmdline').write_text(commandline.replace(' ', '\0') + '\0')
        self.children.append(child)
        def reap():
            child.wait()
            (directory / 'stat').unlink(missing_ok=True)
        threading.Thread(target=reap, daemon=True).start()
        return child

    def close(self):
        for child in self.children:
            if child.poll() is None: child.kill()
            child.wait()


class CollectorCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.client = ShellDevice(self.root)
        self.base = self.root / 'runs'; self.base.mkdir()
        self.lock = self.root / 'session.lock'
        self.addCleanup(self.temp.cleanup); self.addCleanup(self.client.close)

    def session(self, name='capture_one', *, terminal=()):
        return CollectorSession(self.client, name, str(self.base / name), {}, lock_path=str(self.lock),
                                terminal_run_ids=terminal, trace_roots=(str(self.client.trace),))

    def ledger(self, session, role, child, *, ticks='1234', token=None):
        step = self.base / session.run_id / 'step_01'; (step / 'collector_processes').mkdir(parents=True, exist_ok=True)
        (step / 'collector_processes' / role).write_text(f'{child.pid} {ticks} fixture-boot {token or session.token}\n')
        return step

    def expire(self): (self.lock / 'lease_until').write_text('999\n')

    def test_exclusive_acquire_refresh_and_release_preserve_output(self):
        first = self.session(); first.acquire()
        output = self.base / first.run_id / 'keep.log'; output.write_text('retained')
        second = self.session('capture_two')
        with self.assertRaises(CapturePreparationError): second.acquire()
        self.assertEqual(second.record['reason'], 'active_lease')
        first.refresh(600)
        self.assertEqual(int((self.lock / 'lease_until').read_text()), 1663)
        first.release()
        self.assertEqual(first.record['status'], 'released'); self.assertFalse(self.lock.exists())
        self.assertEqual(output.read_text(), 'retained')
        second.acquire(); second.release()

    def test_expired_session_reclaims_registered_orphan_only(self):
        old = self.session(); old.acquire()
        owned = self.client.spawn('hilog -P 10'); unrelated = self.client.spawn('hilog -P 20')
        self.ledger(old, 'logger', owned)
        self.expire()
        new = self.session('capture_two'); new.acquire()
        self.assertIsNotNone(owned.poll()); self.assertIsNone(unrelated.poll())
        self.assertEqual(new.record['status'], 'acquired')
        new.release()

    def test_reused_pid_starttime_is_never_signalled(self):
        old = self.session(); old.acquire(); external = self.client.spawn('external workload')
        self.ledger(old, 'logger', external, ticks='9999'); self.expire()
        new = self.session('capture_two'); new.acquire()
        self.assertIsNone(external.poll()); new.release()

    def test_expired_lease_does_not_kill_active_capture_owner(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'
        owner = self.client.spawn(f'sh {step}/capture.sh'); self.ledger(old, 'owner', owner)
        self.expire(); new = self.session('capture_two')
        with self.assertRaises(CapturePreparationError): new.acquire()
        self.assertEqual(new.record['status'], 'pending'); self.assertIsNone(owner.poll())
        old.release(); self.assertIsNotNone(owner.poll())

    def test_terminal_old_lock_owner_can_recover_but_unverified_legacy_cannot(self):
        self.lock.mkdir(); (self.lock / 'owner').write_text('capture_old')
        step = self.base / 'capture_old' / 'step_01'; step.mkdir(parents=True)
        owner = self.client.spawn(f'sh {step}/capture.sh'); (step / 'owner.pid').write_text(str(owner.pid))
        new = self.session('capture_two')
        with self.assertRaises(CapturePreparationError): new.acquire()
        self.assertEqual(new.record['reason'], 'legacy_owner_unverified'); self.assertIsNone(owner.poll())
        permitted = self.session('capture_three', terminal=('capture_old',)); permitted.acquire()
        self.assertIsNotNone(owner.poll()); permitted.release()

    def test_foreign_trace_marker_is_preserved_and_lock_retained(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'; (step / 'collector_processes').mkdir(parents=True)
        (step / 'trace.owner').write_text(f'{self.client.trace} {old.token} owned\n')
        (self.client.trace / 'tracing_on').write_text('1\n'); (self.client.trace / 'trace').write_text('external trace\n')
        old.release()
        self.assertEqual(old.record['status'], 'pending'); self.assertTrue(self.lock.exists())
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '1')
        self.assertFalse((step / 'trace.recovered.ftrace').exists())

    def test_owned_trace_saved_and_finished_while_other_logs_are_retained(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'; (step / 'collector_processes').mkdir(parents=True)
        (step / 'trace.owner').write_text(f'{self.client.trace} {old.token} owned\n')
        (step / 'pmu.csv').write_text('original pmu')
        content = f'event HETERO_TRACE_OWNER_{old.token}|1\noriginal trace\n'
        (self.client.trace / 'tracing_on').write_text('1\n'); (self.client.trace / 'trace').write_text(content)
        old.release()
        self.assertEqual(old.record['status'], 'released', old.record)
        recovered = list(step.glob('trace.recovered.*.ftrace'))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_text(), content)
        self.assertEqual((step / 'pmu.csv').read_text(), 'original pmu')

    def test_global_claim_recovers_platform_trace_after_session_lock_was_lost(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'; step.mkdir(parents=True)
        script = [f'out={shlex.quote(str(step))}',
                  *step_shell_prelude(old.run_id, str(step), old.token, lock_path=str(self.lock)),
                  f'mark_trace_intent {shlex.quote(str(self.client.trace))}',
                  f'mark_trace_owned {shlex.quote(str(self.client.trace))}']
        self.client.shell('\n'.join(script))
        content = f'event HETERO_TRACE_OWNER_{old.token}|1\nplatform trace\n'
        (self.client.trace / 'tracing_on').write_text('1\n')
        (self.client.trace / 'trace').write_text(content)
        shutil.rmtree(self.lock)

        current = self.session('capture_two')
        current.acquire()
        self.assertEqual(current.record['operations'][-1]['operation'], 'trace_reconcile')
        self.assertEqual(current.record['operations'][-1]['status'], 'recovered')
        recovered = list(step.glob(f'trace.recovered.{current.token}.ftrace'))
        self.assertEqual([item.read_text() for item in recovered], [content])
        self.assertFalse(Path(str(self.lock) + '.trace_owner').exists())
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '0')
        current.release()

    def test_global_claim_never_stops_trace_without_matching_buffer_marker(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'; step.mkdir(parents=True)
        script = [f'out={shlex.quote(str(step))}',
                  *step_shell_prelude(old.run_id, str(step), old.token, lock_path=str(self.lock)),
                  f'mark_trace_intent {shlex.quote(str(self.client.trace))}',
                  f'mark_trace_owned {shlex.quote(str(self.client.trace))}']
        self.client.shell('\n'.join(script))
        (self.client.trace / 'tracing_on').write_text('1\n')
        (self.client.trace / 'trace').write_text('external trace without platform marker\n')
        shutil.rmtree(self.lock)

        current = self.session('capture_two')
        with self.assertRaises(CapturePreparationError):
            current.acquire()
        self.assertEqual(current.record['reason'], 'trace_ownership_marker_missing')
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '1')
        self.assertTrue(Path(str(self.lock) + '.trace_owner').exists())

    def test_private_device_preflight_finishes_unclaimed_trace_and_retains_diagnostic(self):
        (self.client.trace / 'tracing_on').write_text('1\n')
        (self.client.trace / 'trace').write_text('trace left by an older unregistered tool\n')
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        current.acquire()

        operation = current.record['operations'][-1]
        self.assertEqual(operation['operation'], 'private_trace_preflight')
        self.assertEqual(operation['status'], 'recovered')
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '0')
        recovered = Path(current.record['private_trace_recovery_path'])
        self.assertTrue(recovered.is_file())
        self.assertIn('older unregistered tool', recovered.read_text())
        current.release()

    def test_private_device_preflight_resets_idle_trace_service_before_capture(self):
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        current.acquire()

        operation = current.record['operations'][-1]
        self.assertEqual(operation['operation'], 'private_trace_preflight')
        self.assertEqual(operation['status'], 'recovered')
        reset_log = Path(current.record['private_trace_recovery_path'])
        self.assertTrue(reset_log.is_file())
        self.assertEqual(self.client.hitrace_calls.read_text().splitlines(), ['--trace_finish_nodump'])
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '0')
        current.release()

    def test_shared_device_never_resets_unclaimed_idle_trace_service(self):
        current = self.session()

        current.acquire()

        self.assertFalse(self.client.hitrace_calls.exists())
        self.assertFalse(any(item['operation'] == 'private_trace_preflight'
                             for item in current.record['operations']))
        current.release()

    def test_private_device_blocks_when_idle_service_reset_is_rejected(self):
        (self.client.bin / 'hitrace').write_text(
            '#!/bin/sh\necho "error: service rejected the request"\nexit 0\n')
        (self.client.bin / 'hitrace').chmod(0o755)
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        with self.assertRaises(CapturePreparationError):
            current.acquire()

        operation = next(item for item in current.record['operations']
                         if item['operation'] == 'private_trace_preflight')
        self.assertEqual((operation['status'], operation['reason']),
                         ('pending', 'service_reset_unverified'))
        self.assertFalse(self.lock.exists())

    def test_private_device_idle_reset_does_not_treat_benign_failure_summary_as_error(self):
        (self.client.bin / 'hitrace').write_text(
            '#!/bin/sh\necho "completed with 0 failures"\nexit 0\n')
        (self.client.bin / 'hitrace').chmod(0o755)
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        current.acquire()

        operation = current.record['operations'][-1]
        self.assertEqual(operation['operation'], 'private_trace_preflight')
        self.assertEqual(operation['status'], 'recovered')
        current.release()

    def test_private_device_idle_reset_requires_selected_trace_root_to_remain_readable(self):
        (self.client.bin / 'hitrace').write_text(
            '#!/bin/sh\nrm -f "$TEST_TRACE/tracing_on"\nexit 0\n')
        (self.client.bin / 'hitrace').chmod(0o755)
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        with self.assertRaises(CapturePreparationError):
            current.acquire()

        operation = next(item for item in current.record['operations']
                         if item['operation'] == 'private_trace_preflight')
        self.assertEqual((operation['status'], operation['reason']),
                         ('pending', 'service_reset_trace_active'))
        self.assertFalse(self.lock.exists())

    def test_private_device_active_finish_requires_every_readable_trace_root_idle(self):
        alias = self.root / 'tracing_alias'; alias.mkdir()
        (alias / 'tracing_on').write_text('1\n'); (alias / 'trace').write_text('alias trace\n')
        # The first root can be stale-idle while another readable alias reports
        # the active trace.  Preflight must choose the data-preserving finish
        # path from the aggregate state rather than issuing finish_nodump.
        (self.client.trace / 'tracing_on').write_text('0\n')
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace), str(alias)), private_device=True)

        with self.assertRaises(CapturePreparationError):
            current.acquire()

        operation = next(item for item in current.record['operations']
                         if item['operation'] == 'private_trace_preflight')
        self.assertEqual((operation['status'], operation['reason']), ('pending', 'finish_unverified'))
        self.assertTrue(self.client.hitrace_calls.read_text().startswith('--trace_finish -o '))
        self.assertEqual((alias / 'tracing_on').read_text().strip(), '1')
        self.assertFalse(self.lock.exists())

    def test_private_device_stubborn_idle_reset_is_killed_reaped_and_unregistered(self):
        # macOS has no Linux /proc. Override only the fixture's identity reader
        # with ps while retaining exact PID/start-token checks and the complete
        # production timeout/TERM/KILL/reap shell below it.
        host_identity_helpers = r'''
collector_ticks() {
  state=$(/bin/ps -o stat= -p "$1" 2>/dev/null) || return 1
  [ -n "$state" ] || return 1
  printf 'fixture-%s\n' "$1"
}
collector_alive() {
  [ "$3" = "$device_boot" ] && [ "$2" = "fixture-$1" ] || return 1
  state=$(/bin/ps -o stat= -p "$1" 2>/dev/null) || return 1
  case "$state" in *Z*) return 1;; esac
  kill -0 "$1" 2>/dev/null
}
'''
        shell = _PRIVATE_TRACE_PREFLIGHT_SHELL.replace(
            "trace_result() {", host_identity_helpers + "\ntrace_result() {", 1)
        (self.client.bin / 'hitrace').write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$$" > "$TEST_TRACE/stubborn.pid"\n'
            "trap '' TERM\nwhile :; do /bin/sleep 0.1; done\n")
        (self.client.bin / 'hitrace').chmod(0o755)
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        with patch('tools.scenario_capture.collector_cleanup._PRIVATE_TRACE_PREFLIGHT_SHELL', shell):
            with self.assertRaises(CapturePreparationError):
                current.acquire()

        operation = next(item for item in current.record['operations']
                         if item['operation'] == 'private_trace_preflight')
        self.assertEqual((operation['status'], operation['reason']),
                         ('pending', 'service_reset_timeout'))
        pid = int((self.client.trace / 'stubborn.pid').read_text())
        with self.assertRaises(OSError):
            os.kill(pid, 0)
        ledger = self.base / 'capture_private' / 'private_trace_preflight' / 'collector_processes' / 'trace_finisher'
        self.assertFalse(ledger.exists())
        self.assertFalse(self.lock.exists())

    def test_private_device_falls_back_after_strict_claim_marker_check_fails(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'; step.mkdir(parents=True)
        script = [f'out={shlex.quote(str(step))}',
                  *step_shell_prelude(old.run_id, str(step), old.token, lock_path=str(self.lock)),
                  f'mark_trace_intent {shlex.quote(str(self.client.trace))}',
                  f'mark_trace_owned {shlex.quote(str(self.client.trace))}']
        self.client.shell('\n'.join(script))
        (self.client.trace / 'tracing_on').write_text('1\n')
        (self.client.trace / 'trace').write_text('marker was overwritten from the ring buffer\n')
        shutil.rmtree(self.lock)

        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)
        current.acquire()

        reconciled = next(item for item in current.record['operations'] if item['operation'] == 'trace_reconcile')
        fallback = next(item for item in current.record['operations'] if item['operation'] == 'private_trace_preflight')
        self.assertEqual((reconciled['status'], reconciled['reason']), ('protected', 'ownership_marker_missing'))
        self.assertEqual(fallback['status'], 'recovered')
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '0')
        self.assertFalse(Path(str(self.lock) + '.trace_owner').exists())
        current.release()

    def test_private_terminal_takeover_defers_old_trace_to_private_preflight(self):
        old = self.session(); old.acquire()
        step = self.base / old.run_id / 'step_01'; step.mkdir(parents=True)
        script = [f'out={shlex.quote(str(step))}',
                  *step_shell_prelude(old.run_id, str(step), old.token, lock_path=str(self.lock)),
                  f'mark_trace_intent {shlex.quote(str(self.client.trace))}',
                  f'mark_trace_owned {shlex.quote(str(self.client.trace))}']
        self.client.shell('\n'.join(script))
        (self.client.trace / 'tracing_on').write_text('1\n')
        # The finite trace ring may no longer contain the marker even though
        # the persisted owner records still belong to the interrupted run.
        (self.client.trace / 'trace').write_text('marker overwritten before Desktop restart\n')

        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), terminal_run_ids=(old.run_id,),
            trace_roots=(str(self.client.trace),), private_device=True)
        current.acquire()

        acquire = next(item for item in current.record['operations'] if item['operation'] == 'acquire')
        self.assertIn('HETERO_COLLECTOR_EVENT|deferred|trace|private_preflight_ownership_marker_missing',
                      acquire['output'])
        fallback = next(item for item in current.record['operations']
                        if item['operation'] == 'private_trace_preflight')
        self.assertEqual(fallback['status'], 'recovered')
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '0')
        self.assertFalse(Path(str(self.lock) + '.trace_owner').exists())
        current.release()

    def test_terminal_stubborn_sampler_and_trace_finisher_are_force_stopped(self):
        old = self.session(); old.acquire()
        sampler = self.client.spawn('/data/local/tmp/pmu-sampler', stubborn=True)
        finisher = self.client.spawn('hitrace --trace_finish', stubborn=True)
        self.ledger(old, 'sampler', sampler)
        self.ledger(old, 'trace_finisher', finisher)

        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), terminal_run_ids=(old.run_id,),
            trace_roots=(str(self.client.trace),), private_device=True)
        current.acquire()

        self.assertIsNotNone(sampler.poll())
        self.assertIsNotNone(finisher.poll())
        acquire = next(item for item in current.record['operations'] if item['operation'] == 'acquire')
        self.assertIn('HETERO_COLLECTOR_EVENT|kill|sampler', acquire['output'])
        self.assertIn('HETERO_COLLECTOR_EVENT|kill|trace_finisher', acquire['output'])
        current.release()

    def test_private_terminal_recovery_retries_only_incomplete_cleanup_once(self):
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), terminal_run_ids=('capture_old',),
            trace_roots=(str(self.client.trace),), private_device=True)
        outcomes = iter(('pending', 'acquired'))

        def run(operation, **_kwargs):
            status = next(outcomes)
            current.record.update(status=status,
                reason='collector_recovery_incomplete' if status == 'pending' else '')
            return status

        with patch.object(current, '_peek_terminal_owner',
                          side_effect=lambda: setattr(current, 'selected_terminal_run_ids', ('capture_old',))), \
                patch.object(current, '_run', side_effect=run) as invoke, \
                patch.object(current, '_private_trace_preflight', return_value=('clear', 'already_stopped')), \
                patch('tools.scenario_capture.collector_cleanup.time.sleep') as sleep:
            current.acquire()

        self.assertEqual(invoke.call_count, 2)
        sleep.assert_called_once_with(0.15)
        self.assertEqual(current.record['recovery_retry_count'], 1)

    def test_regular_busy_acquire_does_not_retry(self):
        owner = self.session(); owner.acquire()
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), terminal_run_ids=(owner.run_id,),
            trace_roots=(str(self.client.trace),), private_device=True)
        # Do not authorize the current owner as terminal in the inspected set.
        current.terminal_run_ids = frozenset()
        with patch.object(current, '_run', wraps=current._run) as invoke:
            with self.assertRaises(CapturePreparationError): current.acquire()
        self.assertEqual(invoke.call_count, 1)
        owner.release()

    def test_private_device_preflight_still_blocks_when_hitrace_cannot_stop_trace(self):
        (self.client.trace / 'tracing_on').write_text('1\n')
        (self.client.bin / 'hitrace').write_text('#!/bin/sh\nexit 1\n')
        (self.client.bin / 'hitrace').chmod(0o755)
        current = CollectorSession(self.client, 'capture_private', str(self.base / 'capture_private'), {},
            lock_path=str(self.lock), trace_roots=(str(self.client.trace),), private_device=True)

        with self.assertRaises(CapturePreparationError):
            current.acquire()

        operation = next(item for item in current.record['operations'] if item['operation'] == 'private_trace_preflight')
        self.assertEqual(operation['status'], 'pending')
        self.assertEqual(operation['reason'], 'finish_unverified')
        self.assertEqual((self.client.trace / 'tracing_on').read_text().strip(), '1')
        self.assertFalse(self.lock.exists())

    def test_cancelled_cleanup_bypasses_cancel_guard_and_wrong_token_cannot_release(self):
        owner = self.session(); owner.acquire(); other = self.session('capture_two')
        other.release(); self.assertEqual(other.record['status'], 'unowned'); self.assertTrue(self.lock.exists())
        self.client.cancelled = True
        with self.assertRaises(CaptureCancelled): owner.refresh()
        owner.release(); self.assertEqual(owner.record['status'], 'released')
        self.assertTrue(self.client.calls[-1][2])

    def test_long_rpc_extends_lease_and_proxy_keeps_device_attributes(self):
        owner = self.session(); owner.acquire()
        owner.lease_client.collect('/remote', self.root / 'local', timeout=600)
        self.assertEqual(int((self.lock / 'lease_until').read_text()), 1663)
        self.assertIs(owner.lease_client.proc, self.client.proc)
        owner.release()

    def test_short_rpc_burst_reuses_lease_and_records_cache_hits(self):
        owner = self.session(); owner.acquire()
        for _ in range(4):
            owner.lease_client.shell('printf ready', timeout=8)
        self.assertEqual(owner.record['lease_cache_hits'], 4)
        self.assertEqual(owner.record['lease_remote_refreshes'], 0)
        self.assertEqual([item['operation'] for item in owner.record['operations']], ['acquire'])
        owner.release()

    def test_refresh_threshold_covers_operation_bridge_grace_and_margin(self):
        now = [100.0]
        self.client.operation_timeout_grace_seconds = 20
        with patch('tools.scenario_capture.collector_cleanup.time.monotonic', side_effect=lambda: now[0]):
            owner = self.session(); owner.acquire()
            self.assertEqual(owner.host_lease_until, 189)
            # 8 s operation + 20 s return grace + 15 s margin needs 43 s.
            now[0] = 146
            owner.refresh(8)
            self.assertEqual(owner.record['lease_cache_hits'], 1)
            now[0] = 147
            owner.refresh(8)
            self.assertEqual(owner.record['lease_remote_refreshes'], 1)
            self.assertEqual(owner.record['lease_required_seconds'], 43)
            self.assertEqual(owner.host_lease_until, 236)
            owner.release()

    def test_host_deadline_starts_before_rpc_not_after_response(self):
        now = [100.0]
        shell = self.client.shell

        def slow_shell(*args, **kwargs):
            result = shell(*args, **kwargs)
            now[0] += 20
            return result

        with patch('tools.scenario_capture.collector_cleanup.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.client, 'shell', side_effect=slow_shell):
            owner = self.session(); owner.acquire()
            self.assertEqual(now[0], 120)
            self.assertEqual(owner.host_lease_until, 189)
            owner.release()

    def test_forced_short_renewal_and_recovery_never_shorten_long_device_lease(self):
        owner = self.session(); owner.acquire(); owner.refresh(600)
        original = int((self.lock / 'lease_until').read_text())
        owner.refresh(8, force=True)
        self.assertGreaterEqual(int((self.lock / 'lease_until').read_text()), original)
        # Even after losing the host cache, recovery preserves the longer
        # lease already recorded by the device rather than overwriting it.
        owner.invalidate_lease_cache(); owner.recover()
        self.assertGreaterEqual(int((self.lock / 'lease_until').read_text()), original)
        self.assertEqual(owner.record['lease_remote_refreshes'], 2)
        owner.release()

    def test_failed_remote_refresh_clears_cache_and_next_operation_rechecks(self):
        owner = self.session(); owner.acquire()
        with patch.object(self.client, 'shell', side_effect=RuntimeError('transport interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'transport interrupted'):
                owner.refresh(8, force=True)
        self.assertEqual(owner.host_lease_until, 0)
        owner.lease_client.shell('printf recovered', timeout=8)
        self.assertEqual(owner.record['lease_remote_refreshes'], 2)
        self.assertEqual(owner.record['lease_cache_hits'], 0)
        owner.release()

    def test_failed_operation_discards_cache_before_next_rpc(self):
        owner = self.session(); owner.acquire()
        with self.assertRaises(RuntimeError):
            owner.lease_client.shell('exit 7', timeout=8)
        self.assertEqual(owner.host_lease_until, 0)
        self.assertEqual(owner.record['lease_cache_invalidations'], 1)
        owner.lease_client.shell('printf recovered', timeout=8)
        self.assertEqual(owner.record['lease_remote_refreshes'], 1)
        owner.release()

    def test_forced_refresh_detects_lost_ownership_before_cached_lease_expires(self):
        owner = self.session(); owner.acquire()
        (self.lock / 'token').write_text('foreign-owner\n')
        with self.assertRaises(CapturePreparationError): owner.refresh(8, force=True)
        self.assertEqual(owner.host_lease_until, 0)
        self.assertEqual(owner.record['reason'], 'session_ownership_lost')
        # Restore only the fixture so the owning test can release its lock.
        (self.lock / 'token').write_text(owner.token + '\n')
        owner.release()

    def test_slow_renewal_does_not_allow_operation_beyond_remaining_lease(self):
        now = [100.0]
        shell = self.client.shell

        def slow_shell(*args, **kwargs):
            result = shell(*args, **kwargs)
            if '# HETERO_COLLECTOR_SESSION refresh' in args[0]: now[0] += 70
            return result

        with patch('tools.scenario_capture.collector_cleanup.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.client, 'shell', side_effect=slow_shell):
            owner = self.session(); owner.acquire()
            with self.assertRaises(CapturePreparationError): owner.refresh(8, force=True)
            self.assertEqual(owner.host_lease_until, 0)
            owner.release()

    def test_stale_recovery_claim_is_recoverable_and_current_claim_is_protected(self):
        owner = self.session(); owner.acquire(); self.expire()
        claim = self.lock / 'recovery'; claim.mkdir()
        (claim / 'claim.dead').write_text('999999 1 fixture-boot\n')
        next_owner = self.session('capture_two'); next_owner.acquire()
        self.assertFalse(claim.exists()); next_owner.release()

    def test_terminal_legacy_logger_is_bound_to_platform_output_not_process_name(self):
        self.lock.mkdir(); (self.lock / 'owner').write_text('capture_old')
        step = self.base / 'capture_old' / 'step_01'; step.mkdir(parents=True)
        (step / 'capture.sh').write_text(f'out={step}\ncommand_action() {{ :; }}\n')
        log = step / 'hilog.log'; log.write_text('retain old log')
        owned = self.client.spawn('/system/bin/hilog -P 10')
        daemon = self.client.spawn('/system/bin/hilogd')
        for child in (owned, daemon):
            fd = self.client.proc / str(child.pid) / 'fd'; fd.mkdir(); (fd / '1').symlink_to(log)
        new = self.session('capture_new', terminal=('capture_old',)); new.acquire()
        self.assertIsNotNone(owned.poll()); self.assertIsNone(daemon.poll())
        self.assertEqual(log.read_text(), 'retain old log'); new.release()

    def test_legacy_owner_requires_exact_script_argument_not_suffix_or_flag_value(self):
        for argument in ('capture.sh.bak', 'capture.sh --note=CAPTURE_PATH'):
            with self.subTest(argument=argument):
                self.lock.mkdir(); (self.lock / 'owner').write_text('capture_old')
                step = self.base / 'capture_old' / 'step_01'; step.mkdir(parents=True, exist_ok=True)
                # The second case only embeds the expected path in a flag,
                # rather than passing the script as a complete argv element.
                command = f'sh {step}/{argument}' if 'CAPTURE_PATH' not in argument else f'other --note={step}/capture.sh'
                unrelated = self.client.spawn(command)
                (step / 'owner.pid').write_text(str(unrelated.pid))
                new = self.session('capture_new', terminal=('capture_old',)); new.acquire()
                self.assertIsNone(unrelated.poll())
                new.release()

    def test_legacy_sampler_requires_both_complete_output_path_arguments(self):
        self.lock.mkdir(); (self.lock / 'owner').write_text('capture_old')
        step = self.base / 'capture_old' / 'step_01'; step.mkdir(parents=True)
        (step / 'capture.sh').write_text(f'out={step}\ncommand_action() {{ :; }}\n')
        log = step / 'pmu.log'; log.write_text('retained')
        owned = self.client.spawn(f'/data/local/tmp/sampler {step}/pmu.csv {step}/pmu.json')
        suffix = self.client.spawn(f'/data/local/tmp/sampler {step}/pmu.csv.bak {step}/pmu.json')
        flag = self.client.spawn(f'/data/local/tmp/sampler --output={step}/pmu.csv {step}/pmu.json')
        for child in (owned, suffix, flag):
            fd = self.client.proc / str(child.pid) / 'fd'; fd.mkdir(); (fd / '1').symlink_to(log)
        new = self.session('capture_new', terminal=('capture_old',)); new.acquire()
        self.assertIsNotNone(owned.poll())
        self.assertIsNone(suffix.poll()); self.assertIsNone(flag.poll())
        new.release()

    def test_missing_identity_decoder_keeps_existing_session_and_process(self):
        owner = self.session(); owner.acquire()
        child = self.client.spawn('hilog -P 10'); self.ledger(owner, 'logger', child)
        (self.client.bin / 'od').unlink()
        owner.release()
        self.assertEqual(owner.record['status'], 'pending')
        self.assertEqual(owner.record['reason'], 'process_identity_tools_unavailable')
        self.assertTrue(self.lock.exists()); self.assertIsNone(child.poll())

    def test_legacy_cancel_without_tr_or_awk_rechecks_identity_and_cleans_owned_shell(self):
        step = self.base / 'capture_old' / 'step_01'; step.mkdir(parents=True)
        child = self.client.spawn(f'/bin/sh {step}/capture.sh')
        owner_file = step / 'owner.pid'; owner_file.write_text(str(child.pid))
        def finish_owner():
            child.wait(); owner_file.unlink(missing_ok=True)
        waiter = threading.Thread(target=finish_owner, daemon=True); waiter.start()
        self.client.cancelled = True
        cancel_owned_capture(self.client, str(step))
        waiter.join(timeout=1)
        self.assertIsNotNone(child.poll()); self.assertFalse(owner_file.exists())
        self.assertTrue(self.client.calls[-1][2])

    def test_legacy_cancel_does_not_signal_process_with_similar_script_name(self):
        step = self.base / 'capture_old' / 'step_01'; step.mkdir(parents=True)
        unrelated = self.client.spawn(f'/bin/sh {step}/capture.sh.bak')
        (step / 'owner.pid').write_text(str(unrelated.pid))
        with self.assertRaises(RuntimeError): cancel_owned_capture(self.client, str(step))
        self.assertIsNone(unrelated.poll())

    def test_stubborn_owned_shell_is_escalated_after_term_with_identity_recheck(self):
        owner = self.session(); owner.acquire()
        step = self.base / owner.run_id / 'step_01'
        child = self.client.spawn(f'sh {step}/capture.sh', stubborn=True)
        self.ledger(owner, 'owner', child)
        owner.release()
        self.assertEqual(owner.record['status'], 'released', owner.record)
        self.assertIsNotNone(child.poll())
        self.assertIn('HETERO_COLLECTOR_EVENT|kill|owner', owner.record['operations'][-1]['output'])

    def test_live_recovery_claim_blocks_second_session(self):
        owner = self.session(); owner.acquire(); self.expire()
        claim = self.lock / 'recovery'; claim.mkdir()
        worker = self.client.spawn('platform recovery')
        (claim / 'claim.active').write_text(f'{worker.pid} 1234 fixture-boot\n')
        next_owner = self.session('capture_two')
        with self.assertRaises(CapturePreparationError): next_owner.acquire()
        self.assertEqual(next_owner.record['reason'], 'recovery_active')
        self.assertEqual((self.lock / 'token').read_text().strip(), owner.token)

    def test_terminal_history_is_filtered_to_one_owner_before_shell_transfer(self):
        self.lock.mkdir(); (self.lock / 'owner').write_text('capture_2499')
        new = self.session('capture_new', terminal=(f'capture_{i}' for i in range(2500)))
        new.acquire()
        commands = [command for command, _, _ in self.client.calls]
        self.assertTrue(commands[0].startswith('# HETERO_COLLECTOR_SESSION inspect'))
        self.assertLess(max(map(len, commands)), 16384)
        self.assertIn("terminal_runs='|capture_2499|'", commands[-1])
        new.release()

    def test_acquire_and_release_fit_wrapped_hdc_payload_budget(self):
        run_id = 'capture_' + 'f' * 24
        old_run_id = 'capture_' + 'e' * 24
        current = CollectorSession(self.client, run_id,
            f'/data/local/tmp/hetero_scenarios/{run_id}', {},
            terminal_run_ids=(old_run_id,), private_device=True)
        current.selected_terminal_run_ids = (old_run_id,)
        marker = '__SCENARIO_RC_' + 'a' * 24

        for operation in ('acquire', 'release'):
            with self.subTest(operation=operation):
                command = current._command(operation, 999999)
                wrapped = f"( {command}\n); scenario_rc=$?; printf '\\n{marker}%s\\n' \"$scenario_rc\""
                self.assertLessEqual(len(command.encode('utf-8')), 15 * 1024)
                self.assertLessEqual(len(wrapped.encode('utf-8')), 15 * 1024)

    def test_prelude_records_owner_identity_and_trace_intent(self):
        owner = self.session(); owner.acquire()
        step = self.base / owner.run_id / 'step_01'; step.mkdir()
        script = [f'out={shlex.quote(str(step))}', *step_shell_prelude(owner.run_id, str(step), owner.token, lock_path=str(self.lock)),
                  f'mark_trace_intent {shlex.quote(str(self.client.trace))}']
        self.client.shell('\n'.join(script))
        self.assertIn(owner.token, (step / 'collector_processes/owner').read_text())
        self.assertTrue((step / 'trace.owner').read_text().endswith(' intent\n'))
        owner.release()

    def test_cached_lease_still_rejects_replaced_token_or_boot_at_script_entry(self):
        for field, replacement in (('token', 'another-owner'), ('boot_id', 'previous-boot')):
            with self.subTest(field=field):
                owner = self.session(); owner.acquire()
                step = self.base / owner.run_id / field; step.mkdir()
                marker = step / 'action-ran'
                script = [f'out={shlex.quote(str(step))}',
                    *step_shell_prelude(owner.run_id, str(step), owner.token, lock_path=str(self.lock)),
                    f'printf executed > {shlex.quote(str(marker))}']
                original = (self.lock / field).read_text()
                (self.lock / field).write_text(replacement + '\n')
                with self.assertRaisesRegex(RuntimeError, '75'):
                    owner.lease_client.shell('\n'.join(script), timeout=8)
                self.assertEqual(owner.record['lease_cache_hits'], 1)
                self.assertEqual(owner.record['lease_remote_refreshes'], 0)
                self.assertFalse(marker.exists())
                self.assertFalse((step / 'collector_processes').exists())
                self.assertEqual(owner.host_lease_until, 0)
                (self.lock / field).write_text(original)
                owner.release()


if __name__ == '__main__': unittest.main()
