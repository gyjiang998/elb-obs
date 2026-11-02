#include <cstddef>
#include <cstdint>

namespace image_pipeline {

static int AbsInt(int value)
{
    return value < 0 ? -value : value;
}

// source and edges must not overlap. Strides are measured in bytes.
// Border pixels are defined as zero.
void BuildEdgeMap(const std::uint8_t *source, std::uint8_t *edges,
    std::size_t width, std::size_t height,
    std::size_t sourceStride, std::size_t edgeStride)
{
    if (source == nullptr || edges == nullptr || width < 3 || height < 3) {
        return;
    }

    for (std::size_t x = 0; x < width; ++x) {
        edges[x] = 0;
        edges[(height - 1) * edgeStride + x] = 0;
    }

    for (std::size_t y = 1; y + 1 < height; ++y) {
        edges[y * edgeStride] = 0;
        edges[y * edgeStride + width - 1] = 0;

        const std::uint8_t *previous = source + (y - 1) * sourceStride;
        const std::uint8_t *current = source + y * sourceStride;
        const std::uint8_t *next = source + (y + 1) * sourceStride;
        std::uint8_t *output = edges + y * edgeStride;

        for (std::size_t x = 1; x + 1 < width; ++x) {
            const int horizontal =
                -static_cast<int>(previous[x - 1]) + static_cast<int>(previous[x + 1])
                - 2 * static_cast<int>(current[x - 1]) + 2 * static_cast<int>(current[x + 1])
                - static_cast<int>(next[x - 1]) + static_cast<int>(next[x + 1]);
            const int vertical =
                -static_cast<int>(previous[x - 1]) - 2 * static_cast<int>(previous[x])
                - static_cast<int>(previous[x + 1]) + static_cast<int>(next[x - 1])
                + 2 * static_cast<int>(next[x]) + static_cast<int>(next[x + 1]);
            const int magnitude = AbsInt(horizontal) + AbsInt(vertical);
            output[x] = static_cast<std::uint8_t>(magnitude > 255 ? 255 : magnitude);
        }
    }
}

} // namespace image_pipeline
