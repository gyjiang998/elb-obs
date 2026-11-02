#include <native_avcapability.h>
#include <native_avcodec_base.h>

#include <stdio.h>

/*
 * Curated AVCodec capability template.
 * Adapt MIME and direction, then extend with the matching decoder/encoder
 * lifecycle and a real bitstream oracle. A PASS here proves only that the
 * runtime reports a hardware-category capability.
 */
int main(void)
{
    const char *mime = OH_AVCODEC_MIMETYPE_VIDEO_AVC;
    const bool is_encoder = false;
    OH_AVCapability *capability =
        OH_AVCodec_GetCapabilityByCategory(mime, is_encoder, HARDWARE);
    if (capability == NULL) {
        printf("RESULT status=NO_HARDWARE_CAPABILITY mime=%s direction=decoder\n", mime);
        return 2;
    }

    const char *name = OH_AVCapability_GetName(capability);
    const bool hardware = OH_AVCapability_IsHardware(capability);
    printf("EVIDENCE api=OH_AVCodec_GetCapabilityByCategory mime=%s hardware=%d name=%s\n",
        mime, hardware ? 1 : 0, name == NULL ? "<unknown>" : name);
    if (!hardware) {
        printf("RESULT status=CATEGORY_MISMATCH\n");
        return 3;
    }

    printf("RESULT status=CAPABILITY_PASS boundary=not_yet_decoded\n");
    return 0;
}
