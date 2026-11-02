# Audio Effect HDI/provider integration recipe

This recipe is intentionally not a portable application binary. Select it only when the current source and target system expose a real Audio Effect HDI/VDI provider path.

1. Resolve the target version's exact provider, factory, controller and process declarations from the profile or repository API contract.
2. Create a deterministic interleaved PCM fixture with explicit sample format, channel count, sample rate and frame count.
3. Run the existing AP/software effect chain as the oracle and retain its output buffer.
4. Enumerate the provider/effect descriptor, create the controller, send required configuration commands and call the real process entrypoint.
5. Compare every output sample with a documented absolute/relative tolerance; record clipping, saturation and stateful warm-up behavior.
6. Measure frame processing median/p95 and deadline misses separately from provider creation and control commands.
7. Use HATS/Developer Test plus xDevice CppTest when a registered runner and system build surface exist. Otherwise stop with a provider/profile evidence gap; do not relabel a CPU-only native test as DSP execution.
