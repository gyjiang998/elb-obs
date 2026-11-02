#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "experiment_shader_spv.h"

/*
 * Curated template boundary:
 * - Keep the Vulkan loader, resource lifecycle, dispatch and cleanup scaffolding.
 * - Replace the problem contract below, CpuBrightness(), PushConstants and the
 *   paired kernel_template.comp for the current hotspot.
 * - Keep a deterministic CPU oracle and compare every output element before
 *   adding a performance claim.
 */

enum {
    WIDTH = 1024,
    HEIGHT = 1024,
    PIXEL_COUNT = WIDTH * HEIGHT,
    CHANNEL_COUNT = 4,
    BUFFER_BYTES = PIXEL_COUNT * CHANNEL_COUNT,
    LOCAL_SIZE_X = 64,
    GROUP_COUNT_X = (PIXEL_COUNT + LOCAL_SIZE_X - 1) / LOCAL_SIZE_X,
    CPU_WARMUP_COUNT = 5,
    CPU_SAMPLE_COUNT = 25,
    GPU_WARMUP_COUNT = 5,
    GPU_SAMPLE_COUNT = 25,
};

static uint8_t g_source[BUFFER_BYTES];
static uint8_t g_cpu_result[BUFFER_BYTES];
static uint8_t g_gpu_result[BUFFER_BYTES];

typedef struct VulkanApi {
    void *library;
    PFN_vkGetInstanceProcAddr GetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr GetDeviceProcAddr;
    PFN_vkCreateInstance CreateInstance;
    PFN_vkDestroyInstance DestroyInstance;
    PFN_vkEnumeratePhysicalDevices EnumeratePhysicalDevices;
    PFN_vkGetPhysicalDeviceProperties GetPhysicalDeviceProperties;
    PFN_vkGetPhysicalDeviceQueueFamilyProperties GetPhysicalDeviceQueueFamilyProperties;
    PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties;
    PFN_vkCreateDevice CreateDevice;
    PFN_vkDestroyDevice DestroyDevice;
    PFN_vkGetDeviceQueue GetDeviceQueue;
    PFN_vkCreateDescriptorSetLayout CreateDescriptorSetLayout;
    PFN_vkDestroyDescriptorSetLayout DestroyDescriptorSetLayout;
    PFN_vkCreatePipelineLayout CreatePipelineLayout;
    PFN_vkDestroyPipelineLayout DestroyPipelineLayout;
    PFN_vkCreateShaderModule CreateShaderModule;
    PFN_vkDestroyShaderModule DestroyShaderModule;
    PFN_vkCreateComputePipelines CreateComputePipelines;
    PFN_vkDestroyPipeline DestroyPipeline;
    PFN_vkCreateBuffer CreateBuffer;
    PFN_vkDestroyBuffer DestroyBuffer;
    PFN_vkGetBufferMemoryRequirements GetBufferMemoryRequirements;
    PFN_vkAllocateMemory AllocateMemory;
    PFN_vkFreeMemory FreeMemory;
    PFN_vkBindBufferMemory BindBufferMemory;
    PFN_vkMapMemory MapMemory;
    PFN_vkUnmapMemory UnmapMemory;
    PFN_vkCreateDescriptorPool CreateDescriptorPool;
    PFN_vkDestroyDescriptorPool DestroyDescriptorPool;
    PFN_vkAllocateDescriptorSets AllocateDescriptorSets;
    PFN_vkUpdateDescriptorSets UpdateDescriptorSets;
    PFN_vkCreateCommandPool CreateCommandPool;
    PFN_vkDestroyCommandPool DestroyCommandPool;
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers;
    PFN_vkBeginCommandBuffer BeginCommandBuffer;
    PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkCmdBindPipeline CmdBindPipeline;
    PFN_vkCmdBindDescriptorSets CmdBindDescriptorSets;
    PFN_vkCmdPushConstants CmdPushConstants;
    PFN_vkCmdDispatch CmdDispatch;
    PFN_vkCmdPipelineBarrier CmdPipelineBarrier;
    PFN_vkQueueSubmit QueueSubmit;
    PFN_vkQueueWaitIdle QueueWaitIdle;
} VulkanApi;

typedef struct VulkanContext {
    VulkanApi api;
    VkInstance instance;
    VkPhysicalDevice physical_device;
    VkDevice device;
    VkQueue queue;
    uint32_t queue_family_index;
    VkDescriptorSetLayout descriptor_set_layout;
    VkPipelineLayout pipeline_layout;
    VkShaderModule shader_module;
    VkPipeline pipeline;
    VkBuffer source_buffer;
    VkBuffer destination_buffer;
    VkDeviceMemory memory;
    VkDeviceSize buffer_stride;
    uint8_t *mapped_source;
    uint8_t *mapped_destination;
    VkDescriptorPool descriptor_pool;
    VkDescriptorSet descriptor_set;
    VkCommandPool command_pool;
    VkCommandBuffer command_buffer;
    char device_name[VK_MAX_PHYSICAL_DEVICE_NAME_SIZE];
} VulkanContext;

