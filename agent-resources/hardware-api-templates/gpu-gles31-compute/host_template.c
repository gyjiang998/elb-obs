// GLES 3.1 compute smoke test for regular RGBA buffer transforms.
//
// Curated adaptation points: kComputeShader, kInput, kExpected, element
// layout, uniforms, dispatch dimensions and comparison tolerance. Keep the
// EGL/context lifecycle, shader checks, SSBO binding, barriers and cleanup.

#include <EGL/egl.h>
#include <GLES3/gl31.h>
#include <dlfcn.h>

typedef __SIZE_TYPE__ Size;

static long SysWrite(int fd, const char *buffer, Size size)
{
    register long x0 __asm__("x0") = fd;
    register const char *x1 __asm__("x1") = buffer;
    register Size x2 __asm__("x2") = size;
    register long x8 __asm__("x8") = 64;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory");
    return x0;
}

static Size StringLength(const char *value)
{
    Size length = 0;
    while (value[length] != '\0') {
        ++length;
    }
    return length;
}

static void WriteLine(const char *value)
{
    SysWrite(1, value, StringLength(value));
    SysWrite(1, "\n", 1);
}

typedef struct GpuApi {
    void *eglLibrary;
    void *glesLibrary;
    __typeof__(&eglGetDisplay) eglGetDisplayFn;
    __typeof__(&eglInitialize) eglInitializeFn;
    __typeof__(&eglBindAPI) eglBindAPIFn;
    __typeof__(&eglChooseConfig) eglChooseConfigFn;
    __typeof__(&eglCreatePbufferSurface) eglCreatePbufferSurfaceFn;
    __typeof__(&eglCreateContext) eglCreateContextFn;
    __typeof__(&eglMakeCurrent) eglMakeCurrentFn;
    __typeof__(&eglDestroyContext) eglDestroyContextFn;
    __typeof__(&eglDestroySurface) eglDestroySurfaceFn;
    __typeof__(&eglTerminate) eglTerminateFn;
    __typeof__(&glGetString) glGetStringFn;
    __typeof__(&glCreateShader) glCreateShaderFn;
    __typeof__(&glShaderSource) glShaderSourceFn;
    __typeof__(&glCompileShader) glCompileShaderFn;
    __typeof__(&glGetShaderiv) glGetShaderivFn;
    __typeof__(&glDeleteShader) glDeleteShaderFn;
    __typeof__(&glCreateProgram) glCreateProgramFn;
    __typeof__(&glAttachShader) glAttachShaderFn;
    __typeof__(&glLinkProgram) glLinkProgramFn;
    __typeof__(&glGetProgramiv) glGetProgramivFn;
    __typeof__(&glDeleteProgram) glDeleteProgramFn;
    __typeof__(&glGenBuffers) glGenBuffersFn;
    __typeof__(&glBindBuffer) glBindBufferFn;
    __typeof__(&glBufferData) glBufferDataFn;
    __typeof__(&glBindBufferBase) glBindBufferBaseFn;
    __typeof__(&glUseProgram) glUseProgramFn;
    __typeof__(&glGetUniformLocation) glGetUniformLocationFn;
    __typeof__(&glUniform1ui) glUniform1uiFn;
    __typeof__(&glDispatchCompute) glDispatchComputeFn;
    __typeof__(&glMemoryBarrier) glMemoryBarrierFn;
    __typeof__(&glFinish) glFinishFn;
    __typeof__(&glMapBufferRange) glMapBufferRangeFn;
    __typeof__(&glUnmapBuffer) glUnmapBufferFn;
    __typeof__(&glDeleteBuffers) glDeleteBuffersFn;
    __typeof__(&glGetError) glGetErrorFn;
} GpuApi;

