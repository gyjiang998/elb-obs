#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#ifndef PMU_TEST_HARNESS
#include <linux/perf_event.h>
#include <sched.h>
#endif
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#ifdef PMU_TEST_HARNESS
#include "pmu_test_portability.h"
#endif

#define EVENT_COUNT 4
#define SAMPLER_VERSION "1.2.0"
#define MIN_PERIOD_US 10ULL
#define MAX_PERIOD_US 10000ULL
#define MAX_DURATION_MS 10000ULL
#define MULTI_GROUP_COUNT 7
#define MULTI_BUFFER_BYTES (128ULL * 1024ULL * 1024ULL)

static int parse_decimal(const char *text, uint64_t minimum, uint64_t maximum, uint64_t *result)
{
    if (text == NULL || *text == '\0') return 0;
    for (const char *cursor = text; *cursor; cursor++) {
        if (*cursor < '0' || *cursor > '9') return 0;
    }
    errno = 0;
    char *end = NULL;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno == ERANGE || end == text || *end || value < minimum || value > maximum) return 0;
    *result = (uint64_t)value;
    return 1;
}

typedef struct {
    uint64_t count;
    uint64_t enabled_ns;
    uint64_t running_ns;
    uint64_t values[EVENT_COUNT];
} CounterRead;

typedef struct {
    uint64_t read_begin_ns;
    uint64_t read_end_ns;
    CounterRead counters;
    int read_error;
    int reader_cpu;
} Sample;

static volatile sig_atomic_t stopped = 0;

static void stop_sampling(int signal_number)
{
    stopped = signal_number;
}