typedef struct PushConstants {
    float bias;
    uint32_t count;
} PushConstants;

static double NowMilliseconds(void)
{
    struct timeval time_value;
    if (gettimeofday(&time_value, NULL) != 0) {
        return 0.0;
    }
    return (double)time_value.tv_sec * 1000.0 + (double)time_value.tv_usec / 1000.0;
}

static int CompareDouble(const void *left, const void *right)
{
    const double a = *(const double *)left;
    const double b = *(const double *)right;
    return (a > b) - (a < b);
}

static double Median(double *samples, size_t count)
{
    qsort(samples, count, sizeof(samples[0]), CompareDouble);
    if ((count & 1u) != 0u) {
        return samples[count / 2u];
    }
    return (samples[count / 2u - 1u] + samples[count / 2u]) * 0.5;
}

static double Percentile95(const double *sorted_samples, size_t count)
{
    size_t index = (count * 95u + 99u) / 100u;
    if (index == 0u) {
        return sorted_samples[0];
    }
    if (index > count) {
        index = count;
    }
    return sorted_samples[index - 1u];
}

static uint8_t ClampChannel(float value)
{
    if (value < 0.0F) {
        return 0;
    }
    if (value > 255.0F) {
        return 255;
    }
    return (uint8_t)value;
}

__attribute__((noinline)) static void CpuBrightness(
    const uint8_t *source, uint8_t *destination, size_t pixel_count, float bias)
{
    for (size_t pixel = 0; pixel < pixel_count; ++pixel) {
        const size_t offset = pixel * CHANNEL_COUNT;
        destination[offset] = ClampChannel((float)source[offset] + bias);
        destination[offset + 1u] = ClampChannel((float)source[offset + 1u] + bias);
        destination[offset + 2u] = ClampChannel((float)source[offset + 2u] + bias);
        destination[offset + 3u] = source[offset + 3u];
    }
}

static int CheckVk(VkResult result, const char *operation)
{
    if (result == VK_SUCCESS) {
        return 0;
    }
    fprintf(stderr, "Vulkan call failed: %s returned %d\n", operation, (int)result);
    return -1;
}

static void *LoadSymbol(void *library, const char *name)
{
    void *symbol = dlsym(library, name);
    if (symbol == NULL) {
        fprintf(stderr, "Vulkan loader symbol missing: %s\n", name);
    }
    return symbol;
}

