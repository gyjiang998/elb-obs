#include <cstddef>
#include <cstdint>

static uint8_t ClampToByte(int value)
{
    if (value < 0) {
        return 0;
    }
    if (value > 255) {
        return 255;
    }
    return static_cast<uint8_t>(value);
}

void ComputeGradientMap(
    const uint8_t *input,
    uint8_t *output,
    std::size_t width,
    std::size_t height,
    std::size_t inputStride,
    std::size_t outputStride)
{
    if (input == nullptr || output == nullptr || width < 3 || height < 3 ||
        inputStride < width || outputStride < width) {
        return;
    }

    for (std::size_t y = 0; y < height; ++y) {
        for (std::size_t x = 0; x < width; ++x) {
            if (x == 0 || y == 0 || x + 1 == width || y + 1 == height) {
                output[y * outputStride + x] = 0;
                continue;
            }

            const std::size_t top = (y - 1) * inputStride;
            const std::size_t middle = y * inputStride;
            const std::size_t bottom = (y + 1) * inputStride;

            const int gradientX =
                -static_cast<int>(input[top + x - 1]) +
                static_cast<int>(input[top + x + 1]) -
                2 * static_cast<int>(input[middle + x - 1]) +
                2 * static_cast<int>(input[middle + x + 1]) -
                static_cast<int>(input[bottom + x - 1]) +
                static_cast<int>(input[bottom + x + 1]);

            const int gradientY =
                -static_cast<int>(input[top + x - 1]) -
                2 * static_cast<int>(input[top + x]) -
                static_cast<int>(input[top + x + 1]) +
                static_cast<int>(input[bottom + x - 1]) +
                2 * static_cast<int>(input[bottom + x]) +
                static_cast<int>(input[bottom + x + 1]);

            const int absX = gradientX < 0 ? -gradientX : gradientX;
            const int absY = gradientY < 0 ? -gradientY : gradientY;
            output[y * outputStride + x] = ClampToByte(absX + absY);
        }
    }
}
