/* Portable calibration proxy. This measures worker CPU demand, not UI speed.
 * Build on the target ABI: cc -O2 -std=c11 -pthread benchmark.c -o benchmark
 * No DVFS writes, privileged setup, external libraries, or device transport.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#if defined(__linux__)
#include <linux/perf_event.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#define CALIBRATION_HAS_PERF_EVENT 1
#else
#define CALIBRATION_HAS_PERF_EVENT 0
#endif

#define MAX_WORKERS 64
#define MAX_CORE_LOGICAL_CPUS 64
#define WORKLOAD_VERSION "parallel-calibration-proxy-v6"
#define ROOFLINE_KERNEL_VERSION "classic-roofline-blocked-v2"
#define ROOFLINE_COMPUTE_FLOPS_PER_UNIT 32
#define ROOFLINE_ILP_LANES 16
#define TARGET_SIBLING_BUSY_FRACTION_LIMIT .05
#define FREQUENCY_PATH_CAPACITY 4096
#define WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE 3
#define WARMUP_FREQUENCY_MAX_SAMPLES 12
#define WARMUP_FREQUENCY_TOLERANCE .03
#define WARMUP_CHUNK_MAX_UNITS UINT64_C(32768)
#if defined(PARALLEL_CALIBRATION_PORTABLE_SMOKE_TEST) && defined(__GNUC__)
#define PORTABLE_UNUSED __attribute__((unused))
#else
#define PORTABLE_UNUSED
#endif

typedef struct Node {
    struct Node *left, *right;
    uint64_t value, attributes[4];
} Node;

typedef struct {
    const char *benchmark, *family;
    const char *workload_class, *peer_workload_class;
    const char *frequency_policy_id, *peer_frequency_policy_id;
    int target_cpu, observer_cpu, helper_cpus[MAX_WORKERS], helper_count;
    int active_tasks, sample_period_us;
    uint64_t work_units;
    uint64_t working_set_bytes;
    int roofline_operations_per_element;
    long frequency_khz, peer_frequency_khz;
    double frequency_tolerance;
} Options;

typedef struct {
    int cpu;
    const char *kind;
    char path[FREQUENCY_PATH_CAPACITY];
    long initial_value;
} FrequencySource;

typedef struct {
    uint64_t sample_count, valid_sample_count;
    int in_range_consecutive, required_consecutive, max_samples, verified;
    long minimum, maximum, last;
} WarmupFrequency;

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    int ready, start;
    pthread_mutex_t queue_mutex;
    pthread_cond_t queue_condition;
    uint64_t completed;
    int pending;
    uint64_t pending_task;
    int dispatch_ready, direct_start, direct_completed, queue_start;
    double direct_producer_cpu_ms, direct_roundtrip_wall_ms;
    double queue_producer_cpu_ms, queue_roundtrip_wall_ms;
    atomic_int stop, helpers_active, sampler_ready;
    Options options;
    FrequencySource target_frequency_source, peer_frequency_source;
} Shared;

typedef struct {
    int cpu, verified;
    uint64_t busy_before, total_before, busy_after, total_after;
    uint64_t busy_ticks, total_ticks;
    double busy_fraction;
} SiblingActivity;

typedef struct {
    int target_cpu, sibling_count;
    int topology_verified, activity_verified, quietness_verified;
    uint64_t before_snapshot_ns, after_snapshot_ns;
    double busy_fraction;
    SiblingActivity siblings[MAX_CORE_LOGICAL_CPUS];
} SiblingQuietness;

enum {
    PMU_CYCLES = 0,
    PMU_INSTRUCTIONS = 1,
    PMU_CACHE_REFERENCES = 2,
    PMU_CACHE_MISSES = 3,
    PMU_EVENT_COUNT = 4
};

typedef struct {
    int descriptor;
    int open_error;
    int reset_error;
    int enable_error;
    int disable_error;
    int read_error;
    uint64_t raw_value;
    uint64_t enabled_ns;
    uint64_t running_ns;
    double scaled_value;
    int available;
} PmuCounter;

typedef struct {
    PmuCounter counters[PMU_EVENT_COUNT];
    int attempted;
    int group_leader_descriptor;
    int window_started;
    int window_finished;
} PmuMeasurement;

typedef struct {
    Shared *shared;
    int cpu, target, affinity_verified, actual_cpu;
    double cpu_ms, wall_ms;
    double cpu_busy_fraction, overlap_cpu_lower_bound_ms, overlap_cpu_lower_bound_fraction;
    int covers_target_interval;
    uint64_t sink, completed_units, warmup_units, start_ns, end_ns;
    uint64_t direct_sink, queue_sink;
    double direct_cpu_ms, direct_wall_ms;
    double queue_cpu_ms, queue_wall_ms;
    volatile uint64_t *memory;
    void *roofline_memory;
    uint64_t roofline_element_count;
    uint64_t memory_index;
    SiblingQuietness sibling_quietness;
    WarmupFrequency warmup_frequency;
    PmuMeasurement pmu;
} Worker;

typedef struct {
    Shared *shared;
    uint64_t samples, read_failures;
    long minimum, maximum;
    long double sum;
    int affinity_verified, actual_cpu;
    uint64_t first_sample_ns, last_sample_ns;
    uint64_t peer_samples, peer_read_failures;
    long peer_minimum, peer_maximum;
    long double peer_sum;
} Sampler;

static uint64_t now_ns(clockid_t clock) {
    struct timespec value;
    if (clock_gettime(clock, &value) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return (uint64_t)value.tv_sec * UINT64_C(1000000000) + (uint64_t)value.tv_nsec;
}

static const char *pmu_event_name(int index) {
    static const char *names[PMU_EVENT_COUNT] = {
        "cycles", "instructions", "generic_cache_references", "generic_cache_misses"
    };
    return index >= 0 && index < PMU_EVENT_COUNT ? names[index] : "unknown";
}

static void pmu_prepare(PmuMeasurement *measurement) {
    memset(measurement, 0, sizeof(*measurement));
    measurement->attempted = 1;
    measurement->group_leader_descriptor = -1;
    for (int index = 0; index < PMU_EVENT_COUNT; index++) measurement->counters[index].descriptor = -1;
#if CALIBRATION_HAS_PERF_EVENT
    static const uint64_t configurations[PMU_EVENT_COUNT] = {
        PERF_COUNT_HW_CPU_CYCLES,
        PERF_COUNT_HW_INSTRUCTIONS,
        PERF_COUNT_HW_CACHE_REFERENCES,
        PERF_COUNT_HW_CACHE_MISSES
    };
    for (int index = 0; index < PMU_EVENT_COUNT; index++) {
        struct perf_event_attr attributes;
        memset(&attributes, 0, sizeof(attributes));
        attributes.size = sizeof(attributes);
        attributes.type = PERF_TYPE_HARDWARE;
        attributes.config = configurations[index];
        attributes.disabled = 1;
        attributes.exclude_kernel = 1;
        attributes.exclude_hv = 1;
        attributes.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        int descriptor = (int)syscall(SYS_perf_event_open, &attributes, 0, -1,
                                      measurement->group_leader_descriptor, 0);
        if (descriptor < 0) measurement->counters[index].open_error = errno ? errno : EIO;
        else {
            measurement->counters[index].descriptor = descriptor;
            if (measurement->group_leader_descriptor < 0)
                measurement->group_leader_descriptor = descriptor;
        }
    }
#else
    for (int index = 0; index < PMU_EVENT_COUNT; index++) measurement->counters[index].open_error = ENOSYS;
#endif
}

static void pmu_begin(PmuMeasurement *measurement) {
#if CALIBRATION_HAS_PERF_EVENT
    if (measurement->group_leader_descriptor < 0) return;
    if (ioctl(measurement->group_leader_descriptor, PERF_EVENT_IOC_RESET,
              PERF_IOC_FLAG_GROUP) != 0) {
        int error = errno ? errno : EIO;
        for (int index = 0; index < PMU_EVENT_COUNT; index++)
            if (measurement->counters[index].descriptor >= 0)
                measurement->counters[index].reset_error = error;
        return;
    }
    if (ioctl(measurement->group_leader_descriptor, PERF_EVENT_IOC_ENABLE,
              PERF_IOC_FLAG_GROUP) != 0) {
        int error = errno ? errno : EIO;
        for (int index = 0; index < PMU_EVENT_COUNT; index++)
            if (measurement->counters[index].descriptor >= 0)
                measurement->counters[index].enable_error = error;
        return;
    }
    measurement->window_started = 1;
#else
    (void)measurement;
#endif
}

static void pmu_end(PmuMeasurement *measurement) {
#if CALIBRATION_HAS_PERF_EVENT
    typedef struct { uint64_t value, enabled_ns, running_ns; } PmuRead;
    if (measurement->window_started && measurement->group_leader_descriptor >= 0) {
        if (ioctl(measurement->group_leader_descriptor, PERF_EVENT_IOC_DISABLE,
                  PERF_IOC_FLAG_GROUP) != 0) {
            int error = errno ? errno : EIO;
            for (int index = 0; index < PMU_EVENT_COUNT; index++)
                if (measurement->counters[index].descriptor >= 0)
                    measurement->counters[index].disable_error = error;
        } else measurement->window_finished = 1;
    }
    for (int index = 0; index < PMU_EVENT_COUNT; index++) {
        PmuCounter *counter = &measurement->counters[index];
        if (counter->descriptor < 0) continue;
        PmuRead value = {0};
        ssize_t received = read(counter->descriptor, &value, sizeof(value));
        if (received != (ssize_t)sizeof(value)) counter->read_error = errno ? errno : EIO;
        else {
            counter->raw_value = value.value;
            counter->enabled_ns = value.enabled_ns;
            counter->running_ns = value.running_ns;
            if (measurement->window_started && measurement->window_finished
                    && !counter->reset_error && !counter->enable_error && !counter->disable_error
                    && value.running_ns > 0 && value.enabled_ns >= value.running_ns) {
                counter->scaled_value = (double)value.value * (double)value.enabled_ns / (double)value.running_ns;
                counter->available = 1;
            }
        }
        close(counter->descriptor);
        counter->descriptor = -1;
    }
#else
    (void)measurement;
#endif
}

static void print_pmu_measurement(const PmuMeasurement *measurement) {
    int available = 0;
    for (int index = 0; index < PMU_EVENT_COUNT; index++) available += measurement->counters[index].available;
    const PmuCounter *cycles = &measurement->counters[PMU_CYCLES];
    const PmuCounter *instructions = &measurement->counters[PMU_INSTRUCTIONS];
    const PmuCounter *references = &measurement->counters[PMU_CACHE_REFERENCES];
    const PmuCounter *misses = &measurement->counters[PMU_CACHE_MISSES];
    int aligned = measurement->window_started && measurement->window_finished;
    printf("\"pmu_collection_status\":\"%s\",\"pmu_scope\":\"target_thread_user_only_timed_window\",",
           available == PMU_EVENT_COUNT ? "collected" : available ? "partial" : "unavailable");
    printf("\"pmu_window_aligned\":%s,\"pmu_window_relation\":\"perf_group_encloses_timed_workload_window; timed wall/cpu exclude perf control/read overhead\",",
           aligned ? "true" : "false");
    printf("\"pmu_counting_semantics\":\"perf_event_open per-thread user-only group; one PERF_IOC_FLAG_GROUP reset/enable/disable window; each count scaled by time_enabled/time_running\",");
    printf("\"pmu_cycles\":"); if (cycles->available) printf("%.17g", cycles->scaled_value); else printf("null");
    printf(",\"pmu_retired_instructions\":"); if (instructions->available) printf("%.17g", instructions->scaled_value); else printf("null");
    printf(",\"pmu_cache_references\":"); if (references->available) printf("%.17g", references->scaled_value); else printf("null");
    printf(",\"pmu_cache_misses\":"); if (misses->available) printf("%.17g", misses->scaled_value); else printf("null");
    printf(",\"pmu_ipc\":");
    if (aligned && cycles->available && instructions->available && cycles->scaled_value > 0)
        printf("%.17g", instructions->scaled_value / cycles->scaled_value);
    else printf("null");
    printf(",\"pmu_cache_mpki\":");
    if (aligned && misses->available && instructions->available && instructions->scaled_value > 0)
        printf("%.17g", misses->scaled_value * 1000.0 / instructions->scaled_value);
    else printf("null");
    printf(",\"pmu_counters\":[");
    for (int index = 0; index < PMU_EVENT_COUNT; index++) {
        const PmuCounter *counter = &measurement->counters[index];
        if (index) putchar(',');
        printf("{\"event\":\"%s\",\"status\":\"%s\",\"raw_value\":", pmu_event_name(index),
               counter->available ? "collected" : "unavailable");
        if (counter->available) printf("%" PRIu64, counter->raw_value); else printf("null");
        printf(",\"scaled_value\":"); if (counter->available) printf("%.17g", counter->scaled_value); else printf("null");
        printf(",\"enabled_ns\":"); if (counter->available) printf("%" PRIu64, counter->enabled_ns); else printf("null");
        printf(",\"running_ns\":"); if (counter->available) printf("%" PRIu64, counter->running_ns); else printf("null");
        printf(",\"error\":");
        int error = counter->open_error ? counter->open_error : counter->reset_error ? counter->reset_error
            : counter->enable_error ? counter->enable_error : counter->disable_error ? counter->disable_error : counter->read_error;
        if (error) printf("%d", error); else printf("null");
        putchar('}');
    }
    printf("],");
}

static int pin_cpu(int cpu) {
#ifdef __linux__
    cpu_set_t desired, actual;
    if (cpu < 0 || cpu >= CPU_SETSIZE) return 0;
    CPU_ZERO(&desired);
    CPU_SET(cpu, &desired);
    if (sched_setaffinity(0, sizeof(desired), &desired) != 0) return 0;
    CPU_ZERO(&actual);
    if (sched_getaffinity(0, sizeof(actual), &actual) != 0) return 0;
    return CPU_COUNT(&actual) == 1 && CPU_ISSET(cpu, &actual) && sched_getcpu() == cpu;
#else
    (void)cpu;
    return 0;
#endif
}

static int actual_cpu(void) {
#ifdef __linux__
    return sched_getcpu();
#else
    return -1;
#endif
}

static long read_long_file(const char *path) {
    FILE *file = fopen(path, "r");
    long result = -1;
    if (file) {
        if (fscanf(file, "%ld", &result) != 1) result = -1;
        fclose(file);
    }
    return result;
}

static int valid_frequency_policy_id(const char *value) {
    const char *digits = NULL;
    if (!value) return 0;
    if (!strncmp(value, "policy", 6)) digits = value + 6;
    else if (!strncmp(value, "cpu", 3)) digits = value + 3;
    else return 0;
    if (!*digits) return 0;
    long number = 0;
    for (const char *cursor = digits; *cursor; cursor++) {
        if (*cursor < '0' || *cursor > '9') return 0;
        number = number * 10 + (*cursor - '0');
        if (number > 4095) return 0;
    }
    return 1;
}

static int scaling_frequency_path_at(
    char *output, size_t size, const char *cpu_root, const char *policy_id
) {
    if (!valid_frequency_policy_id(policy_id)) return 0;
    int written = !strncmp(policy_id, "policy", 6)
        ? snprintf(output, size, "%s/cpufreq/%s/scaling_cur_freq", cpu_root, policy_id)
        : snprintf(output, size, "%s/%s/cpufreq/scaling_cur_freq", cpu_root, policy_id);
    return written > 0 && (size_t)written < size;
}

static int cpuinfo_frequency_path_at(
    char *output, size_t size, const char *cpu_root, int cpu
) {
    int written = snprintf(output, size, "%s/cpu%d/cpufreq/cpuinfo_cur_freq", cpu_root, cpu);
    return written > 0 && (size_t)written < size;
}

/* Freeze exactly one source before warm-up.  cpuinfo_cur_freq is tied to the
 * logical CPU whose work is being measured.  scaling_cur_freq is retained as
 * a diagnostic fallback only when cpuinfo is unavailable at this initial
 * decision; later read failures never switch the source mid-window. */
