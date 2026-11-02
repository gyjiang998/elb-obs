// NNRT accelerator smoke test for 02_npu_matmul.cpp.
//
// The test intentionally validates a narrow, auditable contract:
// - enumerate an OH_NN_ACCELERATOR device through the public NNRT API;
// - build the equivalent 2x3 by 3x2 MatMul graph;
// - verify that the selected accelerator reports the operation available;
// - compile and run the graph on that selected device; and
// - compare its output with the fixed CPU reference result.
//
// A PASS does not claim that an arbitrary hotspot has been migrated, or that
// the accelerator is faster for a production-sized workload.

#include <neural_network_runtime/neural_network_runtime.h>

typedef __SIZE_TYPE__ Size;

static long SysWrite(int fd, const char *buffer, Size size)
{
    register long x0 __asm__("x0") = fd;
    register const char *x1 __asm__("x1") = buffer;
    register Size x2 __asm__("x2") = size;
    register long x8 __asm__("x8") = 64; // Linux aarch64 write
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory");
    return x0;
}

static Size StringLength(const char *value)
{
    Size length = 0;
    while (value[length] != '\0') {
        ++length;
    }
    return length;
}

static void WriteLine(const char *value)
{
    SysWrite(1, value, StringLength(value));
    SysWrite(1, "\n", 1);
}

static void DestroyDesc(NN_TensorDesc **desc)
{
    if (desc != 0 && *desc != 0) {
        OH_NNTensorDesc_Destroy(desc);
    }
}

static void DestroyTensor(NN_Tensor **tensor)
{
    if (tensor != 0 && *tensor != 0) {
        OH_NNTensor_Destroy(tensor);
    }
}

static NN_TensorDesc *CreateTensorDesc(OH_NN_DataType dataType, const int32_t *shape, Size shapeLength)
{
    NN_TensorDesc *desc = OH_NNTensorDesc_Create();
    if (desc == 0 || OH_NNTensorDesc_SetDataType(desc, dataType) != OH_NN_SUCCESS ||
        OH_NNTensorDesc_SetFormat(desc, OH_NN_FORMAT_NONE) != OH_NN_SUCCESS ||
        (shapeLength > 0 && OH_NNTensorDesc_SetShape(desc, shape, shapeLength) != OH_NN_SUCCESS)) {
        DestroyDesc(&desc);
        return 0;
    }
    return desc;
}

static int AddModelTensor(OH_NNModel *model, NN_TensorDesc **desc, uint32_t index, OH_NN_TensorType type)
{
    const OH_NN_ReturnCode addResult = OH_NNModel_AddTensorToModel(model, *desc);
    if (addResult != OH_NN_SUCCESS || OH_NNModel_SetTensorType(model, index, type) != OH_NN_SUCCESS) {
        return 0;
    }
    return 1;
}

static int BuildMatMulModel(OH_NNModel **outModel)
{
    static const int32_t kLeftShape[] = {2, 3};
    static const int32_t kRightShape[] = {3, 2};
    static const int32_t kOutputShape[] = {2, 2};
    NN_TensorDesc *descs[3] = {0, 0, 0};
    OH_NNModel *model = OH_NNModel_Construct();
    if (model == 0) {
        WriteLine("NPU NNRT graph: model construct failed");
        return 0;
    }

    descs[0] = CreateTensorDesc(OH_NN_FLOAT32, kLeftShape, 2);
    descs[1] = CreateTensorDesc(OH_NN_FLOAT32, kRightShape, 2);
    descs[2] = CreateTensorDesc(OH_NN_FLOAT32, kOutputShape, 2);
    for (Size index = 0; index < 3; ++index) {
        if (descs[index] == 0) {
            WriteLine("NPU NNRT graph: tensor descriptor failed");
            for (Size cleanup = 0; cleanup < 3; ++cleanup) {
                DestroyDesc(&descs[cleanup]);
            }
            OH_NNModel_Destroy(&model);
            return 0;
        }
    }

    const int tensorsAdded =
        AddModelTensor(model, &descs[0], 0, OH_NN_TENSOR) &&
        AddModelTensor(model, &descs[1], 1, OH_NN_TENSOR) &&
        AddModelTensor(model, &descs[2], 2, OH_NN_TENSOR);
    for (Size index = 0; index < 3; ++index) {
        DestroyDesc(&descs[index]);
    }
    if (!tensorsAdded) {
        WriteLine("NPU NNRT graph: tensor setup failed");
        OH_NNModel_Destroy(&model);
        return 0;
    }

    uint32_t inputIndicesData[] = {0, 1};
    uint32_t outputIndicesData[] = {2};
    OH_NN_UInt32Array parameterIndices = {0, 0};
    OH_NN_UInt32Array inputIndices = {inputIndicesData, 2};
    OH_NN_UInt32Array outputIndices = {outputIndicesData, 1};
    if (OH_NNModel_AddOperation(model, OH_NN_OPS_MATMUL, &parameterIndices, &inputIndices, &outputIndices) != OH_NN_SUCCESS ||
        OH_NNModel_SpecifyInputsAndOutputs(model, &inputIndices, &outputIndices) != OH_NN_SUCCESS ||
        OH_NNModel_Finish(model) != OH_NN_SUCCESS) {
        WriteLine("NPU NNRT graph: FINISH FAIL");
        OH_NNModel_Destroy(&model);
        return 0;
    }

    WriteLine("NPU NNRT graph: FINISH PASS");
    *outModel = model;
    return 1;
}

