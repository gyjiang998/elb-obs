// Vulkan compute offload smoke test for RGBA brightness adjustment.
// Each invocation consumes one 32-bit RGBA pixel (little-endian),
// adds a float bias to R/G/B, clamps to [0,255], preserves A.

// Provide freestanding fixed-width types so Vulkan headers don't need libc.
#define VK_NO_STDDEF_H
#define VK_NO_STDINT_H
typedef __SIZE_TYPE__ size_t;
typedef __PTRDIFF_TYPE__ ptrdiff_t;
typedef signed char int8_t;
typedef unsigned char uint8_t;
typedef signed short int16_t;
typedef unsigned short uint16_t;
typedef signed int int32_t;
typedef unsigned int uint32_t;
typedef signed long int64_t;
typedef unsigned long uint64_t;

#include <vulkan/vulkan.h>

typedef __SIZE_TYPE__ Size;
typedef long ssize_t;

static long SysWrite(int fd, const char *buffer, Size size)
{
    register long x0 __asm__("x0") = fd;
    register const char *x1 __asm__("x1") = buffer;
    register Size x2 __asm__("x2") = size;
    register long x8 __asm__("x8") = 64;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory");
    return x0;
}

static long SysClockGettime(int clock, void *ts)
{
    register long x0 __asm__("x0") = clock;
    register void *x1 __asm__("x1") = ts;
    register long x8 __asm__("x8") = 113;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x8) : "memory");
    return x0;
}

static void SysExit(int code)
{
    register long x0 __asm__("x0") = code;
    register long x8 __asm__("x8") = 93;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8));
    __builtin_unreachable();
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

static void WriteString(const char *value)
{
    SysWrite(1, value, StringLength(value));
}

static void WriteHex(unsigned long value)
{
    char buf[24];
    int i = 0;
    buf[i++] = '0';
    buf[i++] = 'x';
    int start = i;
    if (value == 0) {
        buf[i++] = '0';
    } else {
        int hex[16];
        int n = 0;
        while (value > 0 && n < 16) {
            hex[n++] = (int)(value & 0xF);
            value >>= 4;
        }
        while (n > 0) {
            int d = hex[--n];
            buf[i++] = (char)(d < 10 ? '0' + d : 'a' + d - 10);
        }
    }
    SysWrite(1, buf, (Size)i);
}

static void WriteInt(long value)
{
    char buf[24];
    int i = 0;
    if (value < 0) {
        buf[i++] = '-';
        value = -value;
    }
    if (value == 0) {
        buf[i++] = '0';
    } else {
        char digits[20];
        int n = 0;
        while (value > 0) {
            digits[n++] = (char)('0' + (value % 10));
            value /= 10;
        }
        while (n > 0) {
            buf[i++] = digits[--n];
        }
    }
    SysWrite(1, buf, (Size)i);
}

static void WriteFixedDouble(double value, int decimals)
{
    if (value < 0.0) {
        SysWrite(1, "-", 1);
        value = -value;
    }
    long integer = (long)value;
    WriteInt(integer);
    SysWrite(1, ".", 1);
    double frac = value - (double)integer;
    for (int d = 0; d < decimals; ++d) {
        frac *= 10.0;
        int digit = (int)frac;
        if (digit < 0) digit = 0;
        if (digit > 9) digit = 9;
        char c = (char)('0' + digit);
        SysWrite(1, &c, 1);
        frac -= (double)digit;
    }
}

static double NowMs(void)
{
    struct { long tv_sec; long tv_nsec; } ts;
    if (SysClockGettime(1, &ts) != 0) {
        return 0.0;
    }
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}

enum {
    WIDTH = 1024,
    HEIGHT = 1024,
    PIXELS = WIDTH * HEIGHT,
    BYTES = PIXELS * 4,
    LOCAL_SIZE_X = 64,
    GROUP_COUNT_X = (PIXELS + LOCAL_SIZE_X - 1) / LOCAL_SIZE_X
};