static void freeze_frequency_source_at(
    FrequencySource *source, int cpu, const char *policy_id, const char *cpu_root
) {
    memset(source, 0, sizeof(*source));
    source->cpu = cpu;
    source->initial_value = -1;
    if (cpuinfo_frequency_path_at(source->path, sizeof(source->path), cpu_root, cpu))
        source->initial_value = read_long_file(source->path);
    if (source->initial_value > 0) {
        source->kind = "cpuinfo_cur_freq";
        return;
    }
    source->kind = "scaling_cur_freq_fallback";
    source->path[0] = '\0';
    if (!scaling_frequency_path_at(source->path, sizeof(source->path), cpu_root, policy_id)) {
        source->path[0] = '\0';
        return;
    }
    source->initial_value = read_long_file(source->path);
}

static void freeze_frequency_source(
    FrequencySource *source, int cpu, const char *policy_id
) {
    freeze_frequency_source_at(source, cpu, policy_id, "/sys/devices/system/cpu");
}

static long read_frozen_frequency(const FrequencySource *source) {
    return source->path[0] ? read_long_file(source->path) : -1;
}

static void warmup_frequency_init(WarmupFrequency *frequency) {
    memset(frequency, 0, sizeof(*frequency));
    frequency->required_consecutive = WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE;
    frequency->max_samples = WARMUP_FREQUENCY_MAX_SAMPLES;
    frequency->minimum = LONG_MAX;
    frequency->maximum = LONG_MIN;
    frequency->last = -1;
}

static void warmup_frequency_observe(
    WarmupFrequency *frequency, long observed_khz, long requested_khz
) {
    if (frequency->sample_count >= (uint64_t)frequency->max_samples || frequency->verified) return;
    frequency->sample_count++;
    if (observed_khz <= 0) {
        frequency->in_range_consecutive = 0;
        return;
    }
    frequency->valid_sample_count++;
    frequency->last = observed_khz;
    if (observed_khz < frequency->minimum) frequency->minimum = observed_khz;
    if (observed_khz > frequency->maximum) frequency->maximum = observed_khz;
    double low = requested_khz * (1 - WARMUP_FREQUENCY_TOLERANCE);
    double high = requested_khz * (1 + WARMUP_FREQUENCY_TOLERANCE);
    if (observed_khz >= low && observed_khz <= high)
        frequency->in_range_consecutive++;
    else
        frequency->in_range_consecutive = 0;
    frequency->verified =
        frequency->in_range_consecutive >= frequency->required_consecutive;
}

static int frequency_window_verified(
    const FrequencySource *source, const WarmupFrequency *warmup,
    int require_warmup, uint64_t samples, uint64_t read_failures,
    long minimum, long maximum, long requested_khz, double tolerance
) {
    if (!source->kind || strcmp(source->kind, "cpuinfo_cur_freq") ||
        (require_warmup && (!warmup || !warmup->verified)) ||
        samples < 5 || read_failures) return 0;
    double low = requested_khz * (1 - tolerance);
    double high = requested_khz * (1 + tolerance);
    return minimum >= low && maximum <= high;
}

typedef enum {
    PHYSICAL_CORE_DIFFERENT = 0,
    PHYSICAL_CORE_SAME = 1,
    PHYSICAL_CORE_UNKNOWN = 2,
    PHYSICAL_CORE_CONFLICT = 3
} PhysicalCoreRelation;

typedef enum {
    CPU_LIST_ABSENT = 0,
    CPU_LIST_VALID = 1,
    CPU_LIST_INVALID = 2
} CpuListStatus;

#define TOPOLOGY_CPU_LIMIT 4096
#define TOPOLOGY_CPU_WORDS (TOPOLOGY_CPU_LIMIT / 64)

typedef struct {
    CpuListStatus status;
    uint64_t bits[TOPOLOGY_CPU_WORDS];
} CpuList;

static int topology_path(char *path, size_t size, const char *root, int cpu, const char *field) {
    int written = snprintf(path, size, "%s/cpu%d/topology/%s", root, cpu, field);
    return written > 0 && (size_t)written < size;
}

static int cpu_list_contains(const CpuList *list, int cpu) {
    return cpu >= 0 && cpu < TOPOLOGY_CPU_LIMIT &&
           (list->bits[cpu / 64] & (UINT64_C(1) << (cpu % 64))) != 0;
}

static int cpu_lists_equal(const CpuList *left, const CpuList *right) {
    return memcmp(left->bits, right->bits, sizeof(left->bits)) == 0;
}

static int topology_space(char value) {
    return value == ' ' || value == '\t' || value == '\r' || value == '\n';
}

static CpuList read_cpu_list(const char *root, int cpu, const char *field) {
    CpuList result = {0};
    char path[4096], contents[4096];
    if (!topology_path(path, sizeof(path), root, cpu, field)) {
        result.status = CPU_LIST_INVALID;
        return result;
    }
    errno = 0;
    FILE *file = fopen(path, "r");
    if (!file) {
        result.status = errno == ENOENT || errno == ENOTDIR ? CPU_LIST_ABSENT : CPU_LIST_INVALID;
        return result;
    }
    size_t size = fread(contents, 1, sizeof(contents) - 1, file);
    int extra = fgetc(file), read_error = ferror(file);
    fclose(file);
    if (read_error || extra != EOF || !size || memchr(contents, '\0', size)) {
        result.status = CPU_LIST_INVALID;
        return result;
    }
    contents[size] = '\0';

    const char *cursor = contents;
    int entries = 0;
    while (topology_space(*cursor)) cursor++;
    while (*cursor) {
        if (*cursor < '0' || *cursor > '9') {
            result.status = CPU_LIST_INVALID;
            return result;
        }
        char *end = NULL;
        long first = strtol(cursor, &end, 10), last = first;
        if (end == cursor || first < 0 || first >= TOPOLOGY_CPU_LIMIT) {
            result.status = CPU_LIST_INVALID;
            return result;
        }
        cursor = end;
        if (*cursor == '-') {
            cursor++;
            if (*cursor < '0' || *cursor > '9') {
                result.status = CPU_LIST_INVALID;
                return result;
            }
            last = strtol(cursor, &end, 10);
            if (end == cursor || last < first || last >= TOPOLOGY_CPU_LIMIT) {
                result.status = CPU_LIST_INVALID;
                return result;
            }
            cursor = end;
        }
        for (long member = first; member <= last; member++)
            result.bits[member / 64] |= UINT64_C(1) << (member % 64);
        entries++;
        while (topology_space(*cursor)) cursor++;
        if (!*cursor) break;
        if (*cursor != ',') {
            result.status = CPU_LIST_INVALID;
            return result;
        }
        cursor++;
        while (topology_space(*cursor)) cursor++;
        if (!*cursor) {
            result.status = CPU_LIST_INVALID;
            return result;
        }
    }
    result.status = entries ? CPU_LIST_VALID : CPU_LIST_INVALID;
    return result;
}

/* Validate the complete sibling set, not only the queried pair. Every member
 * must publish the exact same set; otherwise two unrelated queried CPUs could
 * be reported as different while one of their own groups is incomplete. */
