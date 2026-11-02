# Selected-step scenario capture

`backend/scenario_capture.py` validates and stores plans. `runner.py` executes
structured UI actions through either `LocalDeviceClient` (HDC on the platform
host) or `GatewayDeviceClient` (signed jobs to a registered Windows Bridge).
The page automatically discovers devices, preferring actual local targets and
falling back to online Windows Bridges. A unique device is selected automatically;
multiple candidates are presented as devices rather than transport settings.
Discovery pauses during capture. Local HDC is
resolved from `HETERO_HDC_BIN`, runtime assets or PATH. Generated Python embeds
the complete plan and calls the platform API. By default, `--platform URL` is
enough for one automatically discovered device; use `--device-serial SERIAL`
to select among multiple devices. Explicit routing uses `--connection-mode local
--device-serial SERIAL` for a locally connected device, or `--connection-mode
bridge --bridge-id ID --device-serial SERIAL` for the Bridge. The API host runs
HDC in local mode; the Python client itself does not require HDC.

The default Gallery template has 17 steps. It prepares all preceding steps,
captures only selected steps and stops after the last selected step. The plan
can set `capture.preparation_mode=already_prepared` to start at the first selected
step; intervening steps still run and current-screen postconditions are checked.
The default preparation mode is `auto`. The profile contains reference screen
dimensions and semantic UI selectors from the source experiment; actual display
dimensions are read from the device at run time, as described below.
Screen preparation is a shared pre-action gate in `screen_preparation.py`, called
by the runner for every scenario step before business actions, measured markers,
Trace, HiLog or PMU start. Local HDC, Windows Bridge, custom plans and generated
Python all reach the same runner entry; changing scenes needs no separate unlock
script. `already_prepared` skips preceding business steps and still performs this
gate. Screen preflight distinguishes swipe lock screens, credential entry and
USB-mode dialogs from the desktop and the plan's configured target application.

Runner `1.2.15` uses `screen_preparation/v7`. It queries the power state and
performs one state-aware wake: Power only when explicitly inactive; unknown
state uses `power-shell wakeup`. Only an actual wake command requires the one-second settling interval; a fresh
usable page is preserved. A locked or unverified page first receives
`aa start -a <profile.ability> -b <profile.bundle>`, before repeating wake/gesture
attempts. A successful command is not unlock
proof: a fresh UI must be usable and the latest known power state cannot be
inactive. For the Gallery bundle, an explicit Ability-not-found response permits
`com.huawei.photos.MainAbility`; this updates the effective run profile, leaving
the saved plan unchanged. A run-local capability cache remembers the working power query interface
and skips explicitly unsupported commands; it does not cache screen state or treat transport failures as unsupported APIs.
Preparatory launches are recorded, and a measured
launch is marked as prelaunched rather than cold start.

Runner 1.2.15 also bounds every exact-child shutdown in the device step script.
Action, Trace heartbeat, PMU probe/sampler, HiLog and Trace finalization receive
a finite flush/TERM grace, then are killed and reaped if still alive. Trace
finalization starts with a 3-second natural wait and adds one second per extra
64 MiB of configured buffer; a 1 GiB plan receives 18 seconds and every input
remains capped at 25 seconds. The host
shell allowance is 45 seconds beyond the configured step timeout instead of 180
seconds. Failed steps fetch `cleanup_phase.txt` and `cleanup_events.log` so the
last stalled collector is visible; successful steps do not add those HDC reads.

On an explicitly selected local private device, idle TraceFS does not by itself
prove that the HiTrace service released its recording session. After acquiring
the exclusive collector session and confirming every readable `tracing_on` is
zero, Runner 1.2.15 invokes bounded `hitrace --trace_finish_nodump`, verifies the
result and checks TraceFS again before capture. Bridge and shared-device paths
never perform this service-state reset. A verified PMU binary whose sampler was
not reached because an earlier collector failed is reported as `PMU_NOT_STARTED`,
not as a missing sampler.

If the page remains locked, remaining non-toggle wake methods, a slow proportional
upward swipe, Back and two compatibility swipes are tried as needed. The slow
reference is `638,2400 -> 638,500` on `1280 x 2832` over 1.9 seconds. Coordinates
scale to the current layout; UITest receives derived velocity in px/s. Back has
an alternate lowercase spelling only for explicit parser rejection. Each attempt
is recorded and followed by a fresh layout; a usable app is not scrolled or
relaunched. The separate compatibility wake helper retains `power_key_stateful/v2`.