static int SelectAccelerator(size_t *outDeviceId)
{
    const size_t *deviceIds = 0;
    uint32_t deviceCount = 0;
    if (OH_NNDevice_GetAllDevicesID(&deviceIds, &deviceCount) != OH_NN_SUCCESS || deviceIds == 0 || deviceCount == 0) {
        WriteLine("NPU NNRT device enumeration: FAIL");
        return 0;
    }
    WriteLine("NPU NNRT device enumeration: PASS");
    for (uint32_t index = 0; index < deviceCount; ++index) {
        OH_NN_DeviceType deviceType = OH_NN_OTHERS;
        if (OH_NNDevice_GetType(deviceIds[index], &deviceType) != OH_NN_SUCCESS || deviceType != OH_NN_ACCELERATOR) {
            continue;
        }
        const char *name = 0;
        if (OH_NNDevice_GetName(deviceIds[index], &name) != OH_NN_SUCCESS || name == 0) {
            WriteLine("NPU NNRT accelerator name: unavailable");
            return 0;
        }
        WriteLine("NPU NNRT selected accelerator:");
        WriteLine(name);
        *outDeviceId = deviceIds[index];
        return 1;
    }
    WriteLine("NPU NNRT accelerator: NOT_FOUND");
    return 0;
}

static int VerifyOperationAvailability(OH_NNModel *model, size_t deviceId)
{
    const bool *supported = 0;
    uint32_t operationCount = 0;
    if (OH_NNModel_GetAvailableOperations(model, deviceId, &supported, &operationCount) != OH_NN_SUCCESS ||
        supported == 0 || operationCount != 1 || !supported[0]) {
        WriteLine("NPU NNRT MatMul availability: NOT_SUPPORTED");
        return 0;
    }
    WriteLine("NPU NNRT MatMul availability: PASS");
    return 1;
}