static CpuList validated_cpu_group(const char *root, int cpu, const char *field) {
    CpuList group = read_cpu_list(root, cpu, field);
    if (group.status == CPU_LIST_INVALID) return group;
    if (group.status == CPU_LIST_VALID && !cpu_list_contains(&group, cpu)) {
        group.status = CPU_LIST_INVALID;
        return group;
    }
    if (group.status == CPU_LIST_VALID) {
        for (int member = 0; member < TOPOLOGY_CPU_LIMIT; member++) {
            if (!cpu_list_contains(&group, member)) continue;
            CpuList peer = read_cpu_list(root, member, field);
            if (peer.status != CPU_LIST_VALID || !cpu_lists_equal(&group, &peer)) {
                group.status = CPU_LIST_INVALID;
                return group;
            }
        }
    }
    /* Also reject reverse-only membership. Validating only the CPUs listed by
     * `cpu` misses a broken graph such as cpu0="0", cpu1="0-1". Scan every
     * CPU directory exposed by sysfs and require that nobody outside this
     * group claims one of its members. */
    DIR *directory = opendir(root);
    if (!directory) {
        group.status = CPU_LIST_INVALID;
        return group;
    }
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strncmp(entry->d_name, "cpu", 3) != 0 || entry->d_name[3] < '0' || entry->d_name[3] > '9') continue;
        char *end = NULL;
        long candidate = strtol(entry->d_name + 3, &end, 10);
        if (!end || *end || candidate < 0 || candidate >= TOPOLOGY_CPU_LIMIT
            || (group.status == CPU_LIST_VALID && cpu_list_contains(&group, (int)candidate))) continue;
        CpuList peer = read_cpu_list(root, (int)candidate, field);
        if ((group.status == CPU_LIST_VALID && peer.status == CPU_LIST_INVALID)
            || (peer.status == CPU_LIST_VALID && cpu_list_contains(&peer, cpu))) {
            group.status = CPU_LIST_INVALID;
            break;
        }
    }
    closedir(directory);
    return group;
}

static PhysicalCoreRelation physical_core_relation_from_list(
    const char *root, int left_cpu, int right_cpu, const char *field
) {
    CpuList left = validated_cpu_group(root, left_cpu, field);
    CpuList right = validated_cpu_group(root, right_cpu, field);
    if (left.status == CPU_LIST_ABSENT && right.status == CPU_LIST_ABSENT)
        return PHYSICAL_CORE_UNKNOWN;
    if (left.status != CPU_LIST_VALID || right.status != CPU_LIST_VALID)
        return PHYSICAL_CORE_CONFLICT;
    return cpu_lists_equal(&left, &right) ? PHYSICAL_CORE_SAME : PHYSICAL_CORE_DIFFERENT;
}

static int topology_at(const char *root, int cpu, long *package, long *core) {
    char path[4096];
    if (!topology_path(path, sizeof(path), root, cpu, "physical_package_id")) return 0;
    *package = read_long_file(path);
    if (!topology_path(path, sizeof(path), root, cpu, "core_id")) return 0;
    *core = read_long_file(path);
    return *package >= 0 && *core >= 0;
}

static PhysicalCoreRelation physical_core_relation_at(const char *root, int left_cpu, int right_cpu) {
    if (left_cpu == right_cpu) return PHYSICAL_CORE_SAME;
    PhysicalCoreRelation core = physical_core_relation_from_list(
        root, left_cpu, right_cpu, "core_cpus_list");
    PhysicalCoreRelation siblings = physical_core_relation_from_list(
        root, left_cpu, right_cpu, "thread_siblings_list");

    if (core == PHYSICAL_CORE_CONFLICT || siblings == PHYSICAL_CORE_CONFLICT)
        return PHYSICAL_CORE_CONFLICT;
    if (core != PHYSICAL_CORE_UNKNOWN && siblings != PHYSICAL_CORE_UNKNOWN)
        return core == siblings ? core : PHYSICAL_CORE_CONFLICT;
    if (core != PHYSICAL_CORE_UNKNOWN) return core;
    if (siblings != PHYSICAL_CORE_UNKNOWN) return siblings;

    /* core_id is a last-resort compatibility signal. Kirin may assign one ID
     * per logical CPU, so it is used only when both sibling-list mechanisms are
     * completely absent for both CPUs. */
    long left_package, left_core, right_package, right_core;
    if (topology_at(root, left_cpu, &left_package, &left_core) &&
        topology_at(root, right_cpu, &right_package, &right_core)) {
        // Equal legacy IDs are sufficient to reject a shared-core placement.
        // Different IDs are not sufficient to prove independence: current
        // Kirin kernels may assign one core_id per logical thread.
        return left_package == right_package && left_core == right_core
            ? PHYSICAL_CORE_SAME : PHYSICAL_CORE_UNKNOWN;
    }
    return PHYSICAL_CORE_UNKNOWN;
}

static PORTABLE_UNUSED PhysicalCoreRelation physical_core_relation(int left_cpu, int right_cpu) {
    return physical_core_relation_at("/sys/devices/system/cpu", left_cpu, right_cpu);
}

/* Return the complete logical-CPU set for one physical core.  Equal legacy
 * package/core IDs can reject a bad placement, but they cannot prove that the
 * set is complete on Kirin kernels that expose one core_id per logical CPU.
 * Quietness evidence therefore requires one of the reciprocal cpulist sources. */
static CpuList physical_core_group_at(const char *root, int cpu) {
    CpuList core = validated_cpu_group(root, cpu, "core_cpus_list");
    CpuList siblings = validated_cpu_group(root, cpu, "thread_siblings_list");
    if (core.status == CPU_LIST_INVALID || siblings.status == CPU_LIST_INVALID) {
        core.status = CPU_LIST_INVALID;
        return core;
    }
    if (core.status == CPU_LIST_VALID && siblings.status == CPU_LIST_VALID) {
        if (!cpu_lists_equal(&core, &siblings)) core.status = CPU_LIST_INVALID;
        return core;
    }
    if (core.status == CPU_LIST_VALID) return core;
    if (siblings.status == CPU_LIST_VALID) return siblings;
    core.status = CPU_LIST_ABSENT;
    return core;
}

static int add_u64(uint64_t *sum, uint64_t value) {
    if (UINT64_MAX - *sum < value) return 0;
    *sum += value;
    return 1;
}

static int cpu_label_id(const char *label, int *cpu) {
    if (strncmp(label, "cpu", 3) || label[3] < '0' || label[3] > '9') return 0;
    char *end = NULL;
    errno = 0;
    long parsed = strtol(label + 3, &end, 10);
    if (errno || !end || *end || parsed < 0 || parsed >= TOPOLOGY_CPU_LIMIT) return 0;
    *cpu = (int)parsed;
    return 1;
}

static int read_sibling_activity_snapshot(
    const char *proc_stat_path, SiblingQuietness *quietness, int after
) {
    FILE *file = fopen(proc_stat_path, "r");
    if (!file) return 0;
    int seen[MAX_CORE_LOGICAL_CPUS] = {0};
    char line[2048];
    while (fgets(line, sizeof(line), file)) {
        char label[32];
        if (sscanf(line, "%31s", label) != 1) continue;
        int cpu = -1;
        if (!cpu_label_id(label, &cpu)) continue;
        int index = -1;
        for (int candidate = 0; candidate < quietness->sibling_count; candidate++) {
            if (quietness->siblings[candidate].cpu == cpu) { index = candidate; break; }
        }
        if (index < 0) continue;
        unsigned long long user, nice, system, idle, iowait, irq, softirq, steal;
        int fields = sscanf(line, "%31s %llu %llu %llu %llu %llu %llu %llu %llu",
                            label, &user, &nice, &system, &idle, &iowait, &irq, &softirq, &steal);
        if (fields != 9 || seen[index]) { fclose(file); return 0; }
        uint64_t busy = 0, total = 0;
        if (!add_u64(&busy, (uint64_t)user) || !add_u64(&busy, (uint64_t)nice)
            || !add_u64(&busy, (uint64_t)system) || !add_u64(&busy, (uint64_t)irq)
            || !add_u64(&busy, (uint64_t)softirq) || !add_u64(&busy, (uint64_t)steal)) {
            fclose(file);
            return 0;
        }
        total = busy;
        if (!add_u64(&total, (uint64_t)idle) || !add_u64(&total, (uint64_t)iowait)) {
            fclose(file);
            return 0;
        }
        SiblingActivity *record = &quietness->siblings[index];
        if (after) { record->busy_after = busy; record->total_after = total; }
        else { record->busy_before = busy; record->total_before = total; }
        seen[index] = 1;
    }
    int read_error = ferror(file);
    fclose(file);
    if (read_error) return 0;
    for (int index = 0; index < quietness->sibling_count; index++) if (!seen[index]) return 0;
    return 1;
}

static int target_sibling_quietness_begin_at(
    SiblingQuietness *quietness, int target_cpu, const char *topology_root, const char *proc_stat_path
) {
    memset(quietness, 0, sizeof(*quietness));
    quietness->target_cpu = target_cpu;
    quietness->busy_fraction = -1;
    CpuList group = physical_core_group_at(topology_root, target_cpu);
    if (group.status != CPU_LIST_VALID) return 0;
    quietness->topology_verified = 1;
    for (int cpu = 0; cpu < TOPOLOGY_CPU_LIMIT; cpu++) {
        if (!cpu_list_contains(&group, cpu) || cpu == target_cpu) continue;
        if (quietness->sibling_count >= MAX_CORE_LOGICAL_CPUS) {
            quietness->topology_verified = 0;
            quietness->sibling_count = 0;
            return 0;
        }
        SiblingActivity *record = &quietness->siblings[quietness->sibling_count++];
        record->cpu = cpu;
        record->busy_fraction = -1;
    }
    if (!quietness->sibling_count) {
        quietness->activity_verified = 1;
        quietness->quietness_verified = 1;
        quietness->busy_fraction = 0;
        quietness->before_snapshot_ns = now_ns(CLOCK_MONOTONIC);
        return 1;
    }
    if (!read_sibling_activity_snapshot(proc_stat_path, quietness, 0)) return 0;
    quietness->before_snapshot_ns = now_ns(CLOCK_MONOTONIC);
    return 1;
}

static int target_sibling_quietness_end_at(
    SiblingQuietness *quietness, const char *proc_stat_path
) {
    if (!quietness->topology_verified) return 0;
    if (!quietness->sibling_count) {
        quietness->after_snapshot_ns = now_ns(CLOCK_MONOTONIC);
        return quietness->quietness_verified;
    }
    if (!quietness->before_snapshot_ns
        || !read_sibling_activity_snapshot(proc_stat_path, quietness, 1)) return 0;
    quietness->after_snapshot_ns = now_ns(CLOCK_MONOTONIC);
    double maximum = 0;
    for (int index = 0; index < quietness->sibling_count; index++) {
        SiblingActivity *record = &quietness->siblings[index];
        if (record->total_after < record->total_before || record->busy_after < record->busy_before) return 0;
        record->total_ticks = record->total_after - record->total_before;
        record->busy_ticks = record->busy_after - record->busy_before;
        if (!record->total_ticks || record->busy_ticks > record->total_ticks) return 0;
        record->busy_fraction = (double)record->busy_ticks / record->total_ticks;
        record->verified = 1;
        if (record->busy_fraction > maximum) maximum = record->busy_fraction;
    }
    quietness->activity_verified = 1;
    quietness->busy_fraction = maximum;
    quietness->quietness_verified = maximum <= TARGET_SIBLING_BUSY_FRACTION_LIMIT;
    return quietness->quietness_verified;
}

static int target_sibling_quietness_begin(SiblingQuietness *quietness, int target_cpu) {
    return target_sibling_quietness_begin_at(
        quietness, target_cpu, "/sys/devices/system/cpu", "/proc/stat");
}

static int target_sibling_quietness_end(SiblingQuietness *quietness) {
    return target_sibling_quietness_end_at(quietness, "/proc/stat");
}