`screen_awake.py` reads `ScreenOffTime` from `power-shell dump -s`, applies a finite
`power-shell timeout -o <milliseconds>` override and verifies it. The idle delay
is at least the step execution budget plus 180 seconds, never shortening an existing longer delay. The override is refreshed
at script handoff only when remaining protection cannot cover the step budget plus 60 seconds.
Cleanup restores the original override, or uses `timeout -r`
when none existed, with read-back confirmation. External changes are preserved.
Unknown original settings are left unchanged; failures remain in
`screen_awake_guard`. Restoration failure produces `SCREEN_TIMEOUT_RESTORE_FAILED`.
The override does not itself expire: hard process termination or a disconnected
device can prevent restoration, in which case the finite delay may remain until
recovery. This uses neither an infinite screen lock nor periodic synthetic input.

After uploading the device script, the runner performs a separate screen
gate (`screen_handoff_timing`, details in `capture_handoffs`). With verified idle protection,
this reads a fresh layout directly; an abnormal page enters wake/recovery, while a usable page
avoids duplicate power queries and sleeps. Without verified protection it uses the full preflight.
Controls, geometry
and target PID are resolved again. A changed script is uploaded and checked once
more; persistent changes stop with `SCENARIO_CAPTURE_HANDOFF_UNSTABLE`. All these
operations precede Trace/HiLog/PMU and measured markers. Neither screen gate has a cumulative time limit;
verified readiness proceeds immediately. Each method has finite attempts and
each device operation remains cancellable with its own timeout. Device-specific firmware still
needs validation on the target phone.

Password, PIN and pattern entry stop preparation with
`SCENARIO_UNLOCK_AUTH_REQUIRED`; complete authentication on the phone and retry.
A visible `UsbFunctionSwitchExtAbility` chooser is dismissed before measurement:
one Back action, followed only if it remains by its uniquely identified close
button (`advanced_dialog_button_0`, visible enabled clickable `Button`, text
`确定` or `OK`) within the chooser subtree. The current layout supplies its bounds.
The verified button handler only closes the dialog; mode selectors are untouched.
Each action requires a fresh layout. Persistent or recurring choosers retain
`SCENARIO_USB_DIALOG` and use the same failure screenshot path.
If the supported preparation attempts cannot verify
unlock, the run retains `SCENARIO_SCREEN_LOCKED` or `SCENARIO_UNLOCK_UNVERIFIED`,
and an unrecoverable preparation failure reports `SCENARIO_SCREEN_PREPARATION_FAILED`.
`screen_preparation` records the initial/final state, Back/dismissal attempts and swipe count,
`outside_capture_preparation` records actions, and `unlock_*.json` layouts retain
the verification evidence. These operations occur before collectors and measured
step markers start; `already_prepared` still skips preceding scenario actions.

Normal screen preparation, Gallery reset and the post-upload screen gate have no cumulative
deadline. Slow but successful Bridge round trips do not fail the run at 45 seconds.
Each device I/O requests at most eight seconds and remains cancellable; layouts
use the short cancellable transfer path. Finite attempts prevent an endless
unlock loop. `screen_preparation_timing`, `gallery_preparation_timing` and
`screen_handoff_timing` retain current
operations, elapsed time and outcomes; `budget_seconds` / `remaining_seconds`
are null. A single command timeout remains a command failure, while transport
`state_unknown` is distinguished from confirmed remote timeout.

Screen-preparation failures use a separate shared 20-second evidence budget for
`power-shell dump -s`, `hidumper -s 3704 -a '-all'` and one PNG screenshot before
collectors start. Each I/O is capped at eight seconds and the remaining evidence
budget. `screen_state_diagnostics` retains bounded power/lock-service output;
`screen_evidence_timing` records evidence progress. Cancellation skips it;
evidence failure or timeout preserves the original failure. Files
up to 16 MiB use the existing client collection path and are registered as
`screen_preparation_screenshot`. The UI offers view/download links through
`GET /v1/scenario-capture/runs/{run_id}/steps/{step_id}/preparation-screenshot`
(`?download=true` for attachment). The endpoint serves only that run's registered,
unchanged PNG, checks project identity, paths, size and SHA-256, and disables
caching. Historical runs have no newly fabricated screenshot. Command errors,
gesture parameters and observed lock IDs remain in the manifest even if the
device cannot provide a screenshot. Owned collector shutdown and device-lock
cleanup are separate. The 20-second evidence deadline applies only after
preparation has already failed and cannot cause an otherwise successful
preparation to fail.

