
using Size = __SIZE_TYPE__;

static float Cl(float value)
{
    if (value > 1.0F) {
        return 1.0F;
    }
    if (value < -1.0F) {
        return -1.0F;
    }
    return value;
}

extern "C" void RunDspPcmEqualizer(float *pp, Size ffCount, Size ccCount, float lb, float hb)
{
    for (Size ff = 0; ff < ffCount; ++ff) {
        for (Size cc = 0; cc < ccCount; ++cc) {
            const Size index = ff * ccCount + cc;
            const float ss = pp[index];
            const float ee = ss * lb + ss * hb;
            pp[index] = Cl(ee);
        }
    }
}