static void print_target_sibling_quietness(const SiblingQuietness *quietness) {
    printf("\"target_sibling_activity_source\":\"proc_stat_cpu_ticks/v1\",\"target_sibling_cpus\":[");
    for (int index = 0; index < quietness->sibling_count; index++) {
        if (index) putchar(',');
        printf("%d", quietness->siblings[index].cpu);
    }
    printf("],\"target_sibling_activity\":[");
    for (int index = 0; index < quietness->sibling_count; index++) {
        const SiblingActivity *record = &quietness->siblings[index];
        if (index) putchar(',');
        printf("{\"cpu\":%d,\"busy_ticks\":%" PRIu64 ",\"total_ticks\":%" PRIu64 ",\"busy_fraction\":",
               record->cpu, record->busy_ticks, record->total_ticks);
        if (record->verified) printf("%.9f", record->busy_fraction); else printf("null");
        printf(",\"verified\":%s}", record->verified ? "true" : "false");
    }
    printf("],\"target_sibling_activity_verified\":%s,\"target_sibling_busy_fraction\":",
           quietness->activity_verified ? "true" : "false");
    if (quietness->activity_verified) printf("%.9f", quietness->busy_fraction); else printf("null");
    printf(",\"target_sibling_busy_threshold\":%.2f,\"target_sibling_quietness_verified\":%s,",
           TARGET_SIBLING_BUSY_FRACTION_LIMIT, quietness->quietness_verified ? "true" : "false");
    printf("\"target_sibling_snapshot_start_ns\":%" PRIu64 ",\"target_sibling_snapshot_end_ns\":%" PRIu64 ",",
           quietness->before_snapshot_ns, quietness->after_snapshot_ns);
}

static PORTABLE_UNUSED int independent_cores(const Options *options) {
    int cpus[MAX_WORKERS];
    int complete = 1;
    cpus[0] = options->target_cpu;
    for (int index = 1; index < options->active_tasks; index++) cpus[index] = options->helper_cpus[index - 1];
    for (int index = 0; index < options->active_tasks; index++) {
        for (int previous = 0; previous < index; previous++) {
            PhysicalCoreRelation relation = physical_core_relation(cpus[index], cpus[previous]);
            if (relation == PHYSICAL_CORE_SAME) return -1;
            if (relation == PHYSICAL_CORE_CONFLICT) return -2;
            if (relation == PHYSICAL_CORE_UNKNOWN) complete = 0;
        }
    }
    return complete;
}

