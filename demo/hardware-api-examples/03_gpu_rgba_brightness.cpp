
static std::uint8_t cc(float value)
{
    if (value < 0.0F) {
        return 0;
    }
    if (value > 255.0F) {
        return 255;
    }
    return static_cast<std::uint8_t>(value);
}


void aaa(const std::uint8_t *sourceda, std::uint8_t *dd,
    std::size_t ppCount, float bbb)
{
    for (std::size_t pp = 0; pp < ppCount; ++pp) {
        const std::size_t offset = pp * 4;
        dd[offset] = cc(static_cast<float>(sourceda[offset]) + bbb);
        dd[offset + 1] = cc(static_cast<float>(sourceda[offset + 1]) + bbb);
        dd[offset + 2] = cc(static_cast<float>(sourceda[offset + 2]) + bbb);
        dd[offset + 3] = sourceda[offset + 3];
    }
}