#define LOAD_INSTANCE(api, instance, member, symbol) \
    do { \
        (api)->member = (PFN_##symbol)(api)->GetInstanceProcAddr((instance), #symbol); \
        if ((api)->member == NULL) { \
            fprintf(stderr, "Vulkan instance symbol missing: %s\n", #symbol); \
            return -1; \
        } \
    } while (0)

#define LOAD_DEVICE(api, device, member, symbol) \
    do { \
        (api)->member = (PFN_##symbol)(api)->GetDeviceProcAddr((device), #symbol); \
        if ((api)->member == NULL) { \
            fprintf(stderr, "Vulkan device symbol missing: %s\n", #symbol); \
            return -1; \
        } \
    } while (0)

static int LoadVulkanLibrary(VulkanApi *api)
{
    memset(api, 0, sizeof(*api));
    api->library = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    if (api->library == NULL) {
        fprintf(stderr, "dlopen(libvulkan.so) failed: %s\n", dlerror());
        return -1;
    }
    api->GetInstanceProcAddr = (PFN_vkGetInstanceProcAddr)LoadSymbol(api->library, "vkGetInstanceProcAddr");
    if (api->GetInstanceProcAddr == NULL) {
        return -1;
    }
    api->CreateInstance = (PFN_vkCreateInstance)api->GetInstanceProcAddr(VK_NULL_HANDLE, "vkCreateInstance");
    if (api->CreateInstance == NULL) {
        api->CreateInstance = (PFN_vkCreateInstance)LoadSymbol(api->library, "vkCreateInstance");
    }
    return api->CreateInstance == NULL ? -1 : 0;
}

static int LoadInstanceFunctions(VulkanApi *api, VkInstance instance)
{
    LOAD_INSTANCE(api, instance, DestroyInstance, vkDestroyInstance);
    LOAD_INSTANCE(api, instance, EnumeratePhysicalDevices, vkEnumeratePhysicalDevices);
    LOAD_INSTANCE(api, instance, GetPhysicalDeviceProperties, vkGetPhysicalDeviceProperties);
    LOAD_INSTANCE(api, instance, GetPhysicalDeviceQueueFamilyProperties, vkGetPhysicalDeviceQueueFamilyProperties);
    LOAD_INSTANCE(api, instance, GetPhysicalDeviceMemoryProperties, vkGetPhysicalDeviceMemoryProperties);
    LOAD_INSTANCE(api, instance, CreateDevice, vkCreateDevice);
    LOAD_INSTANCE(api, instance, GetDeviceProcAddr, vkGetDeviceProcAddr);
    return 0;
}

static int LoadDeviceFunctions(VulkanApi *api, VkDevice device)
{
    LOAD_DEVICE(api, device, DestroyDevice, vkDestroyDevice);
    LOAD_DEVICE(api, device, GetDeviceQueue, vkGetDeviceQueue);
    LOAD_DEVICE(api, device, CreateDescriptorSetLayout, vkCreateDescriptorSetLayout);
    LOAD_DEVICE(api, device, DestroyDescriptorSetLayout, vkDestroyDescriptorSetLayout);
    LOAD_DEVICE(api, device, CreatePipelineLayout, vkCreatePipelineLayout);
    LOAD_DEVICE(api, device, DestroyPipelineLayout, vkDestroyPipelineLayout);
    LOAD_DEVICE(api, device, CreateShaderModule, vkCreateShaderModule);
    LOAD_DEVICE(api, device, DestroyShaderModule, vkDestroyShaderModule);
    LOAD_DEVICE(api, device, CreateComputePipelines, vkCreateComputePipelines);
    LOAD_DEVICE(api, device, DestroyPipeline, vkDestroyPipeline);
    LOAD_DEVICE(api, device, CreateBuffer, vkCreateBuffer);
    LOAD_DEVICE(api, device, DestroyBuffer, vkDestroyBuffer);
    LOAD_DEVICE(api, device, GetBufferMemoryRequirements, vkGetBufferMemoryRequirements);
    LOAD_DEVICE(api, device, AllocateMemory, vkAllocateMemory);
    LOAD_DEVICE(api, device, FreeMemory, vkFreeMemory);
    LOAD_DEVICE(api, device, BindBufferMemory, vkBindBufferMemory);
    LOAD_DEVICE(api, device, MapMemory, vkMapMemory);
    LOAD_DEVICE(api, device, UnmapMemory, vkUnmapMemory);
    LOAD_DEVICE(api, device, CreateDescriptorPool, vkCreateDescriptorPool);
    LOAD_DEVICE(api, device, DestroyDescriptorPool, vkDestroyDescriptorPool);
    LOAD_DEVICE(api, device, AllocateDescriptorSets, vkAllocateDescriptorSets);
    LOAD_DEVICE(api, device, UpdateDescriptorSets, vkUpdateDescriptorSets);
    LOAD_DEVICE(api, device, CreateCommandPool, vkCreateCommandPool);
    LOAD_DEVICE(api, device, DestroyCommandPool, vkDestroyCommandPool);
    LOAD_DEVICE(api, device, AllocateCommandBuffers, vkAllocateCommandBuffers);
    LOAD_DEVICE(api, device, BeginCommandBuffer, vkBeginCommandBuffer);
    LOAD_DEVICE(api, device, EndCommandBuffer, vkEndCommandBuffer);
    LOAD_DEVICE(api, device, CmdBindPipeline, vkCmdBindPipeline);
    LOAD_DEVICE(api, device, CmdBindDescriptorSets, vkCmdBindDescriptorSets);
    LOAD_DEVICE(api, device, CmdPushConstants, vkCmdPushConstants);
    LOAD_DEVICE(api, device, CmdDispatch, vkCmdDispatch);
    LOAD_DEVICE(api, device, CmdPipelineBarrier, vkCmdPipelineBarrier);
    LOAD_DEVICE(api, device, QueueSubmit, vkQueueSubmit);
    LOAD_DEVICE(api, device, QueueWaitIdle, vkQueueWaitIdle);
    return 0;
}

static int FindMemoryType(
    const VulkanContext *context, uint32_t type_bits, VkMemoryPropertyFlags properties, uint32_t *result)
{
    VkPhysicalDeviceMemoryProperties memory_properties;
    context->api.GetPhysicalDeviceMemoryProperties(context->physical_device, &memory_properties);
    for (uint32_t index = 0; index < memory_properties.memoryTypeCount; ++index) {
        const VkMemoryPropertyFlags flags = memory_properties.memoryTypes[index].propertyFlags;
        if ((type_bits & (1u << index)) != 0u && (flags & properties) == properties) {
            *result = index;
            return 0;
        }
    }
    return -1;
}

static int CreateInstanceAndDevice(VulkanContext *context)
{
    const VkApplicationInfo application_info = {
        .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "gpu-vulkan-brightness-test",
        .applicationVersion = VK_MAKE_VERSION(1, 0, 0),
        .pEngineName = "heterogeneous-enable-harness",
        .engineVersion = VK_MAKE_VERSION(1, 0, 0),
        .apiVersion = VK_API_VERSION_1_0,
    };
    const VkInstanceCreateInfo instance_info = {
        .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &application_info,
    };
    if (CheckVk(context->api.CreateInstance(&instance_info, NULL, &context->instance), "vkCreateInstance") != 0) {
        return -1;
    }
    if (LoadInstanceFunctions(&context->api, context->instance) != 0) {
        return -1;
    }

    uint32_t physical_device_count = 0;
    if (CheckVk(context->api.EnumeratePhysicalDevices(
            context->instance, &physical_device_count, NULL), "vkEnumeratePhysicalDevices(count)") != 0 ||
        physical_device_count == 0u) {
        fprintf(stderr, "No Vulkan physical device is available\n");
        return -1;
    }
    VkPhysicalDevice devices[8];
    if (physical_device_count > 8u) {
        physical_device_count = 8u;
    }
    if (CheckVk(context->api.EnumeratePhysicalDevices(
            context->instance, &physical_device_count, devices), "vkEnumeratePhysicalDevices") != 0) {
        return -1;
    }
    context->physical_device = devices[0];

    VkPhysicalDeviceProperties device_properties;
    context->api.GetPhysicalDeviceProperties(context->physical_device, &device_properties);
    snprintf(context->device_name, sizeof(context->device_name), "%s", device_properties.deviceName);

    uint32_t queue_family_count = 0;
    context->api.GetPhysicalDeviceQueueFamilyProperties(
        context->physical_device, &queue_family_count, NULL);
    if (queue_family_count == 0u) {
        fprintf(stderr, "The Vulkan device exposes no queue family\n");
        return -1;
    }
    VkQueueFamilyProperties queue_families[32];
    if (queue_family_count > 32u) {
        queue_family_count = 32u;
    }
    context->api.GetPhysicalDeviceQueueFamilyProperties(
        context->physical_device, &queue_family_count, queue_families);
    context->queue_family_index = UINT32_MAX;
    for (uint32_t index = 0; index < queue_family_count; ++index) {
        if ((queue_families[index].queueFlags & VK_QUEUE_COMPUTE_BIT) != 0u) {
            context->queue_family_index = index;
            break;
        }
    }
    if (context->queue_family_index == UINT32_MAX) {
        fprintf(stderr, "The Vulkan device exposes no compute queue\n");
        return -1;
    }

    const float queue_priority = 1.0F;
    const VkDeviceQueueCreateInfo queue_info = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = context->queue_family_index,
        .queueCount = 1,
        .pQueuePriorities = &queue_priority,
    };
    const VkDeviceCreateInfo device_info = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &queue_info,
    };
    if (CheckVk(context->api.CreateDevice(
            context->physical_device, &device_info, NULL, &context->device), "vkCreateDevice") != 0) {
        return -1;
    }
    if (LoadDeviceFunctions(&context->api, context->device) != 0) {
        return -1;
    }
    context->api.GetDeviceQueue(context->device, context->queue_family_index, 0, &context->queue);
    return 0;
}

static int CreatePipeline(VulkanContext *context)
{
    const VkDescriptorSetLayoutBinding bindings[] = {
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
    const VkDescriptorSetLayoutCreateInfo descriptor_layout_info = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 2,
        .pBindings = bindings,
    };
    if (CheckVk(context->api.CreateDescriptorSetLayout(
            context->device, &descriptor_layout_info, NULL, &context->descriptor_set_layout),
            "vkCreateDescriptorSetLayout") != 0) {
        return -1;
    }

    const VkPushConstantRange push_constant_range = {
        .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT,
        .offset = 0,
        .size = sizeof(PushConstants),
    };
    const VkPipelineLayoutCreateInfo pipeline_layout_info = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1,
        .pSetLayouts = &context->descriptor_set_layout,
        .pushConstantRangeCount = 1,
        .pPushConstantRanges = &push_constant_range,
    };
    if (CheckVk(context->api.CreatePipelineLayout(
            context->device, &pipeline_layout_info, NULL, &context->pipeline_layout),
            "vkCreatePipelineLayout") != 0) {
        return -1;
    }

    const VkShaderModuleCreateInfo shader_info = {
        .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = kExperimentShaderSpvSize,
        .pCode = (const uint32_t *)kExperimentShaderSpv,
    };
    if (CheckVk(context->api.CreateShaderModule(
            context->device, &shader_info, NULL, &context->shader_module), "vkCreateShaderModule") != 0) {
        return -1;
    }
    const VkPipelineShaderStageCreateInfo shader_stage = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
        .stage = VK_SHADER_STAGE_COMPUTE_BIT,
        .module = context->shader_module,
        .pName = "main",
    };
    const VkComputePipelineCreateInfo pipeline_info = {
        .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = shader_stage,
        .layout = context->pipeline_layout,
    };
    return CheckVk(context->api.CreateComputePipelines(
        context->device, VK_NULL_HANDLE, 1, &pipeline_info, NULL, &context->pipeline),
        "vkCreateComputePipelines");
}