static unsigned char cc_f(float value)
{
    if (value < 0.0F) {
        return 0;
    }
    if (value > 255.0F) {
        return 255;
    }
    return (unsigned char)value;
}

static void aaa_cpu(const unsigned char *src, unsigned char *dst, Size count, float bias)
{
    for (Size pp = 0; pp < count; ++pp) {
        const Size offset = pp * 4;
        dst[offset] = cc_f((float)src[offset] + bias);
        dst[offset + 1] = cc_f((float)src[offset + 1] + bias);
        dst[offset + 2] = cc_f((float)src[offset + 2] + bias);
        dst[offset + 3] = src[offset + 3];
    }
}

static const char *VkResultString(VkResult r)
{
    switch (r) {
        case VK_SUCCESS: return "VK_SUCCESS";
        case VK_NOT_READY: return "VK_NOT_READY";
        case VK_TIMEOUT: return "VK_TIMEOUT";
        case VK_EVENT_SET: return "VK_EVENT_SET";
        case VK_EVENT_RESET: return "VK_EVENT_RESET";
        case VK_INCOMPLETE: return "VK_INCOMPLETE";
        case VK_ERROR_OUT_OF_HOST_MEMORY: return "VK_ERROR_OUT_OF_HOST_MEMORY";
        case VK_ERROR_OUT_OF_DEVICE_MEMORY: return "VK_ERROR_OUT_OF_DEVICE_MEMORY";
        case VK_ERROR_INITIALIZATION_FAILED: return "VK_ERROR_INITIALIZATION_FAILED";
        case VK_ERROR_DEVICE_LOST: return "VK_ERROR_DEVICE_LOST";
        case VK_ERROR_MEMORY_MAP_FAILED: return "VK_ERROR_MEMORY_MAP_FAILED";
        case VK_ERROR_LAYER_NOT_PRESENT: return "VK_ERROR_LAYER_NOT_PRESENT";
        case VK_ERROR_EXTENSION_NOT_PRESENT: return "VK_ERROR_EXTENSION_NOT_PRESENT";
        case VK_ERROR_FEATURE_NOT_PRESENT: return "VK_ERROR_FEATURE_NOT_PRESENT";
        case VK_ERROR_INCOMPATIBLE_DRIVER: return "VK_ERROR_INCOMPATIBLE_DRIVER";
        case VK_ERROR_TOO_MANY_OBJECTS: return "VK_ERROR_TOO_MANY_OBJECTS";
        case VK_ERROR_FORMAT_NOT_SUPPORTED: return "VK_ERROR_FORMAT_NOT_SUPPORTED";
        case VK_ERROR_FRAGMENTED_POOL: return "VK_ERROR_FRAGMENTED_POOL";
        case VK_ERROR_UNKNOWN: return "VK_ERROR_UNKNOWN";
        default: return "UNKNOWN";
    }
}

static int CheckVk(VkResult r, const char *step)
{
    if (r == VK_SUCCESS) {
        return 0;
    }
    WriteString("Vulkan error at ");
    WriteString(step);
    WriteString(": ");
    WriteString(VkResultString(r));
    WriteString(" (");
    WriteHex((unsigned long)(long)r);
    WriteLine(")");
    return 1;
}

// SPIR-V compute shader generated by glslang from brightness.comp
#include "brightness_spv.h"







struct PushConstants {
    float bias;
    unsigned int count;
};

static int FindMemoryType(VkPhysicalDevice physicalDevice, unsigned int typeFilter,
                          VkMemoryPropertyFlags properties, unsigned int *outType)
{
    VkPhysicalDeviceMemoryProperties memProps;
    vkGetPhysicalDeviceMemoryProperties(physicalDevice, &memProps);
    for (unsigned int i = 0; i < memProps.memoryTypeCount; ++i) {
        if ((typeFilter & (1u << i)) &&
            (memProps.memoryTypes[i].propertyFlags & properties) == properties) {
            *outType = i;
            return 0;
        }
    }
    return -1;
}