#define LOAD_SYMBOL(api, library, member, symbol) \
    do { \
        (api)->member = (__typeof__((api)->member))dlsym((library), #symbol); \
        if ((api)->member == 0) { \
            return 0; \
        } \
    } while (0)

static int LoadGpuApi(GpuApi *api)
{
    api->eglLibrary = dlopen("libEGL.so", RTLD_NOW | RTLD_LOCAL);
    api->glesLibrary = dlopen("libGLESv3.so", RTLD_NOW | RTLD_LOCAL);
    if (api->eglLibrary == 0 || api->glesLibrary == 0) {
        return 0;
    }
    LOAD_SYMBOL(api, api->eglLibrary, eglGetDisplayFn, eglGetDisplay);
    LOAD_SYMBOL(api, api->eglLibrary, eglInitializeFn, eglInitialize);
    LOAD_SYMBOL(api, api->eglLibrary, eglBindAPIFn, eglBindAPI);
    LOAD_SYMBOL(api, api->eglLibrary, eglChooseConfigFn, eglChooseConfig);
    LOAD_SYMBOL(api, api->eglLibrary, eglCreatePbufferSurfaceFn, eglCreatePbufferSurface);
    LOAD_SYMBOL(api, api->eglLibrary, eglCreateContextFn, eglCreateContext);
    LOAD_SYMBOL(api, api->eglLibrary, eglMakeCurrentFn, eglMakeCurrent);
    LOAD_SYMBOL(api, api->eglLibrary, eglDestroyContextFn, eglDestroyContext);
    LOAD_SYMBOL(api, api->eglLibrary, eglDestroySurfaceFn, eglDestroySurface);
    LOAD_SYMBOL(api, api->eglLibrary, eglTerminateFn, eglTerminate);
    LOAD_SYMBOL(api, api->glesLibrary, glGetStringFn, glGetString);
    LOAD_SYMBOL(api, api->glesLibrary, glCreateShaderFn, glCreateShader);
    LOAD_SYMBOL(api, api->glesLibrary, glShaderSourceFn, glShaderSource);
    LOAD_SYMBOL(api, api->glesLibrary, glCompileShaderFn, glCompileShader);
    LOAD_SYMBOL(api, api->glesLibrary, glGetShaderivFn, glGetShaderiv);
    LOAD_SYMBOL(api, api->glesLibrary, glDeleteShaderFn, glDeleteShader);
    LOAD_SYMBOL(api, api->glesLibrary, glCreateProgramFn, glCreateProgram);
    LOAD_SYMBOL(api, api->glesLibrary, glAttachShaderFn, glAttachShader);
    LOAD_SYMBOL(api, api->glesLibrary, glLinkProgramFn, glLinkProgram);
    LOAD_SYMBOL(api, api->glesLibrary, glGetProgramivFn, glGetProgramiv);
    LOAD_SYMBOL(api, api->glesLibrary, glDeleteProgramFn, glDeleteProgram);
    LOAD_SYMBOL(api, api->glesLibrary, glGenBuffersFn, glGenBuffers);
    LOAD_SYMBOL(api, api->glesLibrary, glBindBufferFn, glBindBuffer);
    LOAD_SYMBOL(api, api->glesLibrary, glBufferDataFn, glBufferData);
    LOAD_SYMBOL(api, api->glesLibrary, glBindBufferBaseFn, glBindBufferBase);
    LOAD_SYMBOL(api, api->glesLibrary, glUseProgramFn, glUseProgram);
    LOAD_SYMBOL(api, api->glesLibrary, glGetUniformLocationFn, glGetUniformLocation);
    LOAD_SYMBOL(api, api->glesLibrary, glUniform1uiFn, glUniform1ui);
    LOAD_SYMBOL(api, api->glesLibrary, glDispatchComputeFn, glDispatchCompute);
    LOAD_SYMBOL(api, api->glesLibrary, glMemoryBarrierFn, glMemoryBarrier);
    LOAD_SYMBOL(api, api->glesLibrary, glFinishFn, glFinish);
    LOAD_SYMBOL(api, api->glesLibrary, glMapBufferRangeFn, glMapBufferRange);
    LOAD_SYMBOL(api, api->glesLibrary, glUnmapBufferFn, glUnmapBuffer);
    LOAD_SYMBOL(api, api->glesLibrary, glDeleteBuffersFn, glDeleteBuffers);
    LOAD_SYMBOL(api, api->glesLibrary, glGetErrorFn, glGetError);
    return 1;
}

#define eglGetDisplay api->eglGetDisplayFn
#define eglInitialize api->eglInitializeFn
#define eglBindAPI api->eglBindAPIFn
#define eglChooseConfig api->eglChooseConfigFn
#define eglCreatePbufferSurface api->eglCreatePbufferSurfaceFn
#define eglCreateContext api->eglCreateContextFn
#define eglMakeCurrent api->eglMakeCurrentFn
#define eglDestroyContext api->eglDestroyContextFn
#define eglDestroySurface api->eglDestroySurfaceFn
#define eglTerminate api->eglTerminateFn
#define glGetString api->glGetStringFn
#define glCreateShader api->glCreateShaderFn
#define glShaderSource api->glShaderSourceFn
#define glCompileShader api->glCompileShaderFn
#define glGetShaderiv api->glGetShaderivFn
#define glDeleteShader api->glDeleteShaderFn
#define glCreateProgram api->glCreateProgramFn
#define glAttachShader api->glAttachShaderFn
#define glLinkProgram api->glLinkProgramFn
#define glGetProgramiv api->glGetProgramivFn
#define glDeleteProgram api->glDeleteProgramFn
#define glGenBuffers api->glGenBuffersFn
#define glBindBuffer api->glBindBufferFn
#define glBufferData api->glBufferDataFn
#define glBindBufferBase api->glBindBufferBaseFn
#define glUseProgram api->glUseProgramFn
#define glGetUniformLocation api->glGetUniformLocationFn
#define glUniform1ui api->glUniform1uiFn
#define glDispatchCompute api->glDispatchComputeFn
#define glMemoryBarrier api->glMemoryBarrierFn
#define glFinish api->glFinishFn
#define glMapBufferRange api->glMapBufferRangeFn
#define glUnmapBuffer api->glUnmapBufferFn
#define glDeleteBuffers api->glDeleteBuffersFn
#define glGetError api->glGetErrorFn

static GLuint CreateComputeProgram(GpuApi *api)
{
    static const char kComputeShader[] =
        "#version 310 es\n"
        "layout(local_size_x = 64) in;\n"
        "layout(std430, binding = 0) readonly buffer InputValues { uint sourceValue[]; };\n"
        "layout(std430, binding = 1) writeonly buffer OutputValues { uint outputValue[]; };\n"
        "uniform uint elementCount;\n"
        "uniform uint bias;\n"
        "void main() {\n"
        "  uint pixel = gl_GlobalInvocationID.x;\n"
        "  uint offset = pixel * 4u;\n"
        "  if (offset + 3u >= elementCount) { return; }\n"
        "  outputValue[offset] = min(sourceValue[offset] + bias, 255u);\n"
        "  outputValue[offset + 1u] = min(sourceValue[offset + 1u] + bias, 255u);\n"
        "  outputValue[offset + 2u] = min(sourceValue[offset + 2u] + bias, 255u);\n"
        "  outputValue[offset + 3u] = sourceValue[offset + 3u];\n"
        "}\n";

    const GLuint shader = glCreateShader(GL_COMPUTE_SHADER);
    if (shader == 0) {
        return 0;
    }
    const GLchar *shaderSource = kComputeShader;
    glShaderSource(shader, 1, &shaderSource, 0);
    glCompileShader(shader);
    GLint compiled = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &compiled);
    if (compiled != GL_TRUE) {
        glDeleteShader(shader);
        return 0;
    }

    const GLuint program = glCreateProgram();
    glAttachShader(program, shader);
    glLinkProgram(program);
    glDeleteShader(shader);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (linked != GL_TRUE) {
        glDeleteProgram(program);
        return 0;
    }
    return program;
}