static uint64_t mix(uint64_t value) {
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

#if defined(__GNUC__) || defined(__clang__)
__attribute__((noinline))
#endif
static uint64_t dispatch_payload(uint64_t seed, uint64_t task) {
    /* The direct batch and queued paths call this exact payload with the same
     * seed/task sequence.  Their difference therefore measures dispatch and
     * synchronization rather than useful work or a different core placement. */
    return mix(seed + task + UINT64_C(0x9e3779b97f4a7c15));
}

#if defined(__GNUC__) || defined(__clang__)
__attribute__((noinline))
#endif
static uint64_t visit(Node *node) {
    if (!node) return 0;
    return mix(node->value + node->attributes[0] + node->attributes[1] * 3 +
               node->attributes[2] * 5 + node->attributes[3] * 7 + visit(node->left)) ^ visit(node->right);
}

static uint64_t build_work(uint64_t units, uint64_t seed) {
    Node *nodes = malloc(63 * sizeof(Node));
    if (!nodes) { fputs("node allocation failed\n", stderr); exit(2); }
    for (uint64_t unit = 0; unit < units; unit++) {
        /* Fixed memory per invocation: set attributes, connect, traverse.
         * Contiguous nodes are a reproducible proxy, not ArkUI node internals. */
        for (unsigned index = 0; index < 63; index++) {
            nodes[index].value = mix(seed + index + unit);
            for (unsigned attr = 0; attr < 4; attr++) nodes[index].attributes[attr] = mix(nodes[index].value + attr);
            nodes[index].left = 2 * index + 1 < 63 ? &nodes[2 * index + 1] : NULL;
            nodes[index].right = 2 * index + 2 < 63 ? &nodes[2 * index + 2] : NULL;
        }
        seed ^= visit(nodes);
    }
    free(nodes);
    return seed;
}

static uint64_t residual_work(uint64_t units, uint64_t seed) {
    volatile uint64_t values[32];
    for (unsigned index = 0; index < 32; index++) values[index] = mix(seed + index);
    for (uint64_t unit = 0; unit < units; unit++) {
        for (unsigned index = 0; index < 64; index++) {
            seed = mix(seed + values[(seed >> 5) & 31] + unit + index);
            values[(seed >> 11) & 31] = seed;
        }
    }
    return seed;
}

static uint64_t work(const Options *options, uint64_t units, uint64_t seed) {
    return strcmp(options->family, "build") == 0 ? build_work(units, seed) : residual_work(units, seed);
}

/* Fixed portable source kernel for a measured compute ceiling.  Sixteen
 * independent accumulators and sixteen source lanes expose instruction-level
 * parallelism.  This is deliberately the same recurrence used by
 * roofline_fp_block: accumulator = accumulator * multiplier + source.  The
 * changing accumulator remains a multiply input, so a conforming optimizer
 * cannot hoist the multiply out of the measured loop.  Each update
 * is one source multiply plus one source add (32 algorithmic FLOPs/iteration).
 * A compiler may contract an update to FMA or vectorize it; the output is
 * therefore an algorithmic FLOP rate for this binary, not a retired-FP-op or
 * ISA theoretical-peak claim. */
static volatile double roofline_fixed_seed = 1.000001;

#if defined(__GNUC__) || defined(__clang__)
typedef double RooflineFp64x2 __attribute__((vector_size(16)));
#define ROOFLINE_EXPLICIT_FP64X2 1
#else
#define ROOFLINE_EXPLICIT_FP64X2 0
#endif

#if defined(__GNUC__) || defined(__clang__)
__attribute__((noinline))
#endif
static uint64_t roofline_compute(uint64_t units) {
    double seed = roofline_fixed_seed;
#if ROOFLINE_EXPLICIT_FP64X2
    RooflineFp64x2 v0 = {seed + .001, seed + .002};
    RooflineFp64x2 v1 = {seed + .003, seed + .004};
    RooflineFp64x2 v2 = {seed + .005, seed + .006};
    RooflineFp64x2 v3 = {seed + .007, seed + .008};
    RooflineFp64x2 v4 = {seed + .009, seed + .010};
    RooflineFp64x2 v5 = {seed + .011, seed + .012};
    RooflineFp64x2 v6 = {seed + .013, seed + .014};
    RooflineFp64x2 v7 = {seed + .015, seed + .016};
    const RooflineFp64x2 s0 = {seed + .017, seed + .018};
    const RooflineFp64x2 s1 = {seed + .019, seed + .020};
    const RooflineFp64x2 s2 = {seed + .021, seed + .022};
    const RooflineFp64x2 s3 = {seed + .023, seed + .024};
    const RooflineFp64x2 s4 = {seed + .025, seed + .026};
    const RooflineFp64x2 s5 = {seed + .027, seed + .028};
    const RooflineFp64x2 s6 = {seed + .029, seed + .030};
    const RooflineFp64x2 s7 = {seed + .031, seed + .032};
    const RooflineFp64x2 multiplier = {.9999993, .9999993};
    for (uint64_t unit = 0; unit < units; unit++) {
        v0 = v0 * multiplier + s0; v1 = v1 * multiplier + s1;
        v2 = v2 * multiplier + s2; v3 = v3 * multiplier + s3;
        v4 = v4 * multiplier + s4; v5 = v5 * multiplier + s5;
        v6 = v6 * multiplier + s6; v7 = v7 * multiplier + s7;
    }
    RooflineFp64x2 vectors[8] = {v0, v1, v2, v3, v4, v5, v6, v7};
    double lanes[16];
    memcpy(lanes, vectors, sizeof(lanes));
    double result = 0;
    for (int lane = 0; lane < 16; lane++) result += lanes[lane];
#else
    double values[16], sources[16];
    for (int lane = 0; lane < 16; lane++) {
        values[lane] = seed + .001 * (lane + 1);
        sources[lane] = seed + .001 * (lane + 17);
    }
    const double multiplier = .9999993;
    for (uint64_t unit = 0; unit < units; unit++)
        for (int lane = 0; lane < 16; lane++)
            values[lane] = values[lane] * multiplier + sources[lane];
    double result = 0;
    for (int lane = 0; lane < 16; lane++) result += values[lane];
#endif
    uint64_t bits = 0;
    memcpy(&bits, &result, sizeof(bits));
    return bits;
}

/* Make the completed output buffer observable without turning every element
 * into a volatile access.  The old v1 kernel used a volatile store and a
 * per-element checksum/branch; that serialized the loop and measured scalar
 * loop overhead rather than a sustainable source-stream roof. */
static void roofline_expose_output(void *output) {
#if defined(__GNUC__) || defined(__clang__)
    __asm__ __volatile__("" : : "r"(output) : "memory");
#else
    atomic_signal_fence(memory_order_seq_cst);
    (void)output;
#endif
}

static uint64_t roofline_output_checksum(
    const double *output, uint64_t begin, uint64_t length, uint64_t seed
) {
    if (!length) return seed;
    uint64_t positions[3] = {begin, begin + length / 2, begin + length - 1};
    for (int index = 0; index < 3; index++) {
        uint64_t bits = 0;
        memcpy(&bits, &output[positions[index]], sizeof(bits));
        seed ^= mix(bits + (uint64_t)index);
    }
    return seed;
}

/* Process sixteen independent elements per inner recurrence.  This gives both
 * constant and arbitrary even operation counts the same multi-chain ILP
 * structure; compilers may additionally combine lanes into SIMD.  Every lane
 * starts from one source element and keeps its intermediate value in a local
 * scalar/vector register, so the algorithmic minimum remains two source reads
 * plus one final source write (24 B/element), independent of FLOP count. */
static void roofline_fp_block(
    const double *restrict left, const double *restrict right,
    double *restrict output, uint64_t length, int operations
) {
    const double multiplier = .9999993;
    uint64_t index = 0;
    for (; index + ROOFLINE_ILP_LANES <= length; index += ROOFLINE_ILP_LANES) {
        double values[ROOFLINE_ILP_LANES], others[ROOFLINE_ILP_LANES];
        for (int lane = 0; lane < ROOFLINE_ILP_LANES; lane++) {
            values[lane] = left[index + (uint64_t)lane];
            others[lane] = right[index + (uint64_t)lane];
        }
        for (int operation = 0; operation < operations; operation += 2) {
            for (int lane = 0; lane < ROOFLINE_ILP_LANES; lane++)
                values[lane] = values[lane] * multiplier + others[lane];
        }
        for (int lane = 0; lane < ROOFLINE_ILP_LANES; lane++)
            output[index + (uint64_t)lane] = values[lane];
    }
    for (; index < length; index++) {
        double value = left[index], other = right[index];
        for (int operation = 0; operation < operations; operation += 2)
            value = value * multiplier + other;
        output[index] = value;
    }
}

static uint64_t roofline_memory_work(Worker *worker, uint64_t units, uint64_t seed) {
    const Options *options = &worker->shared->options;
    uint64_t count = worker->roofline_element_count;
    uint64_t index = worker->memory_index;
    if (!strcmp(options->workload_class, "instruction_proxy")) {
        uint64_t *data = worker->roofline_memory;
        uint64_t *left = data, *right = data + count;
        volatile uint64_t *output = data + 2 * count;
        for (uint64_t unit = 0; unit < units; unit++) {
            uint64_t value = left[index] + right[index];
            for (int round = 0; round < options->roofline_operations_per_element; round++)
                value = mix(value + (uint64_t)round);
            output[index] = value;
            seed ^= value;
            if (++index == count) index = 0;
        }
    } else {
        double *data = worker->roofline_memory;
        const double *restrict left = data, *restrict right = data + count;
        double *restrict output = data + 2 * count;
        int operations = !strcmp(options->workload_class, "bandwidth_ceiling")
                         ? 2 : options->roofline_operations_per_element;
        uint64_t remaining = units;
        while (remaining) {
            uint64_t available = count - index;
            uint64_t block = remaining < available ? remaining : available;
            roofline_fp_block(left + index, right + index, output + index,
                              block, operations);
            roofline_expose_output(output + index);
            seed = roofline_output_checksum(output, index, block, seed);
            index += block;
            if (index == count) index = 0;
            remaining -= block;
        }
    }
    worker->memory_index = index;
    return seed;
}

static uint64_t roofline_work(Worker *worker, uint64_t units, uint64_t seed) {
    if (!strcmp(worker->shared->options.workload_class, "compute_ceiling"))
        return seed ^ roofline_compute(units);
    return roofline_memory_work(worker, units, seed);
}

static uint64_t worker_work(Worker *worker, uint64_t units, uint64_t seed) {
    const Options *options = &worker->shared->options;
    if (!strcmp(options->benchmark, "roofline")) return roofline_work(worker, units, seed);
    if (strcmp(options->benchmark, "pair_work")) return work(options, units, seed);
    const char *kind = worker->target ? options->workload_class : options->peer_workload_class;
    uint64_t count = options->working_set_bytes / sizeof(uint64_t);
    for (uint64_t unit = 0; unit < units; unit++) {
        if (strcmp(kind, "memory")) {
            for (unsigned index = 0; index < 64; index++) seed = mix(seed + unit + index);
        }
        if (strcmp(kind, "compute")) {
            /* Contiguous blocks, one wrap check per block. Four independent
             * sums keep integer division / a single dependency chain from
             * defining the memory proxy's throughput. */
            if (worker->memory_index + 1024 > count) worker->memory_index = 0;
            volatile uint64_t *block = worker->memory + worker->memory_index;
            uint64_t sum0 = 0, sum1 = 0, sum2 = 0, sum3 = 0;
            for (unsigned index = 0; index < 1024; index += 4) {
                uint64_t a = block[index], b = block[index + 1], c = block[index + 2], d = block[index + 3];
                block[index] = a + unit; block[index + 1] = b + unit;
                block[index + 2] = c + unit; block[index + 3] = d + unit;
                sum0 += a; sum1 += b; sum2 += c; sum3 += d;
            }
            worker->memory_index += 1024;
            seed ^= sum0 + sum1 + sum2 + sum3;
        }
    }
    return seed;
}

static void signal_ready(Shared *shared) {
    pthread_mutex_lock(&shared->mutex);
    shared->ready++;
    pthread_cond_broadcast(&shared->condition);
    while (!shared->start) pthread_cond_wait(&shared->condition, &shared->mutex);
    pthread_mutex_unlock(&shared->mutex);
}

static void *worker_main(void *raw) {
    Worker *worker = raw;
    Shared *shared = worker->shared;
    Options *options = &shared->options;
    worker->affinity_verified = pin_cpu(worker->cpu);
    worker->actual_cpu = actual_cpu();
    uint64_t seed = UINT64_C(0x12345678);
    const char *kind = worker->target ? options->workload_class : options->peer_workload_class;
    if (!strcmp(options->benchmark, "pair_work") && strcmp(kind, "compute")) {
        worker->memory = malloc(options->working_set_bytes);
        if (!worker->memory) { fputs("working-set allocation failed\n", stderr); exit(2); }
        for (uint64_t index = 0; index < options->working_set_bytes / sizeof(uint64_t); index++) worker->memory[index] = mix(index);
    }
    /* Exercise a bounded workload chunk, then sample the already-frozen
     * per-CPU frequency source.  The target must accumulate three consecutive
     * readings inside the fixed +/-3% band before its measured window starts.
     * A fixed 12-sample ceiling keeps an unreachable OPP from hanging a run;
     * the run is still emitted, with warmup/frequency verification false. */
    uint64_t warmup_units = options->work_units < WARMUP_CHUNK_MAX_UNITS
        ? options->work_units : WARMUP_CHUNK_MAX_UNITS;
    if (!strcmp(options->benchmark, "roofline")) {
        if (strcmp(options->workload_class, "compute_ceiling")) {
            uint64_t bytes = options->working_set_bytes - options->working_set_bytes % (3 * sizeof(uint64_t));
            worker->roofline_element_count = bytes / (3 * sizeof(uint64_t));
            if (posix_memalign(&worker->roofline_memory, 64, bytes) != 0 || !worker->roofline_memory) {
                fputs("roofline working-set allocation failed\n", stderr); exit(2);
            }
            if (!strcmp(options->workload_class, "instruction_proxy")) {
                uint64_t *data = worker->roofline_memory;
                for (uint64_t index = 0; index < bytes / sizeof(uint64_t); index++) data[index] = mix(index + 1);
            } else {
                double *data = worker->roofline_memory;
                for (uint64_t index = 0; index < bytes / sizeof(double); index++)
                    data[index] = .5 + (double)((index % 1024) + 1) / 4096.0;
            }
        }
    }
    uint64_t warmup_chunk_units = !strcmp(options->benchmark, "empty_dispatch") ? 32 : warmup_units;
    warmup_frequency_init(&worker->warmup_frequency);
    int warmup_rounds = worker->target ? WARMUP_FREQUENCY_MAX_SAMPLES : 1;
    for (int round = 0; round < warmup_rounds; round++) {
        if (strcmp(options->benchmark, "empty_dispatch"))
            seed = worker_work(worker, warmup_chunk_units, seed);
        else for (uint64_t task = 0; task < warmup_chunk_units; task++)
            seed = dispatch_payload(seed, task);
        worker->warmup_units += warmup_chunk_units;
        if (!worker->target) break;
        warmup_frequency_observe(
            &worker->warmup_frequency,
            read_frozen_frequency(&shared->target_frequency_source),
            options->frequency_khz);
        if (worker->warmup_frequency.verified) break;
    }
    if (worker->target && strcmp(options->benchmark, "empty_dispatch")) pmu_prepare(&worker->pmu);
    signal_ready(shared);
    if (!worker->target) {
        worker->start_ns = now_ns(CLOCK_MONOTONIC);
        uint64_t start_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
        atomic_fetch_add(&shared->helpers_active, 1);
        while (!atomic_load(&shared->stop)) {
            seed = worker_work(worker, 32, seed);
            worker->completed_units += 32;
        }
        worker->cpu_ms = (now_ns(CLOCK_THREAD_CPUTIME_ID) - start_cpu) / 1e6;
        worker->end_ns = now_ns(CLOCK_MONOTONIC);
    } else {
        while (atomic_load(&shared->helpers_active) != options->active_tasks - 1 || !atomic_load(&shared->sampler_ready)) sched_yield();
        if (strcmp(options->benchmark, "empty_dispatch") == 0) {
            pthread_mutex_lock(&shared->queue_mutex);
            shared->dispatch_ready = 1;
            pthread_cond_broadcast(&shared->queue_condition);
            while (!shared->direct_start) pthread_cond_wait(&shared->queue_condition, &shared->queue_mutex);
            pthread_mutex_unlock(&shared->queue_mutex);

            uint64_t direct_seed = UINT64_C(0x12345678);
            target_sibling_quietness_begin(&worker->sibling_quietness, worker->cpu);
            worker->start_ns = now_ns(CLOCK_MONOTONIC);
            uint64_t start_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
            for (uint64_t task = 0; task < options->work_units; task++)
                direct_seed = dispatch_payload(direct_seed, task);
            worker->direct_cpu_ms = (now_ns(CLOCK_THREAD_CPUTIME_ID) - start_cpu) / 1e6;
            worker->end_ns = now_ns(CLOCK_MONOTONIC);
            worker->direct_wall_ms = (worker->end_ns - worker->start_ns) / 1e6;
            worker->direct_sink = direct_seed;

            pthread_mutex_lock(&shared->queue_mutex);
            shared->direct_completed = 1;
            pthread_cond_broadcast(&shared->queue_condition);
            while (!shared->queue_start) pthread_cond_wait(&shared->queue_condition, &shared->queue_mutex);
            pthread_mutex_unlock(&shared->queue_mutex);

            uint64_t queue_seed = UINT64_C(0x12345678);
            worker->start_ns = now_ns(CLOCK_MONOTONIC);
            start_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
            for (uint64_t task = 0; task < options->work_units; task++) {
                pthread_mutex_lock(&shared->queue_mutex);
                while (!shared->pending) pthread_cond_wait(&shared->queue_condition, &shared->queue_mutex);
                queue_seed = dispatch_payload(queue_seed, shared->pending_task);
                shared->pending = 0;
                shared->completed++;
                pthread_cond_broadcast(&shared->queue_condition);
                pthread_mutex_unlock(&shared->queue_mutex);
            }
            worker->queue_cpu_ms = (now_ns(CLOCK_THREAD_CPUTIME_ID) - start_cpu) / 1e6;
            worker->end_ns = now_ns(CLOCK_MONOTONIC);
            worker->queue_wall_ms = (worker->end_ns - worker->start_ns) / 1e6;
            worker->queue_sink = queue_seed;
            target_sibling_quietness_end(&worker->sibling_quietness);
            /* Preserve the v1-v3 fields for older fitters. */
            worker->cpu_ms = worker->queue_cpu_ms;
            worker->wall_ms = worker->queue_wall_ms;
            worker->sink = queue_seed;
        } else {
            target_sibling_quietness_begin(&worker->sibling_quietness, worker->cpu);
            /* The perf group deliberately encloses the timed window.  Capture
             * both end clocks before disabling/reading/closing perf so those
             * control-plane costs never inflate wall_ms or cpu_ms. */
            pmu_begin(&worker->pmu);
            worker->start_ns = now_ns(CLOCK_MONOTONIC);
            uint64_t start_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
            seed = worker_work(worker, options->work_units, seed);
            uint64_t end_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
            worker->end_ns = now_ns(CLOCK_MONOTONIC);
            pmu_end(&worker->pmu);
            worker->cpu_ms = (end_cpu - start_cpu) / 1e6;
            worker->wall_ms = (worker->end_ns - worker->start_ns) / 1e6;
            worker->sink = seed;
            target_sibling_quietness_end(&worker->sibling_quietness);
        }
        worker->completed_units = options->work_units;
        atomic_store(&shared->stop, 1);
    }
    if (!worker->target) {
        worker->wall_ms = (worker->end_ns - worker->start_ns) / 1e6;
        worker->sink = seed;
    }
    free((void *)worker->memory);
    free(worker->roofline_memory);
    return NULL;
}

static void frequency_sample(Sampler *sampler) {
    long value = read_frozen_frequency(&sampler->shared->target_frequency_source);
    uint64_t timestamp = now_ns(CLOCK_MONOTONIC);
    if (value > 0) {
        if (!sampler->samples) { sampler->minimum = value; sampler->maximum = value; sampler->first_sample_ns = timestamp; }
        if (value < sampler->minimum) sampler->minimum = value;
        if (value > sampler->maximum) sampler->maximum = value;
        sampler->last_sample_ns = timestamp;
        sampler->sum += value;
        sampler->samples++;
    } else {
        sampler->read_failures++;
    }
    if (!strcmp(sampler->shared->options.benchmark, "pair_work") && sampler->shared->options.active_tasks == 2) {
        long peer = read_frozen_frequency(&sampler->shared->peer_frequency_source);
        if (peer > 0) {
            if (!sampler->peer_samples) { sampler->peer_minimum = peer; sampler->peer_maximum = peer; }
            if (peer < sampler->peer_minimum) sampler->peer_minimum = peer;
            if (peer > sampler->peer_maximum) sampler->peer_maximum = peer;
            sampler->peer_sum += peer;
            sampler->peer_samples++;
        } else sampler->peer_read_failures++;
    }
}

static void *sampler_main(void *raw) {
    Sampler *sampler = raw;
    Shared *shared = sampler->shared;
    sampler->affinity_verified = pin_cpu(shared->options.observer_cpu);
    sampler->actual_cpu = actual_cpu();
    signal_ready(shared);
    frequency_sample(sampler);
    atomic_store(&shared->sampler_ready, 1);
    struct timespec period = {.tv_sec = shared->options.sample_period_us / 1000000,
                              .tv_nsec = (shared->options.sample_period_us % 1000000) * 1000L};
    while (!atomic_load(&shared->stop)) {
        nanosleep(&period, NULL);
        frequency_sample(sampler);
    }
    return NULL;
}

static void usage(void) {
    fputs("benchmark --benchmark cpu_work|empty_dispatch|pair_work|roofline --family build|residual|dispatch|roofline --cpu N --observer-cpu N --frequency-khz N --work-units N [--frequency-policy-id policyN|cpuN] [--active-tasks N --helper-cpus 4,6,8 --sample-period-us 1000 --frequency-tolerance 0.03] [--workload-class compute|memory|mixed|compute_ceiling|bandwidth_ceiling|intensity_ladder|instruction_proxy --peer-workload-class compute|memory|mixed|none --peer-frequency-khz N --peer-frequency-policy-id policyN|cpuN --working-set-bytes N --roofline-operations-per-element N]\n", stderr);
}

static long parse_long(const char *value, long minimum, long maximum) {
    char *end = NULL;
    errno = 0;
    long result = strtol(value, &end, 10);
    if (errno || !end || *end || result < minimum || result > maximum) { usage(); exit(2); }
    return result;
}

#if !defined(PARALLEL_CALIBRATION_TOPOLOGY_TEST) && \
    !defined(PARALLEL_CALIBRATION_SIBLING_QUIETNESS_TEST) && \
    !defined(PARALLEL_CALIBRATION_FREQUENCY_TEST)
int main(int argc, char **argv) {
    char default_frequency_policy_id[16] = {0}, default_peer_frequency_policy_id[16] = {0};
    Options options = {.benchmark = "cpu_work", .family = "build", .target_cpu = -1, .observer_cpu = -1,
                       .active_tasks = 1, .sample_period_us = 1000, .work_units = 10000,
                       .frequency_tolerance = .03, .workload_class = "compute", .peer_workload_class = "none", .working_set_bytes = 64 * 1024 * 1024,
                       .roofline_operations_per_element = ROOFLINE_COMPUTE_FLOPS_PER_UNIT};
    for (int index = 1; index < argc; index += 2) {
        if (index + 1 >= argc) { usage(); return 2; }
        const char *key = argv[index], *value = argv[index + 1];
        if (!strcmp(key, "--benchmark")) options.benchmark = value;
        else if (!strcmp(key, "--family")) options.family = value;
        else if (!strcmp(key, "--workload-class")) options.workload_class = value;
        else if (!strcmp(key, "--peer-workload-class")) options.peer_workload_class = value;
        else if (!strcmp(key, "--frequency-policy-id")) options.frequency_policy_id = value;
        else if (!strcmp(key, "--peer-frequency-policy-id")) options.peer_frequency_policy_id = value;
        else if (!strcmp(key, "--working-set-bytes")) options.working_set_bytes = (uint64_t)parse_long(value, 1024 * 1024, 512 * 1024 * 1024);
        else if (!strcmp(key, "--roofline-operations-per-element")) options.roofline_operations_per_element = (int)parse_long(value, 2, 4096);
        else if (!strcmp(key, "--peer-frequency-khz")) options.peer_frequency_khz = parse_long(value, 0, INT_MAX);
        else if (!strcmp(key, "--cpu")) options.target_cpu = (int)parse_long(value, 0, 4095);
        else if (!strcmp(key, "--observer-cpu")) options.observer_cpu = (int)parse_long(value, 0, 4095);
        else if (!strcmp(key, "--frequency-khz")) options.frequency_khz = parse_long(value, 1, INT_MAX);
        else if (!strcmp(key, "--work-units")) options.work_units = (uint64_t)parse_long(value, 1, 2000000000);
        else if (!strcmp(key, "--active-tasks")) options.active_tasks = (int)parse_long(value, 1, MAX_WORKERS);
        else if (!strcmp(key, "--sample-period-us")) options.sample_period_us = (int)parse_long(value, 50, 1000000);
        else if (!strcmp(key, "--frequency-tolerance")) {
            char *end = NULL;
            options.frequency_tolerance = strtod(value, &end);
            if (!end || *end || !isfinite(options.frequency_tolerance) || options.frequency_tolerance < 0 || options.frequency_tolerance > .2) return 2;
        } else if (!strcmp(key, "--helper-cpus")) {
            char *copy = strdup(value), *save = NULL;
            if (!copy) return 2;
            for (char *part = strtok_r(copy, ",", &save); part; part = strtok_r(NULL, ",", &save)) {
                if (options.helper_count >= MAX_WORKERS - 1) { free(copy); return 2; }
                options.helper_cpus[options.helper_count++] = (int)parse_long(part, 0, 4095);
            }
            free(copy);
        } else { usage(); return 2; }
    }
    /* Preserve the standalone collector contract. Desktop passes the exact
     * verified policy explicitly; older/offline callers safely fall back to
     * the target CPU's per-CPU cpufreq view. */
    if (!options.frequency_policy_id && options.target_cpu >= 0) {
        snprintf(default_frequency_policy_id, sizeof(default_frequency_policy_id), "cpu%d", options.target_cpu);
        options.frequency_policy_id = default_frequency_policy_id;
    }
    if (!options.peer_frequency_policy_id && options.active_tasks == 2 && options.helper_count > 0) {
        snprintf(default_peer_frequency_policy_id, sizeof(default_peer_frequency_policy_id), "cpu%d", options.helper_cpus[0]);
        options.peer_frequency_policy_id = default_peer_frequency_policy_id;
    }
    if (options.target_cpu < 0 || options.observer_cpu < 0 || options.frequency_khz <= 0 ||
        !valid_frequency_policy_id(options.frequency_policy_id) ||
        (strcmp(options.benchmark, "cpu_work") && strcmp(options.benchmark, "empty_dispatch") && strcmp(options.benchmark, "pair_work") && strcmp(options.benchmark, "roofline")) ||
        (strcmp(options.family, "build") && strcmp(options.family, "residual") && strcmp(options.family, "dispatch") && strcmp(options.family, "roofline")) ||
        options.helper_count != options.active_tasks - 1 ||
        (!strcmp(options.benchmark, "empty_dispatch") && options.active_tasks != 1)) { usage(); return 2; }
    if (!strcmp(options.benchmark, "pair_work") &&
        ((strcmp(options.workload_class, "compute") && strcmp(options.workload_class, "memory") && strcmp(options.workload_class, "mixed")) ||
         (options.active_tasks != 1 && options.active_tasks != 2) || options.working_set_bytes % 8192 ||
         (options.active_tasks == 1 && strcmp(options.peer_workload_class, "none")) ||
         (options.active_tasks == 2 && ((strcmp(options.peer_workload_class, "compute") && strcmp(options.peer_workload_class, "memory") && strcmp(options.peer_workload_class, "mixed")) || options.peer_frequency_khz <= 0 || !valid_frequency_policy_id(options.peer_frequency_policy_id))))) { usage(); return 2; }
    if (!strcmp(options.benchmark, "roofline") &&
        ((strcmp(options.workload_class, "compute_ceiling") && strcmp(options.workload_class, "bandwidth_ceiling") &&
          strcmp(options.workload_class, "intensity_ladder") && strcmp(options.workload_class, "instruction_proxy")) ||
         strcmp(options.family, "roofline") || options.active_tasks != 1 ||
         options.roofline_operations_per_element % 2 ||
         (!strcmp(options.workload_class, "compute_ceiling") && options.roofline_operations_per_element != ROOFLINE_COMPUTE_FLOPS_PER_UNIT))) { usage(); return 2; }
    int cpus[MAX_WORKERS]; cpus[0] = options.target_cpu;
    for (int index = 1; index < options.active_tasks; index++) cpus[index] = options.helper_cpus[index - 1];
    for (int index = 0; index < options.active_tasks; index++) {
        if (cpus[index] == options.observer_cpu) { fputs("Observer must use another CPU\n", stderr); return 2; }
        for (int previous = 0; previous < index; previous++) if (cpus[index] == cpus[previous]) { fputs("Duplicate worker CPU\n", stderr); return 2; }
    }
    /* The portable smoke-test build exercises workload accounting on hosts
     * without Linux CPU topology (for example macOS CI).  It never claims
     * placement evidence and is not used for the managed target binary. */
#ifdef PARALLEL_CALIBRATION_PORTABLE_SMOKE_TEST
    int topology_status = 0;
#else
    int topology_status = independent_cores(&options);
    if (topology_status == -1) {
        fputs("Target/helpers share a physical core; choose independent physical-core representatives\n", stderr);
        return 2;
    }
    if (topology_status == -2) {
        fputs("Conflicting or incomplete CPU sibling topology; refusing unverified physical-core placement\n", stderr);
        return 2;
    }
#endif
    int observer_topology_status = 1;
#ifdef PARALLEL_CALIBRATION_PORTABLE_SMOKE_TEST
    observer_topology_status = 0;
#else
    for (int index = 0; index < options.active_tasks; index++) {
        PhysicalCoreRelation relation = physical_core_relation(options.observer_cpu, cpus[index]);
        if (relation == PHYSICAL_CORE_SAME) {
            fputs("Observer shares a physical core with a worker; choose an independent observer\n", stderr);
            return 2;
        }
        if (relation == PHYSICAL_CORE_CONFLICT) {
            fputs("Conflicting or incomplete observer CPU sibling topology; refusing unverified placement\n", stderr);
            return 2;
        }
        if (relation == PHYSICAL_CORE_UNKNOWN) observer_topology_status = 0;
    }
#endif
    Shared shared = {.mutex = PTHREAD_MUTEX_INITIALIZER, .condition = PTHREAD_COND_INITIALIZER,
                     .queue_mutex = PTHREAD_MUTEX_INITIALIZER, .queue_condition = PTHREAD_COND_INITIALIZER,
                     .options = options};
    freeze_frequency_source(
        &shared.target_frequency_source, options.target_cpu, options.frequency_policy_id);
    if (!strcmp(options.benchmark, "pair_work") && options.active_tasks == 2)
        freeze_frequency_source(
            &shared.peer_frequency_source, cpus[1], options.peer_frequency_policy_id);
    atomic_init(&shared.stop, 0); atomic_init(&shared.helpers_active, 0); atomic_init(&shared.sampler_ready, 0);
    Worker workers[MAX_WORKERS] = {0}; pthread_t threads[MAX_WORKERS], sampler_thread;
    Sampler sampler = {.shared = &shared};
    int controller_affinity = pin_cpu(options.observer_cpu);
    for (int index = 0; index < options.active_tasks; index++) {
        workers[index].shared = &shared; workers[index].target = index == 0; workers[index].cpu = cpus[index];
        if (pthread_create(&threads[index], NULL, worker_main, &workers[index])) { perror("pthread_create"); return 2; }
    }
    if (pthread_create(&sampler_thread, NULL, sampler_main, &sampler)) { perror("pthread_create"); return 2; }
    pthread_mutex_lock(&shared.mutex);
    while (shared.ready != options.active_tasks + 1) pthread_cond_wait(&shared.condition, &shared.mutex);
    shared.start = 1; pthread_cond_broadcast(&shared.condition); pthread_mutex_unlock(&shared.mutex);
    if (!strcmp(options.benchmark, "empty_dispatch")) {
        pthread_mutex_lock(&shared.queue_mutex);
        while (!shared.dispatch_ready) pthread_cond_wait(&shared.queue_condition, &shared.queue_mutex);
        uint64_t direct_wall_start = now_ns(CLOCK_MONOTONIC);
        uint64_t direct_cpu_start = now_ns(CLOCK_THREAD_CPUTIME_ID);
        shared.direct_start = 1;
        pthread_cond_broadcast(&shared.queue_condition);
        while (!shared.direct_completed) pthread_cond_wait(&shared.queue_condition, &shared.queue_mutex);
        shared.direct_producer_cpu_ms = (now_ns(CLOCK_THREAD_CPUTIME_ID) - direct_cpu_start) / 1e6;
        shared.direct_roundtrip_wall_ms = (now_ns(CLOCK_MONOTONIC) - direct_wall_start) / 1e6;

        uint64_t queue_wall_start = now_ns(CLOCK_MONOTONIC);
        uint64_t queue_cpu_start = now_ns(CLOCK_THREAD_CPUTIME_ID);
        shared.queue_start = 1;
        pthread_cond_broadcast(&shared.queue_condition);
        pthread_mutex_unlock(&shared.queue_mutex);
        for (uint64_t task = 0; task < options.work_units; task++) {
            pthread_mutex_lock(&shared.queue_mutex);
            shared.pending_task = task;
            shared.pending = 1; pthread_cond_broadcast(&shared.queue_condition);
            while (shared.completed <= task) pthread_cond_wait(&shared.queue_condition, &shared.queue_mutex);
            pthread_mutex_unlock(&shared.queue_mutex);
        }
        shared.queue_producer_cpu_ms = (now_ns(CLOCK_THREAD_CPUTIME_ID) - queue_cpu_start) / 1e6;
        shared.queue_roundtrip_wall_ms = (now_ns(CLOCK_MONOTONIC) - queue_wall_start) / 1e6;
    }
    for (int index = 0; index < options.active_tasks; index++) pthread_join(threads[index], NULL);
    pthread_join(sampler_thread, NULL);
    int affinity = controller_affinity && sampler.affinity_verified;
    for (int index = 0; index < options.active_tasks; index++) affinity = affinity && workers[index].affinity_verified;
    int topology_verified = topology_status == 1;
    int observer_independent = observer_topology_status == 1;
    int frequency_verified = frequency_window_verified(
        &shared.target_frequency_source, &workers[0].warmup_frequency, 1,
        sampler.samples, sampler.read_failures, sampler.minimum, sampler.maximum,
        options.frequency_khz, options.frequency_tolerance);
    /* A live helper thread is not necessarily running. A conservative lower
     * bound subtracts ALL wall time outside the target interval from its total
     * thread CPU, even if the helper was actually preempted in that outside time.
     * This prevents pre/post-target work from concealing insufficient co-run. */
    const double concurrency_threshold = .9;
    workers[0].cpu_busy_fraction = workers[0].wall_ms > 0 ? workers[0].cpu_ms / workers[0].wall_ms : 0;
    double concurrency_min_busy = workers[0].cpu_busy_fraction;
    int concurrency_verified = strcmp(options.benchmark, "empty_dispatch") && workers[0].cpu_busy_fraction >= concurrency_threshold;
    for (int index = 1; index < options.active_tasks; index++) {
        Worker *helper = &workers[index];
        helper->covers_target_interval = helper->start_ns <= workers[0].start_ns && helper->end_ns >= workers[0].end_ns;
        helper->cpu_busy_fraction = helper->wall_ms > 0 ? helper->cpu_ms / helper->wall_ms : 0;
        uint64_t before_ns = helper->start_ns < workers[0].start_ns ? workers[0].start_ns - helper->start_ns : 0;
        uint64_t after_ns = helper->end_ns > workers[0].end_ns ? helper->end_ns - workers[0].end_ns : 0;
        double lower = helper->cpu_ms - (before_ns + after_ns) / 1e6;
        helper->overlap_cpu_lower_bound_ms = lower > 0 ? lower : 0;
        helper->overlap_cpu_lower_bound_fraction = workers[0].wall_ms > 0 ? helper->overlap_cpu_lower_bound_ms / workers[0].wall_ms : 0;
        if (helper->overlap_cpu_lower_bound_fraction < concurrency_min_busy) concurrency_min_busy = helper->overlap_cpu_lower_bound_fraction;
        if (!helper->covers_target_interval || helper->cpu_busy_fraction < concurrency_threshold ||
            helper->overlap_cpu_lower_bound_fraction < concurrency_threshold) concurrency_verified = 0;
    }
    printf("{\"schema_version\":\"parallel_calibration_measurement/v1\",\"workload_version\":\"%s\",", WORKLOAD_VERSION);
    printf("\"benchmark\":\"%s\",\"family\":\"%s\",\"cpu\":%d,\"observer_cpu\":%d,\"active_tasks\":%d,\"work_units\":%" PRIu64 ",", options.benchmark, options.family, options.target_cpu, options.observer_cpu, options.active_tasks, options.work_units);
    if (!strcmp(options.benchmark, "pair_work")) {
        int has_peer = options.active_tasks == 2;
        int verified = has_peer && frequency_window_verified(
            &shared.peer_frequency_source, NULL, 0,
            sampler.peer_samples, sampler.peer_read_failures,
            sampler.peer_minimum, sampler.peer_maximum,
            options.peer_frequency_khz, options.frequency_tolerance);
        printf("\"workload_class\":\"%s\",\"peer_workload_class\":\"%s\",\"working_set_bytes\":%" PRIu64 ",\"peer_frequency_khz\":%ld,\"peer_frequency_policy_id\":\"%s\",", options.workload_class, options.peer_workload_class, options.working_set_bytes, options.peer_frequency_khz, options.peer_frequency_policy_id ? options.peer_frequency_policy_id : "");
        printf("\"peer_frequency_source\":\"%s\",\"peer_frequency_source_cpu\":%d,\"peer_frequency_source_path\":\"%s\",",
               has_peer ? shared.peer_frequency_source.kind : "",
               has_peer ? shared.peer_frequency_source.cpu : -1,
               has_peer ? shared.peer_frequency_source.path : "");
        printf("\"peer_frequency_verified\":%s,\"peer_frequency_sample_count\":%" PRIu64 ",\"peer_frequency_read_failures\":%" PRIu64 ",",
               verified ? "true" : "false", sampler.peer_samples, sampler.peer_read_failures);
        if (sampler.peer_samples)
            printf("\"peer_observed_frequency_khz\":%.3Lf,\"peer_frequency_min_khz\":%ld,\"peer_frequency_max_khz\":%ld,", sampler.peer_sum / sampler.peer_samples, sampler.peer_minimum, sampler.peer_maximum);
        else
            printf("\"peer_observed_frequency_khz\":null,\"peer_frequency_min_khz\":null,\"peer_frequency_max_khz\":null,");
    }
    if (!strcmp(options.benchmark, "roofline")) {
        uint64_t elements = workers[0].roofline_element_count;
        uint64_t source_bytes = strcmp(options.workload_class, "compute_ceiling")
                                ? options.work_units * UINT64_C(24) : 0;
        uint64_t source_flops = 0, integer_rounds = 0;
        int operations = options.roofline_operations_per_element;
        const char *basis = "algorithmic_flop";
        if (!strcmp(options.workload_class, "compute_ceiling")) {
            source_flops = options.work_units * ROOFLINE_COMPUTE_FLOPS_PER_UNIT;
        } else if (!strcmp(options.workload_class, "bandwidth_ceiling")) {
            operations = 2;
            source_flops = options.work_units * UINT64_C(2);
            basis = "minimum_source_bytes";
        } else if (!strcmp(options.workload_class, "intensity_ladder")) {
            source_flops = options.work_units * (uint64_t)operations;
            basis = "algorithmic_flop_per_minimum_source_byte";
        } else {
            integer_rounds = options.work_units * (uint64_t)operations;
            basis = "integer_mix_round_proxy";
        }
        const char *kernel_execution = !strcmp(options.workload_class, "compute_ceiling")
            ? "fp64_16_independent_recurrences"
            : !strcmp(options.workload_class, "instruction_proxy")
            ? "integer_mix_proxy"
            : "blocked_fp64_16way_ilp";
        int kernel_lanes = !strcmp(options.workload_class, "compute_ceiling")
            ? 16 : !strcmp(options.workload_class, "instruction_proxy") ? 1 : ROOFLINE_ILP_LANES;
        printf("\"workload_class\":\"%s\",\"metric_basis\":\"%s\",", options.workload_class, basis);
        printf("\"roofline_kernel_version\":\"%s\",", ROOFLINE_KERNEL_VERSION);
        printf("\"roofline_kernel_execution\":\"%s\",\"roofline_ilp_lanes\":%d,", kernel_execution, kernel_lanes);
        printf("\"roofline_generic_even_operations_supported\":true,\"roofline_quality_gate_required\":true,");
        printf("\"roofline_operations_per_element\":%d,\"working_set_bytes\":%" PRIu64 ",", operations, options.working_set_bytes);
        printf("\"working_set_bytes_effective\":%" PRIu64 ",", elements * UINT64_C(24));
        printf("\"working_set_element_count\":%" PRIu64 ",\"algorithmic_flops\":", elements);
        if (source_flops) printf("%" PRIu64, source_flops); else printf("null");
        printf(",\"algorithmic_bytes_minimum\":");
        if (source_bytes) printf("%" PRIu64, source_bytes); else printf("null");
        printf(",\"integer_mix_rounds\":");
        if (integer_rounds) printf("%" PRIu64, integer_rounds); else printf("null");
        printf(",\"operational_intensity_flop_per_byte\":");
        if (source_flops && source_bytes) printf("%.12f", (double)source_flops / source_bytes); else printf("null");
        printf(",\"attainable_gflops\":");
        if (source_flops && workers[0].wall_ms > 0) printf("%.12f", source_flops / (workers[0].wall_ms * 1e6)); else printf("null");
        printf(",\"source_level_gbytes_per_s\":");
        if (source_bytes && workers[0].wall_ms > 0) printf("%.12f", source_bytes / (workers[0].wall_ms * 1e6)); else printf("null");
        printf(",\"full_working_set_passes\":");
        if (elements) printf("%.9f", (double)options.work_units / elements); else printf("null");
        printf(",\"working_set_coverage_verified\":%s,", !elements || options.work_units >= elements ? "true" : "false");
        printf("\"count_semantics\":\"FLOPs are source-level operations; bytes count two source reads plus one source write, exclude sparse checksum reads and cache/write-allocate traffic, and are not measured DRAM traffic; integer mix rounds are not retired instructions\",");
    }
    printf("\"cpu_ms\":%.9f,\"wall_ms\":%.9f,\"thread_cpu_clock\":\"CLOCK_THREAD_CPUTIME_ID\",\"target_start_ns\":%" PRIu64 ",\"target_end_ns\":%" PRIu64 ",", workers[0].cpu_ms, workers[0].wall_ms, workers[0].start_ns, workers[0].end_ns);
    print_pmu_measurement(&workers[0].pmu);
    printf("\"affinity_verified\":%s,\"physical_cores_verified\":%s,\"physical_core_placement_verified\":%s,\"observer_independent_core\":%s,", affinity ? "true" : "false", topology_verified ? "true" : "false", topology_verified ? "true" : "false", observer_independent ? "true" : "false");
    print_target_sibling_quietness(&workers[0].sibling_quietness);
    printf("\"concurrency_verified\":%s,\"target_cpu_busy_fraction\":%.9f,\"concurrency_threshold\":%.2f,\"concurrency_min_busy_fraction\":%.9f,", concurrency_verified ? "true" : "false", workers[0].cpu_busy_fraction, concurrency_threshold, concurrency_min_busy);
    printf("\"frequency_khz\":%ld,\"frequency_verified\":%s,\"frequency_source\":\"%s\",\"frequency_source_cpu\":%d,\"frequency_source_path\":\"%s\",\"frequency_policy_id\":\"%s\",\"frequency_sample_count\":%" PRIu64 ",\"frequency_read_failures\":%" PRIu64 ",",
           options.frequency_khz, frequency_verified ? "true" : "false",
           shared.target_frequency_source.kind, shared.target_frequency_source.cpu,
           shared.target_frequency_source.path, options.frequency_policy_id,
           sampler.samples, sampler.read_failures);
    if (sampler.samples) printf("\"frequency_mean_khz\":%.3Lf,\"frequency_min_khz\":%ld,\"frequency_max_khz\":%ld,", sampler.sum / sampler.samples, sampler.minimum, sampler.maximum);
    else printf("\"frequency_mean_khz\":null,\"frequency_min_khz\":null,\"frequency_max_khz\":null,");
    printf("\"frequency_first_sample_ns\":%" PRIu64 ",\"frequency_last_sample_ns\":%" PRIu64 ",\"sample_period_us\":%d,", sampler.first_sample_ns, sampler.last_sample_ns, options.sample_period_us);
    printf("\"warmup_frequency_sample_count\":%" PRIu64 ",\"warmup_frequency_in_range_consecutive\":%d,\"warmup_frequency_required_consecutive\":%d,\"warmup_frequency_max_samples\":%d,",
           workers[0].warmup_frequency.sample_count,
           workers[0].warmup_frequency.in_range_consecutive,
           workers[0].warmup_frequency.required_consecutive,
           workers[0].warmup_frequency.max_samples);
    if (workers[0].warmup_frequency.valid_sample_count)
        printf("\"warmup_frequency_min_khz\":%ld,\"warmup_frequency_max_khz\":%ld,\"warmup_frequency_last_khz\":%ld,",
               workers[0].warmup_frequency.minimum,
               workers[0].warmup_frequency.maximum,
               workers[0].warmup_frequency.last);
    else
        printf("\"warmup_frequency_min_khz\":null,\"warmup_frequency_max_khz\":null,\"warmup_frequency_last_khz\":null,");
    printf("\"warmup_frequency_verified\":%s,\"warmup_work_units\":%" PRIu64 ",\"task_count\":%" PRIu64 ",\"worker_cpu_ms_per_task\":",
           workers[0].warmup_frequency.verified ? "true" : "false",
           workers[0].warmup_units,
           strcmp(options.benchmark, "empty_dispatch") ? UINT64_C(0) : options.work_units);
    if (!strcmp(options.benchmark, "empty_dispatch")) printf("%.12f", workers[0].cpu_ms / options.work_units); else printf("null");
    if (!strcmp(options.benchmark, "empty_dispatch")) {
        const double count = (double)options.work_units;
        printf(",\"dispatch_measurement_schema\":\"paired_dispatch_delta/v1\",\"dispatch_payload\":\"mix64_v1\",");
        printf("\"dispatch_payload_match\":%s,\"direct_batch_worker_cpu_ms\":%.12f,\"direct_batch_worker_wall_ms\":%.12f,",
               workers[0].direct_sink == workers[0].queue_sink ? "true" : "false",
               workers[0].direct_cpu_ms, workers[0].direct_wall_ms);
        printf("\"direct_batch_producer_cpu_ms\":%.12f,\"direct_batch_roundtrip_wall_ms\":%.12f,",
               shared.direct_producer_cpu_ms, shared.direct_roundtrip_wall_ms);
        printf("\"queue_dispatch_worker_cpu_ms\":%.12f,\"queue_dispatch_worker_wall_ms\":%.12f,",
               workers[0].queue_cpu_ms, workers[0].queue_wall_ms);
        printf("\"queue_dispatch_producer_cpu_ms\":%.12f,\"queue_dispatch_roundtrip_wall_ms\":%.12f,",
               shared.queue_producer_cpu_ms, shared.queue_roundtrip_wall_ms);
        printf("\"dispatch_wall_delta_ms_per_task\":%.12f,\"dispatch_worker_cpu_delta_ms_per_task\":%.12f,\"dispatch_producer_cpu_delta_ms_per_task\":%.12f,",
               (shared.queue_roundtrip_wall_ms - shared.direct_roundtrip_wall_ms) / count,
               (workers[0].queue_cpu_ms - workers[0].direct_cpu_ms) / count,
               (shared.queue_producer_cpu_ms - shared.direct_producer_cpu_ms) / count);
        printf("\"cold_start_included\":false");
    }
    if (!strcmp(options.benchmark, "empty_dispatch"))
        printf(",\"measurement_scope\":\"paired direct-batch versus per-item queue dispatch on one persistent target worker; producer and worker CPU plus roundtrip wall are separate; cold thread start excluded; proxy workload, not application code\",\"helpers\":[");
    else
        printf(",\"measurement_scope\":\"target worker thread CPU only; producer/main and sampler CPU excluded; proxy workload, not application code\",\"helpers\":[");
    for (int index = 1; index < options.active_tasks; index++) {
        if (index > 1) putchar(',');
        printf("{\"cpu\":%d,\"actual_cpu\":%d,\"affinity_verified\":%s,\"cpu_ms\":%.9f,\"wall_ms\":%.9f,\"completed_work_units\":%" PRIu64 ",\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ",\"covers_target_interval\":%s,\"cpu_busy_fraction\":%.9f,\"overlap_cpu_lower_bound_ms\":%.9f,\"overlap_cpu_lower_bound_fraction\":%.9f}", workers[index].cpu, workers[index].actual_cpu, workers[index].affinity_verified ? "true" : "false", workers[index].cpu_ms, workers[index].wall_ms, workers[index].completed_units, workers[index].start_ns, workers[index].end_ns, workers[index].covers_target_interval ? "true" : "false", workers[index].cpu_busy_fraction, workers[index].overlap_cpu_lower_bound_ms, workers[index].overlap_cpu_lower_bound_fraction);
    }
    printf("],\"affinities\":[");
    for (int index = 0; index < options.active_tasks; index++) {
        if (index) putchar(',');
        printf("{\"role\":\"%s\",\"cpu_requested\":%d,\"actual_cpu\":%d,\"verified\":%s}", index ? "helper" : "target", workers[index].cpu, workers[index].actual_cpu, workers[index].affinity_verified ? "true" : "false");
    }
    printf(",{\"role\":\"observer\",\"cpu_requested\":%d,\"actual_cpu\":%d,\"verified\":%s}],\"sink\":\"%016" PRIx64 "\"}\n", options.observer_cpu, sampler.actual_cpu, sampler.affinity_verified ? "true" : "false", workers[0].sink);
    return 0;
}
#elif defined(PARALLEL_CALIBRATION_TOPOLOGY_TEST)
static const char *physical_core_relation_name(PhysicalCoreRelation relation) {
    switch (relation) {
        case PHYSICAL_CORE_SAME: return "SAME";
        case PHYSICAL_CORE_DIFFERENT: return "DIFFERENT";
        case PHYSICAL_CORE_UNKNOWN: return "UNKNOWN";
        case PHYSICAL_CORE_CONFLICT: return "CONFLICT";
    }
    return "CONFLICT";
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fputs("topology-test ROOT LEFT_CPU RIGHT_CPU\n", stderr);
        return 2;
    }
    char *left_end = NULL, *right_end = NULL;
    long left = strtol(argv[2], &left_end, 10), right = strtol(argv[3], &right_end, 10);
    if (!left_end || *left_end || !right_end || *right_end ||
        left < 0 || left >= TOPOLOGY_CPU_LIMIT || right < 0 || right >= TOPOLOGY_CPU_LIMIT) {
        fputs("invalid test CPU\n", stderr);
        return 2;
    }
    puts(physical_core_relation_name(physical_core_relation_at(argv[1], (int)left, (int)right)));
    return 0;
}
#elif defined(PARALLEL_CALIBRATION_SIBLING_QUIETNESS_TEST)
int main(int argc, char **argv) {
    if (argc != 5) {
        fputs("sibling-quietness-test TOPOLOGY_ROOT BEFORE_PROC_STAT AFTER_PROC_STAT TARGET_CPU\n", stderr);
        return 2;
    }
    char *end = NULL;
    long target = strtol(argv[4], &end, 10);
    if (!end || *end || target < 0 || target >= TOPOLOGY_CPU_LIMIT) return 2;
    SiblingQuietness quietness;
    target_sibling_quietness_begin_at(&quietness, (int)target, argv[1], argv[2]);
    target_sibling_quietness_end_at(&quietness, argv[3]);
    putchar('{');
    print_target_sibling_quietness(&quietness);
    printf("\"topology_verified\":%s}\n", quietness.topology_verified ? "true" : "false");
    return 0;
}
#else
static int frequency_test_write(const char *path, const char *value) {
    if (!strcmp(value, "missing")) return unlink(path) == 0 || errno == ENOENT;
    FILE *file = fopen(path, "w");
    if (!file) return 0;
    int wrote = fprintf(file, "%s\n", value) > 0;
    int closed = fclose(file) == 0;
    return wrote && closed;
}