static int RunVulkan(const unsigned char *src, unsigned char *dst, Size count,
                     float bias, double *outMs)
{
    VkResult r;

    WriteLine("Vulkan: create instance");
    const VkApplicationInfo appInfo = {
        .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "brightness",
        .applicationVersion = VK_MAKE_VERSION(1, 0, 0),
        .pEngineName = "none",
        .engineVersion = VK_MAKE_VERSION(1, 0, 0),
        .apiVersion = VK_API_VERSION_1_0,
    };
    const VkInstanceCreateInfo instanceInfo = {
        .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &appInfo,
    };
    VkInstance instance = 0;
    r = vkCreateInstance(&instanceInfo, 0, &instance);
    if (CheckVk(r, "vkCreateInstance")) return 1;

    WriteLine("Vulkan: enumerate physical devices");
    unsigned int deviceCount = 0;
    r = vkEnumeratePhysicalDevices(instance, &deviceCount, 0);
    if (CheckVk(r, "vkEnumeratePhysicalDevices(count)") || deviceCount == 0) {
        WriteLine("No Vulkan physical device found");
        return 2;
    }
    VkPhysicalDevice physicalDevice = 0;
    r = vkEnumeratePhysicalDevices(instance, &deviceCount, &physicalDevice);
    if (CheckVk(r, "vkEnumeratePhysicalDevices")) return 2;

    VkPhysicalDeviceProperties deviceProps;
    vkGetPhysicalDeviceProperties(physicalDevice, &deviceProps);
    WriteString("Vulkan device: ");
    WriteLine(deviceProps.deviceName);

    WriteLine("Vulkan: find compute queue");
    unsigned int queueFamilyCount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(physicalDevice, &queueFamilyCount, 0);
    VkQueueFamilyProperties queueFamilies[8];
    if (queueFamilyCount > 8) queueFamilyCount = 8;
    vkGetPhysicalDeviceQueueFamilyProperties(physicalDevice, &queueFamilyCount, queueFamilies);
    int queueFamilyIndex = -1;
    for (unsigned int i = 0; i < queueFamilyCount; ++i) {
        if (queueFamilies[i].queueFlags & VK_QUEUE_COMPUTE_BIT) {
            queueFamilyIndex = (int)i;
            break;
        }
    }
    if (queueFamilyIndex < 0) {
        WriteLine("No compute queue family found");
        return 3;
    }

    WriteLine("Vulkan: create device");
    float queuePriority = 1.0F;
    const VkDeviceQueueCreateInfo queueCreateInfo = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = (unsigned int)queueFamilyIndex,
        .queueCount = 1,
        .pQueuePriorities = &queuePriority,
    };
    const VkDeviceCreateInfo deviceCreateInfo = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &queueCreateInfo,
    };
    VkDevice device = 0;
    r = vkCreateDevice(physicalDevice, &deviceCreateInfo, 0, &device);
    if (CheckVk(r, "vkCreateDevice")) return 4;

    VkQueue queue = 0;
    vkGetDeviceQueue(device, (unsigned int)queueFamilyIndex, 0, &queue);

    WriteLine("Vulkan: create descriptor set layout");
    const VkDescriptorSetLayoutBinding bindings[2] = {
        {
            .binding = 0,
            .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .descriptorCount = 1,
            .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT,
        },
        {
            .binding = 1,
            .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .descriptorCount = 1,
            .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT,
        },
    };
    const VkDescriptorSetLayoutCreateInfo setLayoutInfo = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 2,
        .pBindings = bindings,
    };
    VkDescriptorSetLayout setLayout = 0;
    r = vkCreateDescriptorSetLayout(device, &setLayoutInfo, 0, &setLayout);
    if (CheckVk(r, "vkCreateDescriptorSetLayout")) return 5;

    const VkPushConstantRange pushRange = {
        .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT,
        .offset = 0,
        .size = sizeof(struct PushConstants),
    };
    const VkPipelineLayoutCreateInfo pipelineLayoutInfo = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1,
        .pSetLayouts = &setLayout,
        .pushConstantRangeCount = 1,
        .pPushConstantRanges = &pushRange,
    };
    VkPipelineLayout pipelineLayout = 0;
    r = vkCreatePipelineLayout(device, &pipelineLayoutInfo, 0, &pipelineLayout);
    if (CheckVk(r, "vkCreatePipelineLayout")) return 6;

    WriteLine("Vulkan: create shader module");
    const VkShaderModuleCreateInfo shaderInfo = {
        .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = kBrightnessSpvSize,
        .pCode = (const unsigned int *)kBrightnessSpv,
    };
    VkShaderModule shaderModule = 0;
    r = vkCreateShaderModule(device, &shaderInfo, 0, &shaderModule);
    if (CheckVk(r, "vkCreateShaderModule")) return 7;

    WriteLine("Vulkan: create compute pipeline");
    const VkPipelineShaderStageCreateInfo stageInfo = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
        .stage = VK_SHADER_STAGE_COMPUTE_BIT,
        .module = shaderModule,
        .pName = "main",
    };
    const VkComputePipelineCreateInfo pipelineInfo = {
        .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = stageInfo,
        .layout = pipelineLayout,
    };
    VkPipeline pipeline = 0;
    r = vkCreateComputePipelines(device, 0, 1, &pipelineInfo, 0, &pipeline);
    if (CheckVk(r, "vkCreateComputePipelines")) return 8;

    WriteLine("Vulkan: create buffers");
    const VkBufferCreateInfo bufferCreateInfo = {
        .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = BYTES,
        .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
    };
    VkBuffer srcBuffer = 0;
    VkBuffer dstBuffer = 0;
    r = vkCreateBuffer(device, &bufferCreateInfo, 0, &srcBuffer);
    if (CheckVk(r, "vkCreateBuffer(src)")) return 9;
    r = vkCreateBuffer(device, &bufferCreateInfo, 0, &dstBuffer);
    if (CheckVk(r, "vkCreateBuffer(dst)")) return 9;

    VkMemoryRequirements memReqs;
    vkGetBufferMemoryRequirements(device, srcBuffer, &memReqs);
    VkDeviceSize alignedSize = memReqs.size;

    unsigned int memoryTypeIndex = 0;
    if (FindMemoryType(physicalDevice, memReqs.memoryTypeBits,
                       VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                       &memoryTypeIndex) != 0) {
        WriteLine("Failed to find host-visible coherent memory type");
        return 10;
    }

    const VkMemoryAllocateInfo allocInfo = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = alignedSize * 2,
        .memoryTypeIndex = memoryTypeIndex,
    };
    VkDeviceMemory memory = 0;
    r = vkAllocateMemory(device, &allocInfo, 0, &memory);
    if (CheckVk(r, "vkAllocateMemory")) return 11;

    r = vkBindBufferMemory(device, srcBuffer, memory, 0);
    if (CheckVk(r, "vkBindBufferMemory(src)")) return 12;
    r = vkBindBufferMemory(device, dstBuffer, memory, alignedSize);
    if (CheckVk(r, "vkBindBufferMemory(dst)")) return 12;

    void *mapped = 0;
    r = vkMapMemory(device, memory, 0, alignedSize * 2, 0, &mapped);
    if (CheckVk(r, "vkMapMemory")) return 13;
    unsigned char *hostSrc = (unsigned char *)mapped;
    unsigned char *hostDst = hostSrc + alignedSize;

    // Fill source data
    for (Size i = 0; i < BYTES; ++i) {
        hostSrc[i] = src[i];
    }

    WriteLine("Vulkan: create descriptor pool and set");
    const VkDescriptorPoolSize poolSize = {
        .type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .descriptorCount = 2,
    };
    const VkDescriptorPoolCreateInfo poolInfo = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1,
        .poolSizeCount = 1,
        .pPoolSizes = &poolSize,
    };
    VkDescriptorPool descriptorPool = 0;
    r = vkCreateDescriptorPool(device, &poolInfo, 0, &descriptorPool);
    if (CheckVk(r, "vkCreateDescriptorPool")) return 14;

    const VkDescriptorSetAllocateInfo setAllocInfo = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = descriptorPool,
        .descriptorSetCount = 1,
        .pSetLayouts = &setLayout,
    };
    VkDescriptorSet descriptorSet = 0;
    r = vkAllocateDescriptorSets(device, &setAllocInfo, &descriptorSet);
    if (CheckVk(r, "vkAllocateDescriptorSets")) return 15;

    const VkDescriptorBufferInfo srcBufferInfo = {
        .buffer = srcBuffer,
        .offset = 0,
        .range = BYTES,
    };
    const VkDescriptorBufferInfo dstBufferInfo = {
        .buffer = dstBuffer,
        .offset = 0,
        .range = BYTES,
    };
    const VkWriteDescriptorSet writes[2] = {
        {
            .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstSet = descriptorSet,
            .dstBinding = 0,
            .dstArrayElement = 0,
            .descriptorCount = 1,
            .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .pBufferInfo = &srcBufferInfo,
        },
        {
            .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstSet = descriptorSet,
            .dstBinding = 1,
            .dstArrayElement = 0,
            .descriptorCount = 1,
            .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .pBufferInfo = &dstBufferInfo,
        },
    };
    vkUpdateDescriptorSets(device, 2, writes, 0, 0);

    WriteLine("Vulkan: create command pool and buffer");
    const VkCommandPoolCreateInfo cmdPoolInfo = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .queueFamilyIndex = (unsigned int)queueFamilyIndex,
    };
    VkCommandPool cmdPool = 0;
    r = vkCreateCommandPool(device, &cmdPoolInfo, 0, &cmdPool);
    if (CheckVk(r, "vkCreateCommandPool")) return 16;

    const VkCommandBufferAllocateInfo cmdBufAllocInfo = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = cmdPool,
        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
        .commandBufferCount = 1,
    };
    VkCommandBuffer cmdBuf = 0;
    r = vkAllocateCommandBuffers(device, &cmdBufAllocInfo, &cmdBuf);
    if (CheckVk(r, "vkAllocateCommandBuffers")) return 17;

    const VkCommandBufferBeginInfo beginInfo = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
    };
    r = vkBeginCommandBuffer(cmdBuf, &beginInfo);
    if (CheckVk(r, "vkBeginCommandBuffer")) return 18;

    vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
    vkCmdBindDescriptorSets(cmdBuf, VK_PIPELINE_BIND_POINT_COMPUTE, pipelineLayout,
                            0, 1, &descriptorSet, 0, 0);

    struct PushConstants pc;
    pc.bias = bias;
    pc.count = (unsigned int)count;
    vkCmdPushConstants(cmdBuf, pipelineLayout, VK_SHADER_STAGE_COMPUTE_BIT,
                       0, sizeof(pc), &pc);

    WriteLine("Vulkan: dispatch compute");
    vkCmdDispatch(cmdBuf, GROUP_COUNT_X, 1, 1);

    const VkBufferMemoryBarrier barrier = {
        .sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
        .dstAccessMask = VK_ACCESS_HOST_READ_BIT,
        .buffer = dstBuffer,
        .offset = 0,
        .size = BYTES,
    };
    vkCmdPipelineBarrier(cmdBuf,
                         VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                         VK_PIPELINE_STAGE_HOST_BIT,
                         0,
                         0, 0,
                         1, &barrier,
                         0, 0);

    r = vkEndCommandBuffer(cmdBuf);
    if (CheckVk(r, "vkEndCommandBuffer")) return 19;

    const VkSubmitInfo submitInfo = {
        .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount = 1,
        .pCommandBuffers = &cmdBuf,
    };

    double t0 = NowMs();
    r = vkQueueSubmit(queue, 1, &submitInfo, 0);
    if (CheckVk(r, "vkQueueSubmit")) return 20;
    r = vkQueueWaitIdle(queue);
    if (CheckVk(r, "vkQueueWaitIdle")) return 21;
    double t1 = NowMs();
    *outMs = t1 - t0;

    // Copy result out
    for (Size i = 0; i < BYTES; ++i) {
        dst[i] = hostDst[i];
    }

    // Cleanup
    vkUnmapMemory(device, memory);
    vkDestroyDescriptorPool(device, descriptorPool, 0);
    vkDestroyCommandPool(device, cmdPool, 0);
    vkDestroyPipeline(device, pipeline, 0);
    vkDestroyShaderModule(device, shaderModule, 0);
    vkDestroyPipelineLayout(device, pipelineLayout, 0);
    vkDestroyDescriptorSetLayout(device, setLayout, 0);
    vkDestroyBuffer(device, srcBuffer, 0);
    vkDestroyBuffer(device, dstBuffer, 0);
    vkFreeMemory(device, memory, 0);
    vkDestroyDevice(device, 0);
    vkDestroyInstance(instance, 0);
    return 0;
}