static int RunCompute(void)
{
    GpuApi apiStorage = {0};
    GpuApi *api = &apiStorage;
    static const EGLint kConfigAttributes[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
        EGL_NONE,
    };
    static const EGLint kSurfaceAttributes[] = {
        EGL_WIDTH, 1,
        EGL_HEIGHT, 1,
        EGL_NONE,
    };
    static const EGLint kContextAttributes[] = {
        EGL_CONTEXT_CLIENT_VERSION, 3,
        EGL_NONE,
    };
    static const GLuint kInput[] = {0, 64, 128, 255, 191, 10, 200, 17};
    static const GLuint kExpected[] = {64, 128, 192, 255, 255, 74, 255, 17};

    WriteLine("GLES compute stage: load EGL/GLES symbols");
    if (!LoadGpuApi(api)) {
        return 1;
    }
    WriteLine("GLES compute stage: create EGL context");
    const EGLDisplay display = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (display == EGL_NO_DISPLAY || eglInitialize(display, 0, 0) != EGL_TRUE ||
        eglBindAPI(EGL_OPENGL_ES_API) != EGL_TRUE) {
        return 2;
    }
    EGLConfig config = 0;
    EGLint configCount = 0;
    if (eglChooseConfig(display, kConfigAttributes, &config, 1, &configCount) != EGL_TRUE || configCount != 1) {
        return 3;
    }
    const EGLSurface surface = eglCreatePbufferSurface(display, config, kSurfaceAttributes);
    const EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, kContextAttributes);
    if (surface == EGL_NO_SURFACE || context == EGL_NO_CONTEXT ||
        eglMakeCurrent(display, surface, surface, context) != EGL_TRUE) {
        return 4;
    }

    const GLubyte *version = glGetString(GL_VERSION);
    const GLubyte *renderer = glGetString(GL_RENDERER);
    if (version == 0 || renderer == 0) {
        return 5;
    }
    WriteLine("GPU compute renderer:");
    WriteLine((const char *)renderer);
    WriteLine("GPU API version:");
    WriteLine((const char *)version);

    WriteLine("GLES compute stage: compile program");
    const GLuint program = CreateComputeProgram(api);
    if (program == 0) {
        return 6;
    }

    GLuint buffers[2] = {0, 0};
    glGenBuffers(2, buffers);
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, buffers[0]);
    glBufferData(GL_SHADER_STORAGE_BUFFER, sizeof(kInput), kInput, GL_STATIC_DRAW);
    glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 0, buffers[0]);
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, buffers[1]);
    glBufferData(GL_SHADER_STORAGE_BUFFER, sizeof(kExpected), 0, GL_DYNAMIC_READ);
    glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 1, buffers[1]);

    glUseProgram(program);
    glUniform1ui(glGetUniformLocation(program, "elementCount"), (GLuint)(sizeof(kInput) / sizeof(kInput[0])));
    glUniform1ui(glGetUniformLocation(program, "bias"), 64);
    glDispatchCompute(1, 1, 1);
    glMemoryBarrier(GL_BUFFER_UPDATE_BARRIER_BIT | GL_SHADER_STORAGE_BARRIER_BIT);
    glFinish();

    glBindBuffer(GL_SHADER_STORAGE_BUFFER, buffers[1]);
    const GLuint *output = (const GLuint *)glMapBufferRange(
        GL_SHADER_STORAGE_BUFFER, 0, sizeof(kExpected), GL_MAP_READ_BIT);
    if (output == 0) {
        return 7;
    }
    int matched = 1;
    for (Size index = 0; index < sizeof(kExpected) / sizeof(kExpected[0]); ++index) {
        if (output[index] != kExpected[index]) {
            matched = 0;
            break;
        }
    }
    glUnmapBuffer(GL_SHADER_STORAGE_BUFFER);
    const GLenum glError = glGetError();
    const int result = matched && glError == GL_NO_ERROR ? 0 : 8;

    glDeleteBuffers(2, buffers);
    glDeleteProgram(program);
    eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(display, context);
    eglDestroySurface(display, surface);
    eglTerminate(display);
    dlclose(api->glesLibrary);
    dlclose(api->eglLibrary);
    return result;
}

int main(void)
{
    WriteLine("GLES 3.1 compute smoke: started");
    const int result = RunCompute();
    WriteLine(result == 0 ? "GLES 3.1 compute smoke: PASS" : "GLES 3.1 compute smoke: FAIL");
    return result;
}
