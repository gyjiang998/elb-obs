// Freestanding arm64 smoke test for the CPU fallback calculation. This test
// deliberately does not claim that DSP hardware is used.

#include "../01_dsp_pcm_equalizer.cpp"

using USize = __SIZE_TYPE__;

static long SysWrite(int fd, const char *buffer, USize size)
{
    register long x0 asm("x0") = fd;
    register const char *x1 asm("x1") = buffer;
    register USize x2 asm("x2") = size;
    register long x8 asm("x8") = 64; // Linux aarch64 write
    asm volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory");
    return x0;
}

[[noreturn]] static void SysExit(int status)
{
    register long x0 asm("x0") = status;
    register long x8 asm("x8") = 93; // Linux aarch64 exit
    asm volatile("svc #0" : : "r"(x0), "r"(x8) : "memory");
    __builtin_unreachable();
}

static bool Near(float actual, float expected)
{
    const float diff = actual > expected ? actual - expected : expected - actual;
    return diff < 0.0001F;
}

extern "C" [[noreturn]] void _start()
{
    float pcm[] = {0.25F, -0.25F, 0.75F, -0.75F, 1.0F, -1.0F};
    RunDspPcmEqualizer(pcm, 3, 2, 0.8F, 0.5F);

    const bool passed =
        Near(pcm[0], 0.325F) && Near(pcm[1], -0.325F) &&
        Near(pcm[2], 0.975F) && Near(pcm[3], -0.975F) &&
        Near(pcm[4], 1.0F) && Near(pcm[5], -1.0F);
    static constexpr char kPass[] = "DSP PCM equalizer CPU fallback: PASS\n";
    static constexpr char kFail[] = "DSP PCM equalizer CPU fallback: FAIL\n";
    if (passed) {
        SysWrite(1, kPass, sizeof(kPass) - 1);
        SysExit(0);
    }
    SysWrite(2, kFail, sizeof(kFail) - 1);
    SysExit(1);
}
