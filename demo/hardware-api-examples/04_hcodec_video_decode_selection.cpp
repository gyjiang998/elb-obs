#include <memory>
#include <string>
#include <vector>

namespace demo {

struct CapabilityData {
    std::string mime;
    std::string componentName;
    bool supportsHardware;
};

class CodecBase;
class HCodecLoader {
public:
    static std::shared_ptr<CodecBase> CreateByName(const std::string &componentName);
};
class FCodecLoader {
public:
    static std::shared_ptr<CodecBase> CreateByName(const std::string &componentName);
};

// A hardware-first video decoder selection path. It first tries a compatible
// HCodec component and then preserves a software FCodec fallback. This is a
// system-integration example: the candidate is HCodec capability/component
// selection, while actual codec profile, component availability, buffer
// lifecycle and latency still need runtime verification.
std::shared_ptr<CodecBase> CreateHCodecVideoDecoderWithFallback(const std::string &mime,
    const std::vector<CapabilityData> &capabilities)
{
    for (const auto &capability : capabilities) {
        if (capability.mime != mime || !capability.supportsHardware) {
            continue;
        }
        std::shared_ptr<CodecBase> decoder = HCodecLoader::CreateByName(capability.componentName);
        if (decoder) {
            return decoder;
        }
    }

    for (const auto &capability : capabilities) {
        if (capability.mime == mime) {
            return FCodecLoader::CreateByName(capability.componentName);
        }
    }
    return nullptr;
}

} // namespace demo
