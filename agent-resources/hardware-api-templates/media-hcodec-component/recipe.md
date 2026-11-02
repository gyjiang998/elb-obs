# HCodec Component HDI Family Template

This is a system-integration recipe for Codec Component HDI. It is not an
application-direct NDK template.

Preserve component manager discovery, capability enumeration, component create
and destroy, callback registration, buffer ownership transitions and failure
cleanup. Adapt component name, role, codec parameters, buffer counts, memory
type and deterministic fixture. Verify that callbacks and output originate from
the selected component, compare against a software codec, and record all
service/permission prerequisites. If the deployed image does not expose the HDI
surface to the experiment profile, stop with an integration-boundary result
rather than replacing it with an unrelated API.