The collector-session lease is reused while its remaining duration covers the next operation,
adapter response allowance and 15 seconds of margin. Slow transfers renew it before execution;
failures invalidate cached ownership. Device-side capture entry still checks session token and boot identity.
`collector_preparation.lease_cache_hits` and `lease_remote_refreshes` expose the actual behavior.
Script uploads pack escaped UTF-8 commands up to 12 KiB per RPC and reuse the already created step directory.

Deploy platform `0.1.20` and restart its server/API. Windows Bridge `1.4.7` already
supports this update: if it is running that version, no Bridge restart is needed
for the new preparation sequence. Older Bridges must be updated, then stopped
and restarted with `deploy/windows/run-device-bridge.ps1`; check
`implementation_version=1.4.7`. That Bridge version shares deadlines across HDC
queue waits and execution, rejects expired work after lock acquisition, and
honors short file-receive budgets instead of extending them to 240 seconds.
File receive and upload share the remaining budget. Updating only the platform
does not replace an older running Windows Bridge. The signed shell/file protocol
remains compatible.

The command semantics follow the OpenHarmony
[power-shell reference](https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/power-shell.md)
and [UITest guide](https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/application-test/uitest-guidelines.md).

Automatic Gallery launch preparation now resets the running session in
`gallery_reset.py`: `aa force-stop <bundleName>`, followed by up to three checks
that its process has exited. An unmeasured launch then runs the configured
`aa start` and verifies a fresh home layout containing Photos and Albums.
If Gallery restores an editor, browser or dialog, at most four Back actions
attempt to reach home. Persistent pages, command failures and an unconfirmed
stop produce specific preparation diagnostics. Only the Gallery bundles receive
this reset; application data and media are preserved. `already_prepared` keeps
the selected starting page, including when launch itself is selected.

