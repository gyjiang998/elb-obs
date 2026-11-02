# Image Codec HDI Family Template

This family covers JPEG encode/decode and JPEG/HEIF hardware image decode paths.
It is a system/HDI integration recipe unless the selected record exposes a
public NDK surface.

Preserve service discovery, codec capability selection, buffer allocation and
ownership, callback/error lifecycle and software fallback. Adapt operation
direction, MIME/container, dimensions, pixel format, color space, stride,
bitstream fixture and output oracle. Exercise a deterministic small image first,
then a representative workload. Verify decoded pixels or validate the encoded
bitstream independently. Report capability, successful output, actual hardware
path, fallback and end-to-end cost as separate evidence levels.
