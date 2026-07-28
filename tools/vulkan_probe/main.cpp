#include <vulkan/vulkan.h>

#include <cstdint>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace {

std::string json_string(const char * value) {
    std::ostringstream output;
    output << '"';
    for (const unsigned char character : std::string(value)) {
        switch (character) {
            case '"': output << "\\\""; break;
            case '\\': output << "\\\\"; break;
            case '\b': output << "\\b"; break;
            case '\f': output << "\\f"; break;
            case '\n': output << "\\n"; break;
            case '\r': output << "\\r"; break;
            case '\t': output << "\\t"; break;
            default:
                if (character < 0x20) {
                    output << "\\u"
                           << std::hex << std::setw(4) << std::setfill('0')
                           << static_cast<int>(character)
                           << std::dec;
                } else {
                    output << character;
                }
        }
    }
    output << '"';
    return output.str();
}

const char * vendor_name(std::uint32_t vendor_id) {
    switch (vendor_id) {
        case 0x1002:
        case 0x1022:
            return "amd";
        case 0x8086:
            return "intel";
        case 0x10DE:
            return "nvidia";
        default:
            return "other";
    }
}

const char * device_type_name(VkPhysicalDeviceType type) {
    switch (type) {
        case VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU:
            return "discrete";
        case VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU:
            return "integrated";
        case VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU:
            return "virtual";
        case VK_PHYSICAL_DEVICE_TYPE_CPU:
            return "cpu";
        default:
            return "other";
    }
}

}  // namespace

int main() {
    const VkApplicationInfo application_info{
        VK_STRUCTURE_TYPE_APPLICATION_INFO,
        nullptr,
        "Rothbald Vulkan probe",
        1,
        "Rothbald",
        1,
        VK_API_VERSION_1_0,
    };
    const VkInstanceCreateInfo create_info{
        VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        nullptr,
        0,
        &application_info,
        0,
        nullptr,
        0,
        nullptr,
    };
    VkInstance instance = VK_NULL_HANDLE;
    if (vkCreateInstance(&create_info, nullptr, &instance) != VK_SUCCESS) {
        std::cout << "[]\n";
        return 0;
    }

    std::uint32_t count = 0;
    if (vkEnumeratePhysicalDevices(instance, &count, nullptr) != VK_SUCCESS || count == 0) {
        vkDestroyInstance(instance, nullptr);
        std::cout << "[]\n";
        return 0;
    }
    std::vector<VkPhysicalDevice> devices(count);
    if (vkEnumeratePhysicalDevices(instance, &count, devices.data()) != VK_SUCCESS) {
        vkDestroyInstance(instance, nullptr);
        std::cout << "[]\n";
        return 0;
    }

    std::cout << '[';
    for (std::uint32_t index = 0; index < count; ++index) {
        VkPhysicalDeviceProperties properties{};
        VkPhysicalDeviceMemoryProperties memory_properties{};
        vkGetPhysicalDeviceProperties(devices[index], &properties);
        vkGetPhysicalDeviceMemoryProperties(devices[index], &memory_properties);
        VkDeviceSize local_memory = 0;
        for (std::uint32_t heap = 0; heap < memory_properties.memoryHeapCount; ++heap) {
            if (memory_properties.memoryHeaps[heap].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) {
                local_memory += memory_properties.memoryHeaps[heap].size;
            }
        }
        if (index) {
            std::cout << ',';
        }
        std::cout << "{\"index\":" << index
                  << ",\"name\":" << json_string(properties.deviceName)
                  << ",\"vendor\":\"" << vendor_name(properties.vendorID) << '"'
                  << ",\"type\":\"" << device_type_name(properties.deviceType) << '"'
                  << ",\"memory\":" << static_cast<std::uint64_t>(local_memory)
                  << '}';
    }
    std::cout << "]\n";
    vkDestroyInstance(instance, nullptr);
    return 0;
}