static uint64_t monotonic_ns(void)
{
    struct timespec timestamp;
    if (clock_gettime(CLOCK_MONOTONIC, &timestamp) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return (uint64_t)timestamp.tv_sec * 1000000000ULL + (uint64_t)timestamp.tv_nsec;
}

static void write_ready(const char *path)
{
    FILE *output = fopen(path, "w");
    if (output == NULL) {
        perror("ready file");
        exit(2);
    }
    fprintf(output, "%d\n", getpid());
    fclose(output);
}

typedef struct {
    const char *name;
    uint32_t type;
    uint64_t config;
} EventSpec;

typedef struct {
    const char *id;
    EventSpec requested[EVENT_COUNT];
    int requested_count;
    EventSpec opened[EVENT_COUNT];
    int descriptors[EVENT_COUNT];
    int errors[EVENT_COUNT];
    int opened_count;
    int enable_error;
    int active;
    Sample *samples;
    size_t sample_count;
} MultiGroup;

static int run_multi(const char **argv, int argc, int target_tid, uint64_t period_us,
                     uint64_t duration_ms, int reader_cpu)
{
    const EventSpec cycles = {"cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES};
    const EventSpec instructions = {"instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS};
    const char *ids[MULTI_GROUP_COUNT] = {"base", "cache", "branch", "stall", "l1d", "llc", "dtlb"};
    EventSpec extras[6][2] = {
        {{"generic_cache_references", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_REFERENCES},
         {"generic_cache_misses", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_MISSES}},
        {{"branch_instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_BRANCH_INSTRUCTIONS},
         {"branch_misses", PERF_TYPE_HARDWARE, PERF_COUNT_HW_BRANCH_MISSES}},
        {{"stalled_frontend", PERF_TYPE_HARDWARE, PERF_COUNT_HW_STALLED_CYCLES_FRONTEND},
         {"stalled_backend", PERF_TYPE_HARDWARE, PERF_COUNT_HW_STALLED_CYCLES_BACKEND}},
        {{"l1d_read_access", PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_L1D},
         {"l1d_read_misses", PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_L1D | (PERF_COUNT_HW_CACHE_RESULT_MISS << 16)}},
        {{"llc_read_access", PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_LL},
         {"llc_read_misses", PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_LL | (PERF_COUNT_HW_CACHE_RESULT_MISS << 16)}},
        {{"dtlb_read_access", PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_DTLB},
         {"dtlb_read_misses", PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_DTLB | (PERF_COUNT_HW_CACHE_RESULT_MISS << 16)}}
    };
    MultiGroup groups[MULTI_GROUP_COUNT];
    memset(groups, 0, sizeof(groups));
    int active_groups = 0;
    for (int index = 0; index < MULTI_GROUP_COUNT; index++) {
        MultiGroup *group = &groups[index];
        group->id = ids[index];
        group->requested[0] = cycles;
        group->requested[1] = instructions;
        group->requested_count = index ? 4 : 2;
        if (index) {
            group->requested[2] = extras[index - 1][0];
            group->requested[3] = extras[index - 1][1];
        }
        for (int event = 0; event < group->requested_count; event++) {
            struct perf_event_attr attributes;
            memset(&attributes, 0, sizeof(attributes));
            attributes.size = sizeof(attributes);
            attributes.type = group->requested[event].type;
            attributes.config = group->requested[event].config;
            attributes.disabled = 1;
            attributes.exclude_kernel = 1;
            attributes.exclude_hv = 1;
            attributes.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
            int leader = group->opened_count ? group->descriptors[0] : -1;
            int descriptor = (int)syscall(SYS_perf_event_open, &attributes, target_tid, -1, leader, 0);
            if (descriptor < 0) {
                group->errors[event] = errno;
                continue;
            }
            group->opened[group->opened_count] = group->requested[event];
            group->descriptors[group->opened_count++] = descriptor;
        }
        if (group->opened_count) active_groups++;
    }
    uint64_t requested_ns = period_us * 1000ULL, duration_ns = duration_ms * 1000000ULL;
    uint64_t period_ns = requested_ns;
    size_t max_ticks = MULTI_BUFFER_BYTES / (sizeof(Sample) * (active_groups ? (size_t)active_groups : 1));
    uint64_t minimum_period = (duration_ns + max_ticks - 5) / (max_ticks - 4);
    if (period_ns < minimum_period) period_ns = minimum_period;
    size_t capacity = (size_t)(duration_ns / period_ns) + 4;
    for (int index = 0; index < MULTI_GROUP_COUNT; index++) {
        MultiGroup *group = &groups[index];
        if (!group->opened_count) continue;
        group->samples = calloc(capacity, sizeof(Sample));
        if (!group->samples) { perror("multi PMU samples"); return 2; }
        volatile unsigned char *pages = (volatile unsigned char *)group->samples;
        for (size_t offset = 0; offset < capacity * sizeof(Sample); offset += 4096) pages[offset] = 0;
    }
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(reader_cpu, &mask);
    int affinity_error = sched_setaffinity(0, sizeof(mask), &mask) == 0 ? 0 : errno;
    const char *requested_policy = argc == 10 ? argv[9] : "normal";
    int policy_error = 0;
    if (strcmp(requested_policy, "fifo") == 0) {
        struct sched_param scheduling = {.sched_priority = sched_get_priority_min(SCHED_FIFO)};
        policy_error = sched_setscheduler(0, SCHED_FIFO, &scheduling) == 0 ? 0 : errno;
    } else if (strcmp(requested_policy, "normal") != 0) return 2;
    signal(SIGTERM, stop_sampling);
    signal(SIGINT, stop_sampling);
    active_groups = 0;
    for (int index = 0; index < MULTI_GROUP_COUNT; index++) {
        MultiGroup *group = &groups[index];
        if (!group->opened_count) continue;
        if (ioctl(group->descriptors[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) ||
            ioctl(group->descriptors[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP)) {
            group->enable_error = errno;
        } else {
            group->active = 1;
            active_groups++;
        }
    }
    uint64_t begin_ns = monotonic_ns();
    int marker = open("/sys/kernel/tracing/trace_marker", O_WRONLY);
    uint64_t sync_begin_ns = monotonic_ns();
    if (marker >= 0) {
        dprintf(marker, "C|%d|PMU_CLOCK_SYNC|%" PRIu64 "\n", getpid(), sync_begin_ns);
        close(marker);
    }
    uint64_t sync_end_ns = monotonic_ns(), deadline_ns = begin_ns, skipped_deadlines = 0;
    size_t ticks = 0, total_samples = 0;
    if (active_groups) write_ready(argv[8]);
    while (active_groups && !stopped && ticks < capacity) {
        // Rotate read order; every group retains its own timestamp bracket.
        for (int offset = 0; offset < MULTI_GROUP_COUNT; offset++) {
            MultiGroup *group = &groups[(ticks + (size_t)offset) % MULTI_GROUP_COUNT];
            if (!group->active) continue;
            Sample *sample = &group->samples[group->sample_count++];
            sample->reader_cpu = sched_getcpu();
            sample->read_begin_ns = monotonic_ns();
            size_t expected = sizeof(uint64_t) * (3 + (size_t)group->opened_count);
            ssize_t received = read(group->descriptors[0], &sample->counters, expected);
            sample->read_end_ns = monotonic_ns();
            sample->read_error = received == (ssize_t)expected ? 0 : (errno ? errno : EIO);
            total_samples++;
        }
        ticks++;
        uint64_t now = monotonic_ns();
        if (now >= begin_ns + duration_ns) break;
        deadline_ns += period_ns;
        if (deadline_ns <= now) {
            uint64_t missed = (now - deadline_ns) / period_ns + 1;
            skipped_deadlines += missed;
            deadline_ns += missed * period_ns;
        }
        struct timespec deadline = {.tv_sec = (time_t)(deadline_ns / 1000000000ULL), .tv_nsec = (long)(deadline_ns % 1000000000ULL)};
        int status = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL);
        if (status != 0 && status != EINTR) { stopped = status; break; }
    }
    for (int index = 0; index < MULTI_GROUP_COUNT; index++) {
        MultiGroup *group = &groups[index];
        if (group->active) ioctl(group->descriptors[0], PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);
        for (int event = 0; event < group->opened_count; event++) close(group->descriptors[event]);
    }
    uint64_t end_ns = monotonic_ns();
    FILE *output = fopen(argv[6], "w");
    FILE *metadata = fopen(argv[7], "w");
    if (!output || !metadata) { perror("multi PMU output"); return 2; }
    fprintf(output, "sample_index,group_id,read_begin_ns,read_end_ns,event_count,enabled_ns,running_ns,event0,event1,event2,event3,read_error,reader_cpu\n");
    for (size_t tick = 0; tick < ticks; tick++) {
        for (int offset = 0; offset < MULTI_GROUP_COUNT; offset++) {
            MultiGroup *group = &groups[(tick + (size_t)offset) % MULTI_GROUP_COUNT];
            if (tick >= group->sample_count) continue;
            Sample *sample = &group->samples[tick];
            fprintf(output, "%zu,%s,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64,
                    tick, group->id, sample->read_begin_ns, sample->read_end_ns, sample->counters.count,
                    sample->counters.enabled_ns, sample->counters.running_ns);
            for (int event = 0; event < EVENT_COUNT; event++) {
                if (event < group->opened_count) fprintf(output, ",%" PRIu64, sample->counters.values[event]);
                else fputc(',', output);
            }
            fprintf(output, ",%d,%d\n", sample->read_error, sample->reader_cpu);
        }
    }
    fclose(output);
    fprintf(metadata, "{\"schema_version\":\"thread_pmu_capture/v2\",\"format\":\"grouped_long_csv\",\"sampler_version\":\"%s\","
            "\"target_tid\":%d,\"reader_tid\":%d,\"reader_cpu_requested\":%d,\"affinity_error\":%d,"
            "\"period_ns_requested\":%" PRIu64 ",\"period_ns_effective\":%" PRIu64 ",\"buffer_limited\":%s,\"maximum_buffer_bytes\":%llu,"
            "\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ",\"sample_count\":%zu,\"tick_count\":%zu,"
            "\"skipped_deadlines\":%" PRIu64 ",\"stopped_signal\":%d,\"sync_begin_ns\":%" PRIu64 ",\"sync_end_ns\":%" PRIu64 ",\"marker_opened\":%s,"
            "\"scope\":\"target_thread_user_only_no_inherit\",\"clock\":\"CLOCK_MONOTONIC\",\"group\":\"%s\","
            "\"wait_strategy\":\"absolute_clock_nanosleep\",\"requested_policy\":\"%s\",\"actual_policy\":%d,\"policy_error\":%d,"
            "\"scheduling\":\"independent_perf_groups_kernel_multiplexing\",\"groups\":[",
            SAMPLER_VERSION, target_tid, getpid(), reader_cpu, affinity_error, requested_ns, period_ns,
            period_ns != requested_ns ? "true" : "false", MULTI_BUFFER_BYTES, begin_ns, end_ns, total_samples, ticks,
            skipped_deadlines, stopped, sync_begin_ns, sync_end_ns, marker >= 0 ? "true" : "false", argv[5],
            requested_policy, sched_getscheduler(0), policy_error);
    for (int index = 0; index < MULTI_GROUP_COUNT; index++) {
        MultiGroup *group = &groups[index];
        fprintf(metadata, "%s{\"id\":\"%s\",\"status\":\"%s\",\"sample_count\":%zu,\"enable_error\":%d,\"event_names\":[",
                index ? "," : "", group->id,
                !group->active ? "unavailable" : group->opened_count < group->requested_count ? "partial" : "active",
                group->sample_count, group->enable_error);
        for (int event = 0; event < group->opened_count; event++) fprintf(metadata, "%s\"%s\"", event ? "," : "", group->opened[event].name);
        fprintf(metadata, "],\"requested_event_names\":[");
        for (int event = 0; event < group->requested_count; event++) fprintf(metadata, "%s\"%s\"", event ? "," : "", group->requested[event].name);
        fprintf(metadata, "],\"unavailable_events\":[");
        int errors = 0;
        for (int event = 0; event < group->requested_count; event++) {
            int error = group->errors[event] ? group->errors[event] : group->enable_error;
            if (!error) continue;
            fprintf(metadata, "%s{\"name\":\"%s\",\"errno\":%d,\"stage\":\"%s\"}", errors++ ? "," : "",
                    group->requested[event].name, error, group->errors[event] ? "open" : "enable");
        }
        fprintf(metadata, "]}");
        free(group->samples);
    }
    fprintf(metadata, "]}\n");
    fclose(metadata);
    return !active_groups ? 3 : stopped ? 5 : 0;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts("gallery_thread_pmu " SAMPLER_VERSION);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--capabilities") == 0) {
        printf("{\"schema_version\":\"gallery_thread_pmu_capabilities/v1\",\"sampler_version\":\"%s\","
               "\"period_us_min\":%llu,\"period_us_max\":%llu,\"duration_ms_max\":%llu,"
               "\"default_policy\":\"normal\",\"wait_strategy\":\"absolute_clock_nanosleep\","
               "\"period_semantics\":\"requested_interval_not_guaranteed_resolution\","
               "\"event_groups\":[\"auto\",\"all\",\"cache\",\"branch\",\"stall\",\"l1d\",\"llc\",\"dtlb\"],"
               "\"capture_formats\":[\"legacy_group_csv\",\"grouped_long_csv\"]}\n",
               SAMPLER_VERSION, MIN_PERIOD_US, MAX_PERIOD_US, MAX_DURATION_MS);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--clock") == 0) {
        struct timespec realtime;
        uint64_t before_ns = monotonic_ns();
        if (clock_gettime(CLOCK_REALTIME, &realtime) != 0) {
            return 2;
        }
        uint64_t after_ns = monotonic_ns();
        uint64_t realtime_ns = (uint64_t)realtime.tv_sec * 1000000000ULL + (uint64_t)realtime.tv_nsec;
        printf("{\"monotonic_before_ns\":%" PRIu64 ",\"realtime_ns\":%" PRIu64
               ",\"monotonic_after_ns\":%" PRIu64 "}\n", before_ns, realtime_ns, after_ns);
        return 0;
    }
    if (argc != 9 && argc != 10) {
        fprintf(stderr, "usage: sampler target_tid period_us duration_ms reader_cpu auto|all|cache|branch|stall|l1d|llc|dtlb output.csv metadata.json ready_file [normal|fifo]\n");
        return 2;
    }
    uint64_t parsed_tid, period_us, duration_ms, parsed_cpu;
    if (!parse_decimal(argv[1], 1, INT_MAX, &parsed_tid) ||
        !parse_decimal(argv[2], MIN_PERIOD_US, MAX_PERIOD_US, &period_us) ||
        !parse_decimal(argv[3], 1, MAX_DURATION_MS, &duration_ms) ||
        !parse_decimal(argv[4], 0, CPU_SETSIZE - 1, &parsed_cpu)) {
        fprintf(stderr, "Invalid numeric parameters: positive target_tid, period_us=10..10000, duration_ms=1..10000, reader_cpu=0..%d; decimal integers required\n", CPU_SETSIZE - 1);
        return 2;
    }
    int target_tid = (int)parsed_tid, reader_cpu = (int)parsed_cpu;
    uint64_t period_ns = period_us * 1000ULL;
    uint64_t duration_ns = duration_ms * 1000000ULL;
    int multi = strcmp(argv[5], "auto") == 0 || strcmp(argv[5], "all") == 0;
    if (duration_ns < period_ns || (!multi &&
        (strcmp(argv[5], "cache") && strcmp(argv[5], "branch") && strcmp(argv[5], "stall") &&
         strcmp(argv[5], "l1d") && strcmp(argv[5], "llc") && strcmp(argv[5], "dtlb")))) {
        return 2;
    }
    if (multi) return run_multi((const char **)argv, argc, target_tid, period_us, duration_ms, reader_cpu);
    size_t capacity = (size_t)(duration_ns / period_ns) + 4;
    if (capacity > SIZE_MAX / sizeof(Sample)) return 2;
    Sample *samples = calloc(capacity, sizeof(Sample));
    if (samples == NULL) {
        return 2;
    }
    volatile unsigned char *pages = (volatile unsigned char *)samples;
    for (size_t offset = 0; offset < capacity * sizeof(Sample); offset += 4096) {
        pages[offset] = 0;
    }
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(reader_cpu, &mask);
    int affinity_error = sched_setaffinity(0, sizeof(mask), &mask) == 0 ? 0 : errno;
    const char *requested_policy = argc == 10 ? argv[9] : "normal";
    int policy_error = 0;
    if (strcmp(requested_policy, "fifo") == 0) {
        struct sched_param scheduling = {.sched_priority = sched_get_priority_min(SCHED_FIFO)};
        policy_error = sched_setscheduler(0, SCHED_FIFO, &scheduling) == 0 ? 0 : errno;
    } else if (strcmp(requested_policy, "normal") != 0) {
        return 2;
    }
    uint64_t configurations[EVENT_COUNT] = {PERF_COUNT_HW_CPU_CYCLES, PERF_COUNT_HW_INSTRUCTIONS,
        PERF_COUNT_HW_CACHE_REFERENCES, PERF_COUNT_HW_CACHE_MISSES};
    uint32_t types[EVENT_COUNT] = {PERF_TYPE_HARDWARE, PERF_TYPE_HARDWARE, PERF_TYPE_HARDWARE, PERF_TYPE_HARDWARE};
    const char *event2 = "generic_cache_references";
    const char *event3 = "generic_cache_misses";
    if (strcmp(argv[5], "branch") == 0) {
        configurations[2] = PERF_COUNT_HW_BRANCH_INSTRUCTIONS;
        configurations[3] = PERF_COUNT_HW_BRANCH_MISSES;
        event2 = "branch_instructions";
        event3 = "branch_misses";
    } else if (strcmp(argv[5], "stall") == 0) {
        configurations[2] = PERF_COUNT_HW_STALLED_CYCLES_FRONTEND;
        configurations[3] = PERF_COUNT_HW_STALLED_CYCLES_BACKEND;
        event2 = "stalled_frontend";
        event3 = "stalled_backend";
    } else if (strcmp(argv[5], "l1d") == 0 || strcmp(argv[5], "llc") == 0 || strcmp(argv[5], "dtlb") == 0) {
        uint64_t cache = strcmp(argv[5], "l1d") == 0 ? PERF_COUNT_HW_CACHE_L1D :
                         strcmp(argv[5], "llc") == 0 ? PERF_COUNT_HW_CACHE_LL : PERF_COUNT_HW_CACHE_DTLB;
        types[2] = types[3] = PERF_TYPE_HW_CACHE;
        configurations[2] = cache;
        configurations[3] = cache | (PERF_COUNT_HW_CACHE_RESULT_MISS << 16);
        event2 = strcmp(argv[5], "l1d") == 0 ? "l1d_read_access" : strcmp(argv[5], "llc") == 0 ? "llc_read_access" : "dtlb_read_access";
        event3 = strcmp(argv[5], "l1d") == 0 ? "l1d_read_misses" : strcmp(argv[5], "llc") == 0 ? "llc_read_misses" : "dtlb_read_misses";
    }
    int descriptors[EVENT_COUNT] = {-1, -1, -1, -1};
    int open_error = 0;
    for (int event_index = 0; event_index < EVENT_COUNT; event_index++) {
        struct perf_event_attr attributes;
        memset(&attributes, 0, sizeof(attributes));
        attributes.size = sizeof(attributes);
        attributes.type = types[event_index];
        attributes.config = configurations[event_index];
        attributes.disabled = 1;
        attributes.exclude_kernel = 1;
        attributes.exclude_hv = 1;
        attributes.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        descriptors[event_index] = (int)syscall(SYS_perf_event_open, &attributes, target_tid, -1, descriptors[0], 0);
        if (descriptors[event_index] < 0) {
            open_error = errno;
            break;
        }
    }
    FILE *metadata = fopen(argv[7], "w");
    if (metadata == NULL) {
        perror("metadata");
        return 2;
    }
    if (open_error) {
        fprintf(metadata, "{\"sampler_version\":\"%s\",\"open_error\":%d,\"target_tid\":%d}\n", SAMPLER_VERSION, open_error, target_tid);
        fclose(metadata);
        fprintf(stderr, "perf_event_open: %s\n", strerror(open_error));
        return 3;
    }
    signal(SIGTERM, stop_sampling);
    signal(SIGINT, stop_sampling);
    if (ioctl(descriptors[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) ||
        ioctl(descriptors[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP)) {
        perror("enable PMU");
        return 4;
    }
    uint64_t begin_ns = monotonic_ns();
    int marker = open("/sys/kernel/tracing/trace_marker", O_WRONLY);
    uint64_t sync_begin_ns = monotonic_ns();
    if (marker >= 0) {
        dprintf(marker, "C|%d|PMU_CLOCK_SYNC|%" PRIu64 "\n", getpid(), sync_begin_ns);
        close(marker);
    }
    uint64_t sync_end_ns = monotonic_ns();
    uint64_t deadline_ns = begin_ns;
    uint64_t skipped_deadlines = 0;
    size_t count = 0;
    write_ready(argv[8]);
    while (!stopped && count < capacity) {
        Sample *sample = &samples[count];
        sample->reader_cpu = sched_getcpu();
        sample->read_begin_ns = monotonic_ns();
        ssize_t received = read(descriptors[0], &sample->counters, sizeof(sample->counters));
        sample->read_end_ns = monotonic_ns();
        sample->read_error = received == sizeof(sample->counters) ? 0 : (errno ? errno : EIO);
        count++;
        if (sample->read_end_ns >= begin_ns + duration_ns) {
            break;
        }
        deadline_ns += period_ns;
        if (deadline_ns <= sample->read_end_ns) {
            uint64_t missed = (sample->read_end_ns - deadline_ns) / period_ns + 1;
            skipped_deadlines += missed;
            deadline_ns += missed * period_ns;
        }
        struct timespec deadline = {.tv_sec = (time_t)(deadline_ns / 1000000000ULL),
                                    .tv_nsec = (long)(deadline_ns % 1000000000ULL)};
        int status = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL);
        if (status != 0 && status != EINTR) {
            fprintf(stderr, "clock_nanosleep: %s\n", strerror(status));
            stopped = status;
        }
    }
    ioctl(descriptors[0], PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);
    uint64_t end_ns = monotonic_ns();
    for (int event_index = 0; event_index < EVENT_COUNT; event_index++) {
        close(descriptors[event_index]);
    }
    FILE *output = fopen(argv[6], "w");
    if (output == NULL) {
        perror("samples output");
        return 2;
    }
    fprintf(output, "read_begin_ns,read_end_ns,event_count,enabled_ns,running_ns,cycles,instructions,event2,event3,read_error,reader_cpu\n");
    for (size_t index = 0; index < count; index++) {
        Sample *sample = &samples[index];
        fprintf(output, "%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ","
                "%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%d,%d\n",
                sample->read_begin_ns, sample->read_end_ns, sample->counters.count,
                sample->counters.enabled_ns, sample->counters.running_ns,
                sample->counters.values[0], sample->counters.values[1],
                sample->counters.values[2], sample->counters.values[3], sample->read_error, sample->reader_cpu);
    }
    fclose(output);
    fprintf(metadata, "{\"sampler_version\":\"%s\",\"wait_strategy\":\"absolute_clock_nanosleep\",\"target_tid\":%d,\"reader_tid\":%d,\"reader_cpu_requested\":%d,\"affinity_error\":%d,"
            "\"period_ns_requested\":%" PRIu64 ",\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ","
            "\"sample_count\":%zu,\"skipped_deadlines\":%" PRIu64 ",\"stopped_signal\":%d,"
            "\"sync_begin_ns\":%" PRIu64 ",\"sync_end_ns\":%" PRIu64 ",\"marker_opened\":%s,"
            "\"requested_policy\":\"%s\",\"actual_policy\":%d,\"policy_error\":%d,"
            "\"scope\":\"target_thread_user_only_no_inherit\",\"clock\":\"CLOCK_MONOTONIC\","
            "\"group\":\"%s\",\"event_names\":[\"cycles\",\"instructions\",\"%s\",\"%s\"]}\n",
            SAMPLER_VERSION, target_tid, getpid(), reader_cpu, affinity_error, period_ns, begin_ns, end_ns,
            count, skipped_deadlines, stopped, sync_begin_ns, sync_end_ns, marker >= 0 ? "true" : "false",
            requested_policy, sched_getscheduler(0), policy_error,
            argv[5], event2, event3);
    fclose(metadata);
    free(samples);
    return stopped ? 5 : 0;
}
