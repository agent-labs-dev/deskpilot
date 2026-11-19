// Spike probe: can Mesa give a GLES context on a KMS-only DRM node through GBM, the path Hyprland takes there.
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    int fd = open(argv[1], O_RDWR | O_CLOEXEC);
    printf("open %s: %d\n", argv[1], fd);
    struct gbm_device *gbm = gbm_create_device(fd);
    printf("gbm device: %p backend %s\n", (void *)gbm, gbm ? gbm_device_get_backend_name(gbm) : "-");
    PFNEGLGETPLATFORMDISPLAYEXTPROC get = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
    EGLDisplay dpy = get(EGL_PLATFORM_GBM_KHR, gbm, NULL);
    printf("display: %p error 0x%x\n", dpy, eglGetError());
    EGLint major = 0, minor = 0;
    EGLBoolean ok = eglInitialize(dpy, &major, &minor);
    printf("initialize: %d %d.%d error 0x%x\n", ok, major, minor, eglGetError());
    if (!ok)
        return 1;
    printf("vendor: %s\nextensions: %s\n", eglQueryString(dpy, EGL_VENDOR), eglQueryString(dpy, EGL_EXTENSIONS));
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint attrs[] = {EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 2, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, attrs);
    printf("context: %p error 0x%x\n", ctx, eglGetError());
    if (ctx == EGL_NO_CONTEXT)
        return 1;
    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx);
    printf("GL_RENDERER: %s\nGL_VERSION: %s\n", glGetString(GL_RENDERER), glGetString(GL_VERSION));
    const char *ext = (const char *)glGetString(GL_EXTENSIONS);
    printf("BGRA8888: %s\n", ext && strstr(ext, "GL_EXT_texture_format_BGRA8888") ? "yes" : "no");
    return 0;
}
