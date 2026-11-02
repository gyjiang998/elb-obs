#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/perf_event.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define EVENT_COUNT 4

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

int main(int argc, char **argv)
{
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
        fprintf(stderr, "usage: sampler target_tid period_us duration_ms reader_cpu cache|branch|stall|l1d|llc|dtlb output.csv metadata.json ready_file [normal|fifo]\n");
        return 2;
    }
    int target_tid = atoi(argv[1]);
    uint64_t period_ns = strtoull(argv[2], NULL, 10) * 1000ULL;
    uint64_t duration_ns = strtoull(argv[3], NULL, 10) * 1000000ULL;
    int reader_cpu = atoi(argv[4]);
    if (target_tid < 1 || period_ns < 20000 || period_ns > 10000000 || duration_ns < period_ns ||
        duration_ns > 10000000000ULL || reader_cpu < 0 || reader_cpu >= CPU_SETSIZE ||
        (strcmp(argv[5], "cache") && strcmp(argv[5], "branch") && strcmp(argv[5], "stall") &&
         strcmp(argv[5], "l1d") && strcmp(argv[5], "llc") && strcmp(argv[5], "dtlb"))) {
        return 2;
    }
    size_t capacity = (size_t)(duration_ns / period_ns) + 4;
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
        fprintf(metadata, "{\"open_error\":%d,\"target_tid\":%d}\n", open_error, target_tid);
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
    fprintf(metadata, "{\"target_tid\":%d,\"reader_tid\":%d,\"reader_cpu_requested\":%d,\"affinity_error\":%d,"
            "\"period_ns_requested\":%" PRIu64 ",\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ","
            "\"sample_count\":%zu,\"skipped_deadlines\":%" PRIu64 ",\"stopped_signal\":%d,"
            "\"sync_begin_ns\":%" PRIu64 ",\"sync_end_ns\":%" PRIu64 ",\"marker_opened\":%s,"
            "\"requested_policy\":\"%s\",\"actual_policy\":%d,\"policy_error\":%d,"
            "\"scope\":\"target_thread_user_only_no_inherit\",\"clock\":\"CLOCK_MONOTONIC\","
            "\"group\":\"%s\",\"event_names\":[\"cycles\",\"instructions\",\"%s\",\"%s\"]}\n",
            target_tid, getpid(), reader_cpu, affinity_error, period_ns, begin_ns, end_ns,
            count, skipped_deadlines, stopped, sync_begin_ns, sync_end_ns, marker >= 0 ? "true" : "false",
            requested_policy, sched_getscheduler(0), policy_error,
            argv[5], event2, event3);
    fclose(metadata);
    free(samples);
    return stopped ? 5 : 0;
}
