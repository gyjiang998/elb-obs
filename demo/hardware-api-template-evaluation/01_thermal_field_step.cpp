#include <cstddef>
#include <cstdint>

bool F_01(
    const float* p0,
    const float* p1,
    float* p2,
    std::uint32_t n0,
    std::uint32_t n1,
    float k0,
    float k1)
{
    if (p0 == nullptr || p1 == nullptr || p2 == nullptr ||
        n0 < 3 || n1 < 3 || k0 < 0.0F || k0 > 0.25F) {
        return false;
    }

    const std::size_t s0 = static_cast<std::size_t>(n0);

    for (std::uint32_t i0 = 0; i0 < n0; ++i0) {
        p2[i0] = p0[i0];
        const std::size_t i1 = static_cast<std::size_t>(n1 - 1) * s0 + i0;
        p2[i1] = p0[i1];
    }

    for (std::uint32_t i0 = 1; i0 + 1 < n1; ++i0) {
        const std::size_t i1 = static_cast<std::size_t>(i0) * s0;
        p2[i1] = p0[i1];
        p2[i1 + n0 - 1] = p0[i1 + n0 - 1];

        for (std::uint32_t i2 = 1; i2 + 1 < n0; ++i2) {
            const std::size_t i3 = i1 + i2;
            const float v0 = p0[i3];
            const float v1 =
                p0[i3 - 1] + p0[i3 + 1] +
                p0[i3 - s0] + p0[i3 + s0] -
                4.0F * v0;

            float v2 = v0 + k0 * v1 + k1 * p1[i3];
            if (v2 < -1000.0F) {
                v2 = -1000.0F;
            } else if (v2 > 1000.0F) {
                v2 = 1000.0F;
            }
            p2[i3] = v2;
        }
    }

    return true;
}