extern int __libc_start_main(int (*mainFunction)(int, char **, char **), int argc, char **argv,
    void (*initFunction)(void), void (*finiFunction)(void), void (*loaderFiniFunction)(void));

__attribute__((noreturn)) void VulkanSmokeStartC(long *stack)
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
    "  b VulkanSmokeStartC\n");

static unsigned char gSrc[BYTES];
static unsigned char gCpuDst[BYTES];
static unsigned char gGpuDst[BYTES];

int main(int argc, char **argv, char **envp)
{
    (void)argc;
    (void)argv;
    (void)envp;

    WriteLine("Vulkan RGBA brightness smoke test");

    // Fill source with deterministic pattern
    for (Size i = 0; i < BYTES; ++i) {
        gSrc[i] = (unsigned char)((i * 17 + 83) & 0xFF);
    }
    const float bias = 42.5F;

    // CPU reference
    double t0 = NowMs();
    int cpuIter = 10;
    for (int i = 0; i < cpuIter; ++i) {
        aaa_cpu(gSrc, gCpuDst, PIXELS, bias);
    }
    double t1 = NowMs();
    double cpuMs = (t1 - t0) / cpuIter;
    WriteString("CPU reference: ");
    WriteFixedDouble(cpuMs, 3);
    WriteLine(" ms (avg over 10 iters)");

    // GPU offload
    double gpuMs = 0.0;
    int gpuOk = RunVulkan(gSrc, gGpuDst, PIXELS, bias, &gpuMs);
    if (gpuOk != 0) {
        WriteLine("GPU path failed");
        return gpuOk;
    }
    WriteString("GPU dispatch: ");
    WriteFixedDouble(gpuMs, 3);
    WriteLine(" ms (dispatch + wait)");

    // Verify
    Size diff = 0;
    Size firstDiff = 0;
    for (Size i = 0; i < BYTES; ++i) {
        if (gCpuDst[i] != gGpuDst[i]) {
            if (diff == 0) {
                firstDiff = i;
            }
            ++diff;
        }
    }

    if (diff == 0) {
        WriteLine("RESULT: CPU/GPU outputs match");
        WriteString("SPEEDUP: ");
        if (gpuMs > 0.0) {
            WriteFixedDouble(cpuMs / gpuMs, 2);
        } else {
            WriteString("N/A");
        }
        WriteLine("x");
        WriteLine("Vulkan RGBA brightness offload: PASS");
        return 0;
    }

    WriteString("RESULT: mismatch count = ");
    WriteInt((long)diff);
    WriteString(" first at byte ");
    WriteInt((long)firstDiff);
    WriteString(" cpu=");
    WriteInt((long)gCpuDst[firstDiff]);
    WriteString(" gpu=");
    WriteInt((long)gGpuDst[firstDiff]);
    WriteLine("");
    WriteLine("Vulkan RGBA brightness offload: FAIL");
    return 100;
}