static int frequency_test_source(int argc, char **argv) {
    if (argc != 7) {
        fputs("frequency-test source CPU_ROOT CPU POLICY AFTER_CPUINFO AFTER_SCALING\n", stderr);
        return 2;
    }
    char *end = NULL;
    long cpu = strtol(argv[3], &end, 10);
    if (!end || *end || cpu < 0 || cpu > 4095 || !valid_frequency_policy_id(argv[4])) return 2;
    FrequencySource source;
    freeze_frequency_source_at(&source, (int)cpu, argv[4], argv[2]);
    char cpuinfo[FREQUENCY_PATH_CAPACITY], scaling[FREQUENCY_PATH_CAPACITY];
    if (!cpuinfo_frequency_path_at(cpuinfo, sizeof(cpuinfo), argv[2], (int)cpu) ||
        !scaling_frequency_path_at(scaling, sizeof(scaling), argv[2], argv[4]) ||
        !frequency_test_write(cpuinfo, argv[5]) ||
        !frequency_test_write(scaling, argv[6])) return 2;
    WarmupFrequency stable = {.verified = 1};
    printf("{\"frequency_source\":\"%s\",\"frequency_source_cpu\":%d,"
           "\"frequency_source_path\":\"%s\",\"initial_value\":%ld,"
           "\"post_mutation_value\":%ld,\"stable_window_frequency_verified\":%s}\n",
           source.kind, source.cpu, source.path, source.initial_value,
           read_frozen_frequency(&source),
           frequency_window_verified(&source, &stable, 1, 5, 0, 1000, 1000, 1000, .03)
               ? "true" : "false");
    return 0;
}