static int CreateBuffersAndDescriptors(VulkanContext *context)
{
    const VkBufferCreateInfo buffer_info = {
        .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = BUFFER_BYTES,
        .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
    };
    if (CheckVk(context->api.CreateBuffer(
            context->device, &buffer_info, NULL, &context->source_buffer), "vkCreateBuffer(source)") != 0 ||
        CheckVk(context->api.CreateBuffer(
            context->device, &buffer_info, NULL, &context->destination_buffer),
            "vkCreateBuffer(destination)") != 0) {
        return -1;
    }

    VkMemoryRequirements source_requirements;
    VkMemoryRequirements destination_requirements;
    context->api.GetBufferMemoryRequirements(context->device, context->source_buffer, &source_requirements);
    context->api.GetBufferMemoryRequirements(
        context->device, context->destination_buffer, &destination_requirements);
    context->buffer_stride = source_requirements.size;
    if (context->buffer_stride < destination_requirements.alignment) {
        context->buffer_stride = destination_requirements.alignment;
    }
    const VkDeviceSize alignment = destination_requirements.alignment;
    context->buffer_stride = (context->buffer_stride + alignment - 1u) / alignment * alignment;

    uint32_t memory_type_index = 0;
    const uint32_t compatible_types = source_requirements.memoryTypeBits & destination_requirements.memoryTypeBits;
    if (FindMemoryType(context, compatible_types,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            &memory_type_index) != 0) {
        fprintf(stderr, "No host-visible coherent Vulkan memory type is available\n");
        return -1;
    }
    const VkMemoryAllocateInfo allocation_info = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = context->buffer_stride + destination_requirements.size,
        .memoryTypeIndex = memory_type_index,
    };
    if (CheckVk(context->api.AllocateMemory(
            context->device, &allocation_info, NULL, &context->memory), "vkAllocateMemory") != 0 ||
        CheckVk(context->api.BindBufferMemory(
            context->device, context->source_buffer, context->memory, 0), "vkBindBufferMemory(source)") != 0 ||
        CheckVk(context->api.BindBufferMemory(
            context->device, context->destination_buffer, context->memory, context->buffer_stride),
            "vkBindBufferMemory(destination)") != 0) {
        return -1;
    }
    void *mapped_memory = NULL;
    if (CheckVk(context->api.MapMemory(context->device, context->memory, 0,
            allocation_info.allocationSize, 0, &mapped_memory), "vkMapMemory") != 0) {
        return -1;
    }
    context->mapped_source = (uint8_t *)mapped_memory;
    context->mapped_destination = context->mapped_source + context->buffer_stride;

    const VkDescriptorPoolSize pool_size = {
        .type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .descriptorCount = 2,
    };
    const VkDescriptorPoolCreateInfo pool_info = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1,
        .poolSizeCount = 1,
        .pPoolSizes = &pool_size,
    };
    if (CheckVk(context->api.CreateDescriptorPool(
            context->device, &pool_info, NULL, &context->descriptor_pool), "vkCreateDescriptorPool") != 0) {
        return -1;
    }
    const VkDescriptorSetAllocateInfo descriptor_allocate_info = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = context->descriptor_pool,
        .descriptorSetCount = 1,
        .pSetLayouts = &context->descriptor_set_layout,
    };
    if (CheckVk(context->api.AllocateDescriptorSets(
            context->device, &descriptor_allocate_info, &context->descriptor_set),
            "vkAllocateDescriptorSets") != 0) {
        return -1;
    }
    const VkDescriptorBufferInfo source_descriptor = {
        .buffer = context->source_buffer,
        .offset = 0,
        .range = BUFFER_BYTES,
    };
    const VkDescriptorBufferInfo destination_descriptor = {
        .buffer = context->destination_buffer,
        .offset = 0,
        .range = BUFFER_BYTES,
    };
    const VkWriteDescriptorSet descriptor_writes[] = {
        {
            .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstSet = context->descriptor_set,
            .dstBinding = 0,
            .descriptorCount = 1,
            .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .pBufferInfo = &source_descriptor,
        },
        {
            .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstSet = context->descriptor_set,
            .dstBinding = 1,
            .descriptorCount = 1,
            .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .pBufferInfo = &destination_descriptor,
        },
    };
    context->api.UpdateDescriptorSets(context->device, 2, descriptor_writes, 0, NULL);
    return 0;
}

