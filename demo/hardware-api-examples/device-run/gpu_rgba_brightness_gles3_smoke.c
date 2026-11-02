// GLES3 offload smoke test for 03_gpu_rgba_brightness.cpp.
// It renders an RGBA texture through a fragment shader, reads it back, and
// verifies the same brightness-and-clamp semantics as the CPU reference.

#include <EGL/egl.h>
#include <GLES3/gl3.h>

typedef __SIZE_TYPE__ Size;

static long SysWrite(int fd, const char *buffer, Size size)
{
    register long x0 __asm__("x0") = fd;
    register const char *x1 __asm__("x1") = buffer;
    register Size x2 __asm__("x2") = size;
    register long x8 __asm__("x8") = 64; // Linux aarch64 write
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

static int ByteNear(unsigned char actual, unsigned char expected)
{
    const int difference = (int)actual - (int)expected;
    return difference >= -1 && difference <= 1;
}

static GLuint CompileShader(GLenum type, const char *source)
{
    const GLuint shader = glCreateShader(type);
    if (shader == 0) {
        return 0;
    }
    glShaderSource(shader, 1, &source, 0);
    glCompileShader(shader);
    GLint compiled = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &compiled);
    return compiled == GL_TRUE ? shader : 0;
}

static GLuint CreateProgram(void)
{
    static const char kVertexShader[] =
        "#version 300 es\n"
        "layout(location = 0) in vec2 position;\n"
        "layout(location = 1) in vec2 texCoord;\n"
        "out vec2 vTexCoord;\n"
        "void main() { gl_Position = vec4(position, 0.0, 1.0); vTexCoord = texCoord; }\n";
    static const char kFragmentShader[] =
        "#version 300 es\n"
        "precision highp float;\n"
        "uniform sampler2D inputRgba;\n"
        "uniform float brightness;\n"
        "in vec2 vTexCoord;\n"
        "layout(location = 0) out vec4 outputRgba;\n"
        "void main() {\n"
        "  vec4 pixel = texture(inputRgba, vTexCoord);\n"
        "  outputRgba = vec4(clamp(pixel.rgb + vec3(brightness), 0.0, 1.0), pixel.a);\n"
        "}\n";

    const GLuint vertex = CompileShader(GL_VERTEX_SHADER, kVertexShader);
    const GLuint fragment = CompileShader(GL_FRAGMENT_SHADER, kFragmentShader);
    if (vertex == 0 || fragment == 0) {
        return 0;
    }
    const GLuint program = glCreateProgram();
    glAttachShader(program, vertex);
    glAttachShader(program, fragment);
    glLinkProgram(program);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    glDeleteShader(vertex);
    glDeleteShader(fragment);
    return linked == GL_TRUE ? program : 0;
}

