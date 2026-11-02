"""Run production sampler C against simulated perf syscalls; never access PMU."""
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.scenario_capture.runner import validate_pmu


PORTABILITY = r'''
#include <stdarg.h>
#define SYS_perf_event_open 999
#define PERF_TYPE_HARDWARE 0
#define PERF_TYPE_HW_CACHE 3
#define PERF_COUNT_HW_CPU_CYCLES 0
#define PERF_COUNT_HW_INSTRUCTIONS 1
#define PERF_COUNT_HW_CACHE_REFERENCES 2
#define PERF_COUNT_HW_CACHE_MISSES 3
#define PERF_COUNT_HW_BRANCH_INSTRUCTIONS 4
#define PERF_COUNT_HW_BRANCH_MISSES 5
#define PERF_COUNT_HW_STALLED_CYCLES_FRONTEND 7
#define PERF_COUNT_HW_STALLED_CYCLES_BACKEND 8
#define PERF_COUNT_HW_CACHE_L1D 0
#define PERF_COUNT_HW_CACHE_LL 2
#define PERF_COUNT_HW_CACHE_DTLB 3
#define PERF_COUNT_HW_CACHE_RESULT_MISS 1
#define PERF_FORMAT_GROUP 8
#define PERF_FORMAT_TOTAL_TIME_ENABLED 1
#define PERF_FORMAT_TOTAL_TIME_RUNNING 2
#define PERF_EVENT_IOC_RESET 0
#define PERF_EVENT_IOC_ENABLE 1
#define PERF_EVENT_IOC_DISABLE 2
#define PERF_IOC_FLAG_GROUP 1
#define CPU_SETSIZE 1024
#define SCHED_FIFO 1
#ifndef TIMER_ABSTIME
#define TIMER_ABSTIME 1
#endif
typedef struct {int ignored;} cpu_set_t;
struct sched_param {int sched_priority;};
struct perf_event_attr { uint32_t size, type; uint64_t config, read_format; int disabled, exclude_kernel, exclude_hv; };
#define CPU_ZERO(mask) ((void)(mask))
#define CPU_SET(cpu, mask) ((void)(cpu), (void)(mask))
static int fake_affinity(int pid, size_t size, void *mask) {(void)pid;(void)size;(void)mask;return 0;}
static int fake_min_priority(int policy) {(void)policy;return 1;}
static int fake_scheduler(int pid, int policy, void *parameter) {(void)pid;(void)policy;(void)parameter;return 0;}
static int fake_getscheduler(int pid) {(void)pid;return 0;}
static int fake_getcpu(void) {return 0;}
#define sched_setaffinity fake_affinity
#define sched_get_priority_min fake_min_priority
#define sched_setscheduler fake_scheduler
#define sched_getscheduler fake_getscheduler
#define sched_getcpu fake_getcpu
static uint64_t fake_ns = 1000000000ULL;
static int open_calls, descriptors_used;
static struct {int leader, group;uint64_t config;uint32_t type;} descriptors[64];
static long fake_syscall(long number, ...) {
    (void)number;
    va_list args; va_start(args, number);
    struct perf_event_attr *attributes = va_arg(args, struct perf_event_attr *);
    (void)va_arg(args, int); (void)va_arg(args, int);
    int leader = va_arg(args, int); (void)va_arg(args, int); va_end(args);
    int group = open_calls < 2 ? 0 : 1 + (open_calls - 2) / 4;
    open_calls++;
    const char *failure = getenv("PMU_FAKE_FAIL");
    if (failure && (!strcmp(failure, "all") || (!strcmp(failure, "branch_misses") &&
            attributes->type == PERF_TYPE_HARDWARE && attributes->config == PERF_COUNT_HW_BRANCH_MISSES))) {
        errno = !strcmp(failure, "all") ? EPERM : EINVAL; return -1;
    }
    int descriptor = ++descriptors_used;
    descriptors[descriptor].leader = leader == -1 ? descriptor : leader;
    descriptors[descriptor].group = group;
    descriptors[descriptor].config = attributes->config;
    descriptors[descriptor].type = attributes->type;
    return descriptor;
}
static int fake_ioctl(int descriptor, unsigned long request, ...) {(void)descriptor;(void)request;return 0;}
static int fake_close(int descriptor) {(void)descriptor;return 0;}
static int fake_open(const char *path, int flags, ...) {(void)path;(void)flags;errno=ENOENT;return -1;}
static ssize_t fake_read(int descriptor, void *buffer, size_t size) {
    uint64_t *values = buffer;
    int count = 0;
    uint64_t elapsed = fake_ns - 1000000000ULL;
    uint64_t denominator = getenv("PMU_FAKE_MUX") ? 4 : 1;
    uint64_t running = elapsed / denominator;
    if (getenv("PMU_FAKE_NOT_RUNNING") && descriptors[descriptor].group != 0) running = 0;
    for (int item = 1; item <= descriptors_used; item++) {
        if (descriptors[item].leader == descriptor) values[3 + count++] = running * (uint64_t)(item % 3 + 1);
    }
    values[0] = (uint64_t)count; values[1] = elapsed; values[2] = running;
    if (size != (size_t)(3 + count) * sizeof(uint64_t)) {errno=EIO;return -1;}
    return (ssize_t)size;
}
static int fake_clock_gettime(clockid_t id, struct timespec *stamp) {
    (void)id;fake_ns += 1000;
    stamp->tv_sec=(time_t)(fake_ns/1000000000ULL);stamp->tv_nsec=(long)(fake_ns%1000000000ULL);return 0;
}
static int fake_sleep(clockid_t id, int flags, const struct timespec *target, struct timespec *remain) {
    (void)id;(void)flags;(void)remain;
    fake_ns=(uint64_t)target->tv_sec*1000000000ULL+(uint64_t)target->tv_nsec;
    if (getenv("PMU_FAKE_STOP")) raise(SIGTERM);
    return 0;
}
#define syscall fake_syscall
#define ioctl fake_ioctl
#define close fake_close
#define open fake_open
#define read fake_read
#define clock_gettime fake_clock_gettime
#define clock_nanosleep fake_sleep
'''


class MultiSamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("Host C compiler required for syscall simulation")
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "pmu_test_portability.h").write_text(PORTABILITY)
        cls.binary = cls.root / "sampler"
        source = Path(__file__).parents[2] / "tools/scenario_capture/thread_pmu_sampler.c"
        process = subprocess.run([compiler, "-DPMU_TEST_HARNESS", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(cls.root),
                                  str(source), "-o", str(cls.binary)], capture_output=True, text=True, timeout=30)
        if process.returncode:
            raise AssertionError(process.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def capture(self, mode="auto", period=100, duration=3, **environment):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        result = subprocess.run([str(self.binary), "123", str(period), str(duration), "0", mode,
                                 str(folder / "pmu.csv"), str(folder / "pmu.json"), str(folder / "pmu.ready"), "normal"],
                                env={**os.environ, **environment}, capture_output=True, text=True, timeout=5)
        (folder / "pmu_exit_code.txt").write_text(str(result.returncode))
        metadata = json.loads((folder / "pmu.json").read_text())
        with (folder / "pmu.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        return folder, result, metadata, rows

    def test_auto_reads_all_groups_with_local_denominators_and_keeps_legacy_mode(self):
        folder, process, metadata, rows = self.capture()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(metadata["sampler_version"], "1.2.0")
        self.assertEqual(len(metadata["groups"]), 7)
        self.assertEqual(metadata["sample_count"], len(rows))
        self.assertEqual(metadata["tick_count"] * 7, len(rows))
        for group in metadata["groups"]:
            self.assertEqual(group["event_names"][:2], ["cycles", "instructions"])
            self.assertEqual(len(group["event_names"]), 2 if group["id"] == "base" else 4)
        result = validate_pmu(folder, target_tid=123)
        self.assertTrue(result["valid"])
        self.assertTrue(result["quality_complete"])
        self.assertEqual(len(result["available_event_names"]), 14)
        _, legacy, metadata, rows = self.capture(mode="branch")
        self.assertEqual(legacy.returncode, 0)
        self.assertNotIn("groups", metadata)
        self.assertEqual(metadata["event_names"], ["cycles", "instructions", "branch_instructions", "branch_misses"])
        self.assertIn("cycles", rows[0])
        self.assertNotIn("group_id", rows[0])

    def test_one_unsupported_event_preserves_other_events_and_diagnostic(self):
        folder, process, metadata, _ = self.capture(PMU_FAKE_FAIL="branch_misses")
        self.assertEqual(process.returncode, 0)
        branch = next(group for group in metadata["groups"] if group["id"] == "branch")
        self.assertEqual(branch["event_names"], ["cycles", "instructions", "branch_instructions"])
        self.assertEqual(branch["status"], "partial")
        self.assertEqual(branch["unavailable_events"][0]["name"], "branch_misses")
        result = validate_pmu(folder, target_tid=123)
        self.assertTrue(result["valid"])
        self.assertFalse(result["quality_complete"])
        self.assertEqual(result["missing_event_names"], ["branch_misses"])
        self.assertIn("llc_read_misses", result["available_event_names"])

    def test_all_permission_failures_leave_metadata_and_never_claim_readiness(self):
        folder, process, metadata, rows = self.capture(PMU_FAKE_FAIL="all")
        self.assertEqual(process.returncode, 3)
        self.assertEqual(rows, [])
        self.assertFalse((folder / "pmu.ready").exists())
        self.assertTrue(all(group["unavailable_events"] for group in metadata["groups"]))
        self.assertFalse(validate_pmu(folder, target_tid=123)["valid"])

    def test_multiplexing_retains_usable_estimates_and_per_group_coverage(self):
        folder, _, _, _ = self.capture(PMU_FAKE_MUX="1")
        result = validate_pmu(folder, target_tid=123)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["groups"]), 7)
        self.assertTrue(all(group["status"] == "estimated" for group in result["groups"]))
        self.assertTrue(all(abs(group["running_ratio"] - .25) < .001 for group in result["groups"]))
        self.assertIn("PMU_GROUP_LOW_COVERAGE", {item["code"] for item in result["diagnostics"]})

    def test_unscheduled_topic_groups_keep_base_and_do_not_claim_supported_data(self):
        folder, _, _, _ = self.capture(PMU_FAKE_NOT_RUNNING="1")
        result = validate_pmu(folder, target_tid=123)
        self.assertTrue(result["valid"])
        self.assertFalse(result["quality_complete"])
        self.assertEqual(result["available_event_names"], ["cycles", "instructions"])
        self.assertTrue(all(group["status"] == "not_running" for group in result["groups"] if group["id"] != "base"))

    def test_counter_growth_without_additional_runtime_is_not_usable(self):
        folder, _, metadata, rows = self.capture()
        base = next(group for group in metadata["groups"] if group["id"] == "base")
        rows = [row for row in rows if row["group_id"] == "base"][:3]
        for index, row in enumerate(rows):
            row.update(enabled_ns=0 if index == 0 else 100, running_ns=0 if index == 0 else 100,
                       event0=index * 100, event1=index * 100)
        with (folder / "pmu.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader(); writer.writerows(rows)
        base["sample_count"] = 3
        metadata.update(groups=[base], sample_count=3, tick_count=3)
        (folder / "pmu.json").write_text(json.dumps(metadata))
        result = validate_pmu(folder, target_tid=123)
        self.assertFalse(result["valid"])
        self.assertFalse(result["quality_complete"])
        self.assertEqual(result["groups"][0]["invalid_counter_rows"], 1)
        self.assertIn("PMU_GROUP_INVALID", {item["code"] for item in result["diagnostics"]})

    def test_step_coverage_requires_snapshots_on_both_sides_of_read_brackets(self):
        folder, _, _, rows = self.capture()
        base = [row for row in rows if row["group_id"] == "base"]
        result = validate_pmu(folder, target_tid=123, step_window={
            "start_s": int(base[0]["read_begin_ns"]) / 1e9,
            "end_s": int(base[-1]["read_end_ns"]) / 1e9})
        group = next(group for group in result["groups"] if group["id"] == "base")
        self.assertTrue(group["valid"])
        self.assertEqual(group["coverage"]["status"], "partial")
        self.assertFalse(group["quality_complete"])
        self.assertEqual(group["coverage"]["first_read_end_ns"], int(base[0]["read_end_ns"]))

    def test_high_resolution_long_window_reports_buffer_limit_without_filling_large_output(self):
        folder, process, metadata, _ = self.capture(period=10, duration=10000, PMU_FAKE_STOP="1")
        self.assertEqual(process.returncode, 5)
        self.assertTrue(metadata["buffer_limited"])
        self.assertGreater(metadata["period_ns_effective"], metadata["period_ns_requested"])
        self.assertEqual(metadata["maximum_buffer_bytes"], 128 * 1024 * 1024)
        result = validate_pmu(folder, target_tid=123)
        self.assertIn("PMU_MULTI_BUFFER_LIMIT", {item["code"] for item in result["diagnostics"]})


if __name__ == "__main__":
    unittest.main()