For a measured launch, stopping Gallery and verifying the desktop happen before
collectors start; the requested `aa start` remains inside the capture window.
The run records the reset and the actual pre-action PID. Restarting an application
does not prove a cold device, cleared caches or a complete cold-start PMU window.
If screen recovery launches the app again before measurement, its prelaunch is
recorded. An unavailable pre-action PID remains a PMU target-resolution diagnostic.
The post-upload handoff verifies readiness without repeating the reset.
`gallery_reset` and `outside_capture_preparation` retain commands and outcomes;
`gallery_preparation_timing` exposes cancellable per-command progress, with no
cumulative 45-second deadline. Command semantics follow the OpenHarmony
[aa reference](https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/aa-tool.md#强制停止进程命令force-stop).

Postconditions poll UI layouts within a bounded interval after collectors stop,
without replaying measured actions. Failures retain step, phase and recovery advice.
Device screen and controls are checked during execution. The first-city and
city-page selectors must be supplied before a plan reaching those steps can run.
Pinch gestures can be executed without claiming their visual extrema are proven;
that missing calibration remains a quality warning.

Each selected step executes one device shell script containing collector startup,
UI operations, markers, dwell and cleanup. HiLog and the PMU reader are background
children of that script. HiTrace has a monotonic clock, and the runner checks
marker closure, scheduler events, buffer overruns and the selected clock.
`collector_cleanup.py` tracks platform ownership with a session token, device
boot identity and process start identity. New captures inspect recoverable
platform leftovers; cancellation and failure stop only collectors whose
ownership can be established. Trace finalization additionally requires the
matching ownership marker, and recovered output stays in its original step.
Unclaimed older captures and external tracing are preserved with a diagnostic.
When cleanup is unconfirmed, the device session remains reserved rather than
letting another capture overlap. Its renewable lease protects concurrency; it
is not a cumulative task deadline. Session checks and recovery are recorded in
`collector_preparation`.

## Dynamic display geometry

Runner `1.2.0` defaults to `profile.screen_size_mode=dynamic`, including saved
plans that omit this field. `screen_size` in a saved plan is a reference, not a
claim about the connected device. Each recovered `dumpLayout` must contain a
valid display-root `bounds` starting at `[0,0]`. The runner updates only its
private profile copy from this evidence; it never rewrites the saved plan or
substitutes child/window bounds or reference dimensions for missing evidence.
Top-level application/ability identities, explicit window types, or a window
ID paired with window-root metadata are rejected even when bounds start at
`[0,0]`. Neutral display IDs or a `root` type alone remain accepted; window
identities on children do not invalidate a normal aggregate display root.
Explicit `fixed` mode instead requires the actual dimensions to match the plan.

Clicks use the current semantic control's actual bounds. Explicit `point` and
`region` selectors, custom swipe endpoints, unlock swipes, scrolling and pinch
gestures use normalized display coordinates. Unknown absolute pixel coordinates
are not scaled automatically. Ratios remain strictly between zero and one;
rounding is bounded to valid pixel indices `0..width-1` and `0..height-1`.
On first discovery and every later size or
orientation change, cached control positions are cleared and the runner obtains
up to three layouts to establish two consecutive identical dimensions. These
additional observations occur outside the measured operation. If the dimensions
keep changing, the run stops before generating subsequent actions. A cached
album-tab position from a different geometry cannot be reused; the tab must be
observed again or a calibrated normalized selector must be supplied.

`manifest.screen_geometry` (`screen_geometry/v1`) retains `reference_size`,
`actual_size`, `mode`, `source=dumpLayout.root.bounds`, confirmation status and
an `observations` list. Each observation links its layout file and records the
step, actual dimensions, orientation, confirmation and invalidated cache keys.
Each step records the geometry used for its resolved actions.
`profile_effective.screen_size_reference` retains the plan's reference while
`profile_effective.screen_size` reflects the most recently observed display.
Missing/invalid root geometry, a fixed-mode mismatch or unstable dimensions
produce actionable `SCENARIO_SCREEN_GEOMETRY_INVALID`,
`SCENARIO_SCREEN_SIZE_MISMATCH` or `SCENARIO_SCREEN_GEOMETRY_UNSTABLE` errors.
Layout observations bracket each measured action batch; they do not prove that
the device never rotated during an already-running batch. Gesture extrema and
page semantics still require their existing checks.

## PMU sampler

`thread_pmu_sampler.c` is the generic thread sampler adapted from the experiment recorded in
`provenance.json`. Version 1.2.0 supports a **10–10000 μs requested period**, with
the existing platform default of **100 μs** preserved. The original source and
SHA-256 remain archived at `reference/gallery_thread_pmu.original.c`; adaptation
version, current source hash and changes are recorded separately in provenance.

The sampler accepts a target TID and has no Gallery package, control or scenario
logic. A single binary serves different applications. The current platform
resolves the unique PID for each step's `profile.bundle` and samples that main
thread's user-mode counters. It does not aggregate all application threads or
kernel time. An application not yet running, or more than one matching PID,
produces a target-resolution diagnostic; it does not claim full cold-start coverage.

The platform uses generic source and binary names. The previous C filename
remains an include compatibility entry; `--version` and the capability schema keep
their historical serialized identifiers. The 1.1.0 source is archived separately;
current bundles bind the 1.2.0 source hash. Existing single-group device samplers
remain usable with their advertised modes. Multi-group requests automatically
prepare a current bundle when an executable at a default path lacks that mode,
using a new hash-addressed path without replacing existing files. No per-application
build is required.

```text
thread_pmu_sampler --version
thread_pmu_sampler --capabilities
thread_pmu_sampler --clock
thread_pmu_sampler TARGET_TID PERIOD_US DURATION_MS READER_CPU GROUP OUTPUT_CSV METADATA_JSON READY_FILE normal
```

`--capabilities` reports `gallery_thread_pmu_capabilities/v1`, sampler version,
minimum/maximum requested period, maximum duration, event groups, default normal
policy and `absolute_clock_nanosleep` wait strategy. It opens no performance
events. Numeric arguments use strict decimal parsing before unit multiplication:
period 10–10000 μs, duration 1–10000 ms, positive TID and a valid CPU index.
Negative, malformed, oversized and overflow-prone inputs fail with a readable
parameter message. Duration must cover at least one requested period.

The runner first hashes the installed executable and queries its capabilities.
It preserves `pmu_capabilities.json`, its command log, and a parsed
`pmu_compatibility.json`. A reported range that excludes the request yields
`PMU_PERIOD_UNSUPPORTED`. An older executable without the capability contract
is still tried for historical requests of 20 μs or more; a sub-20 μs request is
stopped before sampling with `PMU_SAMPLER_UPGRADE_REQUIRED`. Trace and HiLog
continue and the PMU diagnostic explains the required device update. The
platform does not silently increase 10 μs to 20/100 μs or claim the old binary
supports it. An old executable rejecting a historical parameter also produces
`PMU_PARAMETERS_REJECTED`, with its original log and exit code retained.

Capability discovery runs before Trace and HiLog start. The background probe has
a two-second polling budget and bounded output files. Timeout or cancellation
cleans up its PID and polling timer, escalates TERM to KILL when needed and waits
for exit; an available `setsid` also contains child processes, with a Linux
`/proc` descendant cleanup fallback. `pmu_capability_probe.json` retains probe
status, actual exit code and timeout information. `pmu_compatibility.json` carries
the same timeout flag. Empty output, nonzero exit, timeout and diagnostic text
outside the JSON object do not establish sub-20 μs support; historical periods
remain eligible under the explicitly unverified legacy contract. Probing does not
extend the Trace/HiLog measurement window.

### Same-operation multi-group sampling

New plans default to `GROUP=auto`; `all` is an API/CLI alias with the same policy.
Existing plans retain their explicit single group (`cache`, `branch`, `stall`,
`l1d`, `llc` or `dtlb`). Both modes execute the user action **once**, with no automatic
replay. The sampler opens seven independent perf groups during that execution:

| Group | Events in the same group |
| --- | --- |
| base | cycles, instructions |
| cache | cycles, instructions, generic_cache_references, generic_cache_misses |
| branch | cycles, instructions, branch_instructions, branch_misses |
| stall | cycles, instructions, stalled_frontend, stalled_backend |
| l1d | cycles, instructions, l1d_read_access, l1d_read_misses |
| llc | cycles, instructions, llc_read_access, llc_read_misses |
| dtlb | cycles, instructions, dtlb_read_access, dtlb_read_misses |

This requests 14 distinct events through 26 perf descriptors. Repeating cycles and
instructions supplies **group-local denominators**: a branch or cache miss ratio
must not borrow instructions measured on another group's schedule. The two-event
base group can still run when the hardware cannot schedule a larger group.
Each failed event records errno and stage; remaining events/groups continue. An
opened event is not proof of collected data: groups with zero running time are
reported as unavailable observations, even when all opens succeeded.

The kernel multiplexes independent groups when physical counters are insufficient.
Every group retains its own `time_enabled`, `time_running`, timestamps and runtime
coverage. Low coverage is visible in result diagnostics. Multiplex-scaled counts
are estimates, never strict observed interval bounds; a short phase may have no
representative coverage. `llc` is the kernel's generic LL-cache mapping, whose
physical cache-level semantics must be verified for the device before an LLC
bottleneck claim. `cache` is not implicitly an LLC group.

Multi-group results keep `pmu.csv` / `pmu.json` names but use
`thread_pmu_capture/v2` and `format=grouped_long_csv`. Each CSV row has `group_id`,
`sample_index`, read brackets, enabled/running times and `event0`–`event3` mapped
by that group's actual `event_names`. Failed events are omitted from the mapping;
unused columns are blank. Metadata preserves requested events, failed opens,
enable errors, and per-group row counts. Group cumulative streams are independent.
The single-group CSV/metadata format is unchanged. Validator v3 reports each
group's completeness, measured cadence, runtime coverage and useful events.

Multi-group sample buffers are capped at **128 MiB** in total, allocated and
prefaulted before the ready marker. If a high-rate, long window would exceed that
limit, the sampler increases its effective period and records both requested and
effective periods. Results show `PMU_MULTI_BUFFER_LIMIT`; there is no silent claim
of 10 μs resolution. Shorten the window or select one group when temporal fidelity
matters more than breadth. Actual perf reads and scheduling can further reduce
resolution; the configured interval is never a guarantee.

### Repository bundle and automatic deployment

Platform 0.1.10 ships the compiled OpenHarmony AArch64 sampler in
`tools/scenario_capture/prebuilt/aarch64-linux-ohos/`. The active version contains
`manifest.json`, a content-addressed binary and its build receipt. These files
are tracked in Git; they do not depend on a developer's `.local-artifacts` directory.

Normal `heteroctl install/start/restart` in device mode verifies and prepares the
bundle at the configured runtime asset path. It reuses a valid existing bundle,
otherwise publishes the repository bundle atomically while retaining old versions.
No SDK, compiler, internet download or separate transfer is needed on the server.
Analysis-only deployments do not prepare device assets. A broken bundle is reported
without overwriting immutable version files; Trace/HiLog retain their independent
availability checks. `heteroctl pmu` can retry preparation from repository assets.

`HETERO_PMU_SAMPLER_BUNDLE_ROOT` remains available for a custom runtime destination.
Without an override it is `runtime_asset_root()/pmu-sampler`. Catalog reads remain
read-only; installation happens during deployment, not when viewing the page.

Maintainers who change `thread_pmu_sampler.c` rebuild the repository bundle using
a full native SDK matched to the build host, then commit the source, manifest,
binary and receipt together:

```sh
python -m tools.scenario_capture.build_pmu_sampler \
  --native-sdk /path/to/openharmony/native \
  --output tools/scenario_capture/prebuilt/aarch64-linux-ohos
```

The compact `HETERO_OPENHARMONY_SDK_ROOT` header resource is separate from that
full native SDK. Explicit `heteroctl pmu` with `HETERO_OPENHARMONY_NATIVE_SDK_ROOT`
configured still supports local rebuilding. Regular deployment uses the shipped
bundle without requiring that compiler. Tests verify the repository bundle against
the current source SHA-256, binary hash, build receipt, target ELF and loader.
Git attributes preserve the byte sequences across Windows checkouts.

Before a platform-managed capture, the device client checks the requested path:

- An existing executable is inspected and reused when it supports the requested
  mode; it is not assumed to match the repository source. A default-path executable
  lacking auto/all triggers installation at a new verified hash path. Custom paths
  remain user-managed and return an explicit upgrade diagnostic if incompatible.
- A missing default `/data/local/tmp/thread_pmu_sampler` checks the legacy default
  `/data/local/tmp/gallery_thread_pmu_focus` first, then triggers installation
  from the verified bundle via the existing local HDC or Windows Bridge channel.
  New files use `/data/local/tmp/hetero_tools/pmu/<sha256>/thread_pmu_sampler`; the
  effective path is recorded in the execution copy without changing the saved plan.
- The platform verifies the remote hash and queries capabilities before starting
  collectors. Preparation failure is recorded with its reason, while Trace and
  HiLog remain eligible to run. `pmu_preparation.json` and the run manifest retain
  the result. Opening a page or saving a plan only checks server asset status.

Capability probes use device-portable shell operations without `tr` or `awk`.
Responses are parsed between explicit field markers, preserving empty legacy
capability output even when a transport removes blank lines. HDC diagnostics
outside a complete envelope are retained as diagnostics; incomplete envelopes
remain failures with a specific reason. Legacy default samplers without auto/all
support follow the verified installation path above.

The capture page can send a failed current capture to the selected Agent for one
repair and recapture. The default-on switch is beside the execution button.
A repair ticket binds the original job, device, application, selected steps and
collectors; it permits repaired preparation parameters and selectors. The Agent
passes `repair_request_id` to `scenario_capture_execute`; the action router also
supplies it from the focused repair context. Repeated submissions reuse the same
retry job. Closing the switch revokes pending tickets and requests cancellation
of a running retry. Historical results, manual cancellation and ordinary partial
sample coverage do not initiate repair.

Ticket endpoints are `POST /v1/scenario-capture/runs/{run_id}/repair-request`
(with `job_id`), `GET /v1/scenario-capture/repair-requests/{request_id}` and
`POST /v1/scenario-capture/repair-requests/{request_id}/cancel`. Pending requests
expire after 30 minutes or are revoked after a server restart; this expiry is
separate from capture execution timeouts.

The generated Python script submits its plan through the platform API and shares
the same managed preparation. Direct use of the runner without the platform
manager still requires the caller to prepare its configured device sampler.
Existing samplers are not overwritten, and capability probing opens no perf
counters. Device permissions and supported events are assessed during sampling.
Windows Bridge 1.4.7 already supports this deployment path.

**10 μs is a sampling request, not a promise of 10 μs observations.** The reader
uses normal scheduling, a configurable CPU and absolute `clock_nanosleep`;
perf-group reads, kernel scheduling and contention determine actual intervals.
No busy loop or FIFO elevation is enabled by the platform. At a 10 μs request
and the 10-second maximum, a single group uses about 80 MB on the 64-bit target;
auto/all uses the 128 MiB total limit described above. Allocation and prefaulting
happen before the ready marker.
Compare observed median/P95/max intervals and missed deadlines in
`sampling_quality` before interpreting a short BuildItem phase. Smaller requests
can increase overhead and skip more deadlines without improving real resolution.

Counters cover the uniquely resolved pre-action application main thread.
If the process does not exist, the sampler is missing, or events are denied,
available Trace/HiLog and specific PMU diagnostics remain. A normal bounded
SIGTERM stop is recorded as exit code 5 with `stopped_signal=15`; this is distinct
from a read error. Validator v2 separates successful initial zero cumulative
baselines from read errors, verifies later active counter samples and preserves
capture completeness independently of sampling cadence. Actual cadence
degradation remains a run-quality warning even with `collector_status=collected`.
A bounded sampler can end before a long UI operation; use measured sample
boundaries and operation windows to establish coverage.

## Result contract

`scenario_capture_manifest/v1` includes per-step execution and collector status,
source/plan/device identity, managed files with content hashes, Trace marker
windows, PMU metadata, clock brackets and cleanup state. Run/manifest provenance
records `connection_mode` and the selected executor. Local capture file recovery
uses a scoped 2 GiB limit and incremental hashing; other local device collection
retains its existing 32 MiB default. `trace.ftrace` files use
the device's absolute monotonic timestamps. The marker shell thread is not the
application thread. HiLog uses epoch/nsec; PMU `--clock` provides bracketed
monotonic/realtime samples when available. The manifest does not turn a step
envelope into a callback critical path or assert calibrated cross-clock mapping.

## Windows-local package execution

When bridge round trips are too slow for collector preparation, generate a
Windows-local package from the saved plan and extract it on the PC connected to
the selected phone. Run this from the extracted directory:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\run-capture.ps1
```

The script uses the packaged device serial unless `-DeviceSerial` is given.
Packages generated without an online platform device discover the unique local
HDC target; multiple targets require an explicit `-DeviceSerial` selection. It
finds HDC from `-HdcPath`, the existing Device Bridge configuration, or `PATH`,
and requires Python 3.11 or newer. `-OutputRoot` changes the result directory.
It calls HDC directly with a fixed argument vector; it does not submit device
jobs through the platform bridge. Do not start another platform capture against
the same device while this command is active.

The output retains a complete local `scenario-capture/runs/capture_<id>` folder
for diagnostics and creates `capture_<id>.capture.zip`. The archive contains a
root `manifest.json` plus only its declared, hashed evidence files, so it can be
registered through Trace management. Service-owned paths are assigned during
import and are never trusted from the portable archive.

Run tests without a phone:

```sh
.venv/bin/python -m unittest backend.tests.test_scenario_capture backend.tests.test_gallery_capture_preparation backend.tests.test_scenario_capture_screen backend.tests.test_scenario_capture_pmu backend.tests.test_pmu_sampler_capabilities backend.tests.test_pmu_multi_sampler backend.tests.test_pmu_capability_probe_lifecycle -v
```

Power-state and screen-off timeout reads try `power-shell dump -s`, then use
`hidumper -s 3301 -a -a` when no unique current value is returned. Wake decisions,
pre-capture checks and reversible timeout protection share this compatibility
path. Each query retains its own timeout and cancellation handling.
