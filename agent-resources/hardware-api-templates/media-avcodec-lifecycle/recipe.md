# AVCodec Lifecycle Family Template

Use this family template after a hardware-category capability query has selected
a concrete codec. Preserve the lifecycle and replace MIME, direction, format,
fixture and oracle for the current workload.

1. Query hardware capability and retain an explicit software fallback.
2. Create the encoder or decoder by the returned codec name.
3. Configure width, height, pixel/sample format, profile, bitrate or frame rate
   only when they belong to the selected direction.
4. Register callbacks and implement the complete input/output buffer lifecycle.
5. Prepare, start, feed a deterministic bitstream/frame fixture, drain output,
   flush/stop and destroy in all success and failure paths.
6. Compare decoded content or validate the encoded bitstream with an independent
   software path before measuring throughput and latency.

Capability discovery alone is not a hardware execution result. Callback reach,
output validity, fallback behavior and end-to-end timing are separate evidence.