static int RecordDispatch(VulkanContext *context, float bias)
{
    const VkCommandPoolCreateInfo command_pool_info = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .queueFamilyIndex = context->queue_family_index,
    };
    if (CheckVk(context->api.CreateCommandPool(
            context->device, &command_pool_info, NULL, &context->command_pool), "vkCreateCommandPool") != 0) {
        return -1;
    }
    const VkCommandBufferAllocateInfo command_buffer_info = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = context->command_pool,
        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
        .commandBufferCount = 1,
    };
    if (CheckVk(context->api.AllocateCommandBuffers(
            context->device, &command_buffer_info, &context->command_buffer),
            "vkAllocateCommandBuffers") != 0) {
        return -1;
    }
    const VkCommandBufferBeginInfo begin_info = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags = VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT,
    };
    if (CheckVk(context->api.BeginCommandBuffer(context->command_buffer, &begin_info),
            "vkBeginCommandBuffer") != 0) {
        return -1;
    }
    context->api.CmdBindPipeline(
        context->command_buffer, VK_PIPELINE_BIND_POINT_COMPUTE, context->pipeline);
    context->api.CmdBindDescriptorSets(context->command_buffer, VK_PIPELINE_BIND_POINT_COMPUTE,
        context->pipeline_layout, 0, 1, &context->descriptor_set, 0, NULL);
    const PushConstants push_constants = {
        .bias = bias,
        .count = PIXEL_COUNT,
    };
    context->api.CmdPushConstants(context->command_buffer, context->pipeline_layout,
        VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(push_constants), &push_constants);

    context->api.CmdDispatch(context->command_buffer, GROUP_COUNT_X, 1, 1);

    const VkBufferMemoryBarrier barrier = {
        .sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
        .dstAccessMask = VK_ACCESS_HOST_READ_BIT,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .buffer = context->destination_buffer,
        .offset = 0,
        .size = BUFFER_BYTES,
    };
    context->api.CmdPipelineBarrier(context->command_buffer,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0,
        0, NULL, 1, &barrier, 0, NULL);
    return CheckVk(context->api.EndCommandBuffer(context->command_buffer), "vkEndCommandBuffer");
}