static int frequency_test_warmup(int argc, char **argv) {
    if (argc < 4) {
        fputs("frequency-test warmup REQUESTED_KHZ SAMPLE...\n", stderr);
        return 2;
    }
    char *end = NULL;
    long requested = strtol(argv[2], &end, 10);
    if (!end || *end || requested <= 0) return 2;
    WarmupFrequency frequency;
    warmup_frequency_init(&frequency);
    for (int index = 3; index < argc && !frequency.verified; index++) {
        long observed = -1;
        if (strcmp(argv[index], "missing")) {
            observed = strtol(argv[index], &end, 10);
            if (!end || *end) return 2;
        }
        warmup_frequency_observe(&frequency, observed, requested);
    }
    printf("{\"warmup_frequency_sample_count\":%" PRIu64
           ",\"warmup_frequency_in_range_consecutive\":%d,"
           "\"warmup_frequency_required_consecutive\":%d,"
           "\"warmup_frequency_max_samples\":%d,",
           frequency.sample_count, frequency.in_range_consecutive,
           frequency.required_consecutive, frequency.max_samples);
    if (frequency.valid_sample_count)
        printf("\"warmup_frequency_min_khz\":%ld,\"warmup_frequency_max_khz\":%ld,"
               "\"warmup_frequency_last_khz\":%ld,",
               frequency.minimum, frequency.maximum, frequency.last);
    else
        printf("\"warmup_frequency_min_khz\":null,\"warmup_frequency_max_khz\":null,"
               "\"warmup_frequency_last_khz\":null,");
    FrequencySource source = {.kind = "cpuinfo_cur_freq"};
    int verified = frequency_window_verified(
        &source, &frequency, 1, 5, 0, requested, requested, requested, .03);
    printf("\"warmup_frequency_verified\":%s,\"frequency_verified\":%s}\n",
           frequency.verified ? "true" : "false", verified ? "true" : "false");
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && !strcmp(argv[1], "source")) return frequency_test_source(argc, argv);
    if (argc >= 2 && !strcmp(argv[1], "warmup")) return frequency_test_warmup(argc, argv);
    fputs("frequency-test source|warmup ...\n", stderr);
    return 2;
}
#endif