static int RunMatMulOnAccelerator(OH_NNModel *model, size_t deviceId)
{
    static const float kLeft[] = {1.0F, 2.0F, 3.0F, 4.0F, 5.0F, 6.0F};
    static const float kRight[] = {7.0F, 8.0F, 9.0F, 10.0F, 11.0F, 12.0F};
    static const float kExpected[] = {58.0F, 64.0F, 139.0F, 154.0F};
    OH_NNCompilation *compilation = OH_NNCompilation_Construct(model);
    OH_NNExecutor *executor = 0;
    NN_TensorDesc *inputDescs[2] = {0, 0};
    NN_TensorDesc *outputDesc = 0;
    NN_Tensor *inputTensors[2] = {0, 0};
    NN_Tensor *outputTensors[1] = {0};
    size_t inputCount = 0;
    size_t outputCount = 0;
    float *left = 0;
    float *right = 0;
    float *output = 0;
    int passed = 0;

    if (compilation == 0 || OH_NNCompilation_SetDevice(compilation, deviceId) != OH_NN_SUCCESS ||
        OH_NNCompilation_Build(compilation) != OH_NN_SUCCESS) {
        WriteLine("NPU NNRT compilation: FAIL");
        goto cleanup;
    }
    WriteLine("NPU NNRT compilation: PASS");
    executor = OH_NNExecutor_Construct(compilation);
    if (executor == 0 || OH_NNExecutor_GetInputCount(executor, &inputCount) != OH_NN_SUCCESS ||
        OH_NNExecutor_GetOutputCount(executor, &outputCount) != OH_NN_SUCCESS || inputCount != 2 || outputCount != 1) {
        WriteLine("NPU NNRT executor: FAIL");
        goto cleanup;
    }
    inputDescs[0] = OH_NNExecutor_CreateInputTensorDesc(executor, 0);
    inputDescs[1] = OH_NNExecutor_CreateInputTensorDesc(executor, 1);
    outputDesc = OH_NNExecutor_CreateOutputTensorDesc(executor, 0);
    if (inputDescs[0] == 0 || inputDescs[1] == 0 || outputDesc == 0) {
        WriteLine("NPU NNRT tensor descriptor: FAIL");
        goto cleanup;
    }
    inputTensors[0] = OH_NNTensor_Create(deviceId, inputDescs[0]);
    inputTensors[1] = OH_NNTensor_Create(deviceId, inputDescs[1]);
    outputTensors[0] = OH_NNTensor_Create(deviceId, outputDesc);
    if (inputTensors[0] == 0 || inputTensors[1] == 0 || outputTensors[0] == 0) {
        WriteLine("NPU NNRT tensor allocation: FAIL");
        goto cleanup;
    }
    left = (float *)OH_NNTensor_GetDataBuffer(inputTensors[0]);
    right = (float *)OH_NNTensor_GetDataBuffer(inputTensors[1]);
    if (left == 0 || right == 0) {
        WriteLine("NPU NNRT input buffer: FAIL");
        goto cleanup;
    }
    for (Size index = 0; index < 6; ++index) {
        left[index] = kLeft[index];
        right[index] = kRight[index];
    }
    if (OH_NNExecutor_RunSync(executor, inputTensors, 2, outputTensors, 1) != OH_NN_SUCCESS) {
        WriteLine("NPU NNRT execution: FAIL");
        goto cleanup;
    }
    WriteLine("NPU NNRT execution: PASS");
    output = (float *)OH_NNTensor_GetDataBuffer(outputTensors[0]);
    if (output == 0) {
        WriteLine("NPU NNRT result comparison: FAIL");
        goto cleanup;
    }
    for (Size index = 0; index < 4; ++index) {
        float difference = output[index] - kExpected[index];
        if (difference < 0.0F) {
            difference = -difference;
        }
        if (difference > 0.0001F) {
            WriteLine("NPU NNRT result comparison: FAIL");
            goto cleanup;
        }
    }
    WriteLine("NPU NNRT result comparison: PASS");
    passed = 1;

cleanup:
    DestroyTensor(&inputTensors[0]);
    DestroyTensor(&inputTensors[1]);
    DestroyTensor(&outputTensors[0]);
    DestroyDesc(&inputDescs[0]);
    DestroyDesc(&inputDescs[1]);
    DestroyDesc(&outputDesc);
    if (executor != 0) {
        OH_NNExecutor_Destroy(&executor);
    }
    if (compilation != 0) {
        OH_NNCompilation_Destroy(&compilation);
    }
    return passed;
}

extern int __libc_start_main(int (*mainFunction)(int, char **, char **), int argc, char **argv,
    void (*initFunction)(void), void (*finiFunction)(void), void (*loaderFiniFunction)(void));

__attribute__((noreturn)) void NpuSmokeStartC(long *stack)
{
    const int argc = (int)stack[0];
    char **argv = (char **)(stack + 1);
    extern int main(int, char **, char **);
    __libc_start_main(main, argc, argv, 0, 0, 0);
    __builtin_unreachable();
}

__asm__(
    ".text\n"
    ".global _start\n"
    ".type _start,%function\n"
    "_start:\n"
    "  hint #34\n"
    "  mov x29, #0\n"
    "  mov x30, #0\n"
    "  mov x0, sp\n"
    "  and sp, x0, #-16\n"
    "  b NpuSmokeStartC\n");

int main(int argc, char **argv, char **envp)
{
    (void)argc;
    (void)argv;
    (void)envp;
    WriteLine("NPU NNRT smoke stage: process started");

    size_t deviceId = 0;
    OH_NNModel *model = 0;
    const int ready = SelectAccelerator(&deviceId) && BuildMatMulModel(&model) &&
        VerifyOperationAvailability(model, deviceId) && RunMatMulOnAccelerator(model, deviceId);
    if (model != 0) {
        OH_NNModel_Destroy(&model);
    }
    if (ready) {
        WriteLine("NPU NNRT MatMul accelerator offload: PASS");
        return 0;
    }
    WriteLine("NPU NNRT MatMul accelerator offload: FAIL");
    return 1;
}
