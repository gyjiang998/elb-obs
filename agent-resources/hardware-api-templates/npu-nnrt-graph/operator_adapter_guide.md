# NNRT Operator Adapter Guide

This guide extends the shared NNRT graph lifecycle host. It is an adapter
contract, not a claim that every operator is supported by the current device.

For each selected `OH_NN_OPS_*` record, replace only these parts:

1. Tensor descriptors: shape, dtype, format, quantization and constant values.
2. Operator parameters and input/output tensor indexes.
3. Deterministic input and CPU/software oracle with an explicit tolerance.
4. The operation-specific availability and execution assertions.

Keep device enumeration, model finish, `OH_NNModel_GetAvailableOperations`,
device-bound compilation, executor lifecycle, error handling and fallback from
the shared host template. A successful graph finish is not sufficient: the
selected operation must be reported available, compile for the selected device,
execute, and match the oracle.

Operator families such as activation, convolution, elementwise arithmetic,
indexing, comparison, matrix, normalization, pooling, reduction, sequence,
shape/layout, unary math and vision post-processing all share this lifecycle.
Their tensor and parameter adapters are not interchangeable.