static int InitializeVulkan(VulkanContext *context, float bias)
{
    memset(context, 0, sizeof(*context));
    if (LoadVulkanLibrary(&context->api) != 0 ||
        CreateInstanceAndDevice(context) != 0 ||
        CreatePipeline(context) != 0 ||
        CreateBuffersAndDescriptors(context) != 0 ||
        RecordDispatch(context, bias) != 0) {
        return -1;
    }
    return 0;
}

static int SubmitDispatch(VulkanContext *context)
{
    const VkSubmitInfo submit_info = {
        .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount = 1,
        .pCommandBuffers = &context->command_buffer,
    };
    if (CheckVk(context->api.QueueSubmit(context->queue, 1, &submit_info, VK_NULL_HANDLE),
            "vkQueueSubmit") != 0) {
        return -1;
    }
    return CheckVk(context->api.QueueWaitIdle(context->queue), "vkQueueWaitIdle");
}

static void DestroyVulkan(VulkanContext *context)
{
    VulkanApi *api = &context->api;
    if (context->device != VK_NULL_HANDLE && api->QueueWaitIdle != NULL && context->queue != VK_NULL_HANDLE) {
        (void)api->QueueWaitIdle(context->queue);
    }
    if (context->device != VK_NULL_HANDLE && context->memory != VK_NULL_HANDLE && api->UnmapMemory != NULL &&
        context->mapped_source != NULL) {
        api->UnmapMemory(context->device, context->memory);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyCommandPool != NULL && context->command_pool != VK_NULL_HANDLE) {
        api->DestroyCommandPool(context->device, context->command_pool, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyDescriptorPool != NULL &&
        context->descriptor_pool != VK_NULL_HANDLE) {
        api->DestroyDescriptorPool(context->device, context->descriptor_pool, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyBuffer != NULL) {
        if (context->source_buffer != VK_NULL_HANDLE) {
            api->DestroyBuffer(context->device, context->source_buffer, NULL);
        }
        if (context->destination_buffer != VK_NULL_HANDLE) {
            api->DestroyBuffer(context->device, context->destination_buffer, NULL);
        }
    }
    if (context->device != VK_NULL_HANDLE && api->FreeMemory != NULL && context->memory != VK_NULL_HANDLE) {
        api->FreeMemory(context->device, context->memory, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyPipeline != NULL && context->pipeline != VK_NULL_HANDLE) {
        api->DestroyPipeline(context->device, context->pipeline, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyShaderModule != NULL &&
        context->shader_module != VK_NULL_HANDLE) {
        api->DestroyShaderModule(context->device, context->shader_module, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyPipelineLayout != NULL &&
        context->pipeline_layout != VK_NULL_HANDLE) {
        api->DestroyPipelineLayout(context->device, context->pipeline_layout, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyDescriptorSetLayout != NULL &&
        context->descriptor_set_layout != VK_NULL_HANDLE) {
        api->DestroyDescriptorSetLayout(context->device, context->descriptor_set_layout, NULL);
    }
    if (context->device != VK_NULL_HANDLE && api->DestroyDevice != NULL) {
        api->DestroyDevice(context->device, NULL);
    }
    if (context->instance != VK_NULL_HANDLE && api->DestroyInstance != NULL) {
        api->DestroyInstance(context->instance, NULL);
    }
    if (api->library != NULL) {
        dlclose(api->library);
    }
}

static int RunBrightnessValidation(void)
{
    const float bias = 42.5F;
    for (size_t index = 0; index < BUFFER_BYTES; ++index) {
        g_source[index] = (uint8_t)((index * 17u + 83u) & 0xffu);
    }

    for (int iteration = 0; iteration < CPU_WARMUP_COUNT; ++iteration) {
        CpuBrightness(g_source, g_cpu_result, PIXEL_COUNT, bias);
    }
    double cpu_samples[CPU_SAMPLE_COUNT];
    for (int iteration = 0; iteration < CPU_SAMPLE_COUNT; ++iteration) {
        const double start = NowMilliseconds();
        CpuBrightness(g_source, g_cpu_result, PIXEL_COUNT, bias);
        cpu_samples[iteration] = NowMilliseconds() - start;
    }

    VulkanContext context;
    const double setup_start = NowMilliseconds();
    if (InitializeVulkan(&context, bias) != 0) {
        DestroyVulkan(&context);
        return -1;
    }
    memcpy(context.mapped_source, g_source, BUFFER_BYTES);
    memset(context.mapped_destination, 0, BUFFER_BYTES);
    const double setup_ms = NowMilliseconds() - setup_start;

    for (int iteration = 0; iteration < GPU_WARMUP_COUNT; ++iteration) {
        if (SubmitDispatch(&context) != 0) {
            DestroyVulkan(&context);
            return -1;
        }
    }
    double gpu_dispatch_samples[GPU_SAMPLE_COUNT];
    double gpu_end_to_end_samples[GPU_SAMPLE_COUNT];
    for (int iteration = 0; iteration < GPU_SAMPLE_COUNT; ++iteration) {
        double start = NowMilliseconds();
        if (SubmitDispatch(&context) != 0) {
            DestroyVulkan(&context);
            return -1;
        }
        gpu_dispatch_samples[iteration] = NowMilliseconds() - start;

        start = NowMilliseconds();
        memcpy(context.mapped_source, g_source, BUFFER_BYTES);
        if (SubmitDispatch(&context) != 0) {
            DestroyVulkan(&context);
            return -1;
        }
        memcpy(g_gpu_result, context.mapped_destination, BUFFER_BYTES);
        gpu_end_to_end_samples[iteration] = NowMilliseconds() - start;
    }
    memcpy(g_gpu_result, context.mapped_destination, BUFFER_BYTES);

    size_t mismatch_count = 0;
    size_t first_mismatch = 0;
    for (size_t index = 0; index < BUFFER_BYTES; ++index) {
        if (g_cpu_result[index] != g_gpu_result[index]) {
            if (mismatch_count == 0u) {
                first_mismatch = index;
            }
            ++mismatch_count;
        }
    }

    const double cpu_median_ms = Median(cpu_samples, CPU_SAMPLE_COUNT);
    const double cpu_p95_ms = Percentile95(cpu_samples, CPU_SAMPLE_COUNT);
    const double gpu_dispatch_median_ms = Median(gpu_dispatch_samples, GPU_SAMPLE_COUNT);
    const double gpu_dispatch_p95_ms = Percentile95(gpu_dispatch_samples, GPU_SAMPLE_COUNT);
    const double gpu_end_to_end_median_ms = Median(gpu_end_to_end_samples, GPU_SAMPLE_COUNT);
    const double gpu_end_to_end_p95_ms = Percentile95(gpu_end_to_end_samples, GPU_SAMPLE_COUNT);

    printf("HARNESS device=%s pixels=%u bytes=%u bias=%.2f\n",
        context.device_name, (unsigned)PIXEL_COUNT, (unsigned)BUFFER_BYTES, bias);
    printf("METRIC cpu_median_ms=%.6f cpu_p95_ms=%.6f samples=%u\n",
        cpu_median_ms, cpu_p95_ms, (unsigned)CPU_SAMPLE_COUNT);
    printf("METRIC vulkan_setup_ms=%.6f\n", setup_ms);
    printf("METRIC gpu_dispatch_wait_median_ms=%.6f gpu_dispatch_wait_p95_ms=%.6f samples=%u\n",
        gpu_dispatch_median_ms, gpu_dispatch_p95_ms, (unsigned)GPU_SAMPLE_COUNT);
    printf("METRIC gpu_e2e_median_ms=%.6f gpu_e2e_p95_ms=%.6f samples=%u\n",
        gpu_end_to_end_median_ms, gpu_end_to_end_p95_ms, (unsigned)GPU_SAMPLE_COUNT);
    printf("METRIC kernel_speedup=%.4f e2e_speedup=%.4f\n",
        gpu_dispatch_median_ms > 0.0 ? cpu_median_ms / gpu_dispatch_median_ms : 0.0,
        gpu_end_to_end_median_ms > 0.0 ? cpu_median_ms / gpu_end_to_end_median_ms : 0.0);
    printf("EVIDENCE api=vkCmdDispatch groups=%u local_size=%u output_bytes_checked=%u\n",
        (unsigned)GROUP_COUNT_X, (unsigned)LOCAL_SIZE_X, (unsigned)BUFFER_BYTES);

    FILE *metrics_file = fopen("gpu_vulkan_brightness_metrics.jsonl", "a");
    if (metrics_file == NULL) {
        fprintf(stderr, "Unable to create the metrics artifact.\n");
        DestroyVulkan(&context);
        return -1;
    }
    const int metrics_write_result = fprintf(metrics_file,
        "{\"device\":\"%s\",\"pixels\":%u,\"bytes\":%u,\"bias\":%.2f,"
        "\"cpu_median_ms\":%.6f,\"cpu_p95_ms\":%.6f,\"vulkan_setup_ms\":%.6f,"
        "\"gpu_dispatch_wait_median_ms\":%.6f,\"gpu_dispatch_wait_p95_ms\":%.6f,"
        "\"gpu_host_marshalled_median_ms\":%.6f,\"gpu_host_marshalled_p95_ms\":%.6f,"
        "\"kernel_speedup\":%.4f,\"host_marshalled_speedup\":%.4f,"
        "\"vk_cmd_dispatch_groups\":%u,\"local_size\":%u,"
        "\"output_bytes_checked\":%u,\"mismatch_count\":%u}\n",
        context.device_name, (unsigned)PIXEL_COUNT, (unsigned)BUFFER_BYTES, bias,
        cpu_median_ms, cpu_p95_ms, setup_ms,
        gpu_dispatch_median_ms, gpu_dispatch_p95_ms,
        gpu_end_to_end_median_ms, gpu_end_to_end_p95_ms,
        gpu_dispatch_median_ms > 0.0 ? cpu_median_ms / gpu_dispatch_median_ms : 0.0,
        gpu_end_to_end_median_ms > 0.0 ? cpu_median_ms / gpu_end_to_end_median_ms : 0.0,
        (unsigned)GROUP_COUNT_X, (unsigned)LOCAL_SIZE_X, (unsigned)BUFFER_BYTES,
        (unsigned)mismatch_count);
    if (metrics_write_result < 0 || fclose(metrics_file) != 0) {
        fprintf(stderr, "Unable to finish the metrics artifact.\n");
        DestroyVulkan(&context);
        return -1;
    }

    DestroyVulkan(&context);
    if (mismatch_count != 0u) {
        fprintf(stderr, "Output mismatch: count=%zu first=%zu cpu=%u gpu=%u\n",
            mismatch_count, first_mismatch, (unsigned)g_cpu_result[first_mismatch],
            (unsigned)g_gpu_result[first_mismatch]);
        return -1;
    }
    return 0;
}

int main(void)
{
    printf("[==========] Running 1 test from 1 test suite.\n");
    printf("[----------] 1 test from GpuVulkanBrightnessTest\n");
    printf("[ RUN      ] GpuVulkanBrightnessTest.VkCmdDispatchMatchesCpuReference\n");
    fflush(stdout);
    const double start = NowMilliseconds();
    const int result = RunBrightnessValidation();
    const double elapsed = NowMilliseconds() - start;
    const long elapsed_ms = (long)(elapsed + 0.5);
    if (result == 0) {
        printf("[       OK ] GpuVulkanBrightnessTest.VkCmdDispatchMatchesCpuReference (%ld ms)\n", elapsed_ms);
        printf("[----------] 1 test from GpuVulkanBrightnessTest (%ld ms total)\n", elapsed_ms);
        printf("[==========] 1 test from 1 test suite ran. (%ld ms total)\n", elapsed_ms);
        printf("[  PASSED  ] 1 test.\n");
        return 0;
    }
    printf("[  FAILED  ] GpuVulkanBrightnessTest.VkCmdDispatchMatchesCpuReference (%ld ms)\n", elapsed_ms);
    printf("[----------] 1 test from GpuVulkanBrightnessTest (%ld ms total)\n", elapsed_ms);
    printf("[==========] 1 test from 1 test suite ran. (%ld ms total)\n", elapsed_ms);
    printf("[  FAILED  ] 1 test, listed below:\n");
    printf("[  FAILED  ] GpuVulkanBrightnessTest.VkCmdDispatchMatchesCpuReference\n");
    return 1;
}