static int RunGpuBrightness(void)
{
    static const EGLint kConfigAttributes[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
        EGL_RED_SIZE, 8,
        EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8,
        EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    };
    static const EGLint kSurfaceAttributes[] = {
        EGL_WIDTH, 2,
        EGL_HEIGHT, 1,
        EGL_NONE,
    };
    static const EGLint kContextAttributes[] = {
        EGL_CONTEXT_CLIENT_VERSION, 3,
        EGL_NONE,
    };
    static const GLfloat kFullscreenQuad[] = {
        -1.0F, -1.0F, 0.0F, 0.0F,
         1.0F, -1.0F, 1.0F, 0.0F,
        -1.0F,  1.0F, 0.0F, 1.0F,
        -1.0F,  1.0F, 0.0F, 1.0F,
         1.0F, -1.0F, 1.0F, 0.0F,
         1.0F,  1.0F, 1.0F, 1.0F,
    };
    static const unsigned char kInput[] = {
        0, 64, 128, 255,
        191, 10, 200, 17,
    };
    static const unsigned char kExpected[] = {
        64, 128, 192, 255,
        255, 74, 255, 17,
    };

    WriteLine("GPU smoke stage: acquire EGL display");
    const EGLDisplay display = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    WriteLine("GPU smoke stage: initialize EGL");
    if (display == EGL_NO_DISPLAY || eglInitialize(display, 0, 0) != EGL_TRUE ||
        eglBindAPI(EGL_OPENGL_ES_API) != EGL_TRUE) {
        return 1;
    }

    EGLConfig config = 0;
    EGLint configCount = 0;
    if (eglChooseConfig(display, kConfigAttributes, &config, 1, &configCount) != EGL_TRUE || configCount != 1) {
        return 2;
    }
    WriteLine("GPU smoke stage: create EGL context");
    const EGLSurface surface = eglCreatePbufferSurface(display, config, kSurfaceAttributes);
    const EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, kContextAttributes);
    if (surface == EGL_NO_SURFACE || context == EGL_NO_CONTEXT ||
        eglMakeCurrent(display, surface, surface, context) != EGL_TRUE) {
        return 3;
    }

    const GLubyte *renderer = glGetString(GL_RENDERER);
    const GLubyte *version = glGetString(GL_VERSION);
    if (renderer == 0 || version == 0) {
        return 4;
    }
    WriteLine("GPU offload renderer:");
    WriteLine((const char *)renderer);
    WriteLine("GPU API version:");
    WriteLine((const char *)version);

    const GLuint program = CreateProgram();
    if (program == 0) {
        return 5;
    }

    GLuint inputTexture = 0;
    GLuint outputTexture = 0;
    GLuint framebuffer = 0;
    GLuint vertexArray = 0;
    GLuint vertexBuffer = 0;
    glGenTextures(1, &inputTexture);
    glBindTexture(GL_TEXTURE_2D, inputTexture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 2, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, kInput);

    glGenTextures(1, &outputTexture);
    glBindTexture(GL_TEXTURE_2D, outputTexture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 2, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, 0);

    glGenFramebuffers(1, &framebuffer);
    glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, outputTexture, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        return 6;
    }

    glGenVertexArrays(1, &vertexArray);
    glBindVertexArray(vertexArray);
    glGenBuffers(1, &vertexBuffer);
    glBindBuffer(GL_ARRAY_BUFFER, vertexBuffer);
    glBufferData(GL_ARRAY_BUFFER, sizeof(kFullscreenQuad), kFullscreenQuad, GL_STATIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * (GLsizei)sizeof(GLfloat), 0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * (GLsizei)sizeof(GLfloat),
        (const void *)(2 * sizeof(GLfloat)));

    glViewport(0, 0, 2, 1);
    glUseProgram(program);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, inputTexture);
    glUniform1i(glGetUniformLocation(program, "inputRgba"), 0);
    glUniform1f(glGetUniformLocation(program, "brightness"), 0.25F);
    glDrawArrays(GL_TRIANGLES, 0, 6);
    glFinish();

    unsigned char output[sizeof(kExpected)] = {0};
    glReadPixels(0, 0, 2, 1, GL_RGBA, GL_UNSIGNED_BYTE, output);
    if (glGetError() != GL_NO_ERROR) {
        return 7;
    }
    for (Size index = 0; index < sizeof(kExpected); ++index) {
        if (!ByteNear(output[index], kExpected[index])) {
            return 8;
        }
    }

    glDeleteBuffers(1, &vertexBuffer);
    glDeleteVertexArrays(1, &vertexArray);
    glDeleteFramebuffers(1, &framebuffer);
    glDeleteTextures(1, &outputTexture);
    glDeleteTextures(1, &inputTexture);
    glDeleteProgram(program);
    eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(display, context);
    eglDestroySurface(display, surface);
    eglTerminate(display);
    return 0;
}

extern int __libc_start_main(int (*mainFunction)(int, char **, char **), int argc, char **argv,
    void (*initFunction)(void), void (*finiFunction)(void), void (*loaderFiniFunction)(void));

__attribute__((noreturn)) void GpuSmokeStartC(long *stack)
{
    const int argc = (int)stack[0];
    char **argv = (char **)(stack + 1);
    extern int main(int, char **, char **);
    __libc_start_main(main, argc, argv, 0, 0, 0);
    __builtin_unreachable();
}

__asm__(
    ".text\n"
    ".global _start\n"
    ".type _start,%function\n"
    "_start:\n"
    "  hint #34\n"
    "  mov x29, #0\n"
    "  mov x30, #0\n"
    "  mov x0, sp\n"
    "  and sp, x0, #-16\n"
    "  b GpuSmokeStartC\n");

int main(int argc, char **argv, char **envp)
{
    (void)argc;
    (void)argv;
    (void)envp;
    WriteLine("GPU smoke stage: process started");
    const int result = RunGpuBrightness();
    if (result == 0) {
        WriteLine("GPU RGBA brightness offload: PASS");
    } else {
        WriteLine("GPU RGBA brightness offload: FAIL");
    }
    return result;
}
