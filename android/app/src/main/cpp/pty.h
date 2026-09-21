#ifndef INTERLUX_PTY_H
#define INTERLUX_PTY_H

#include <jni.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Create a new pseudo-terminal and spawn a shell as its child.
 *
 * @param userland_path absolute path to the extracted bundled userland dir, or
 *                      NULL to use the system shell directly. When non-NULL the
 *                      child execs <userland_path>/busybox as an interactive
 *                      `sh` (ash), falling back to /system/bin/sh on failure.
 * @return the master fd (>= 0) on success, or -1 on failure.
 */
JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeCreate(JNIEnv *env, jobject thiz,
                                                     jstring userland_path);

/**
 * Read up to `len` bytes from the master side of the pty.
 *
 * @return the number of bytes read, or -1 on error/EOF.
 */
JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeRead(JNIEnv *env, jobject thiz,
                                                   jint fd, jbyteArray buf,
                                                   jint len);

/**
 * Write `len` bytes to the master side of the pty (i.e. send input to the
 * shell).
 *
 * @return the number of bytes written, or -1 on error.
 */
JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeWrite(JNIEnv *env, jobject thiz,
                                                    jint fd, jbyteArray buf,
                                                    jint len);

/**
 * Notify the child that the terminal window size changed (TIOCSWINSZ).
 */
JNIEXPORT void JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeResize(JNIEnv *env, jobject thiz,
                                                     jint fd, jint cols,
                                                     jint rows);

/** Close the master fd. */
JNIEXPORT void JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeClose(JNIEnv *env, jobject thiz,
                                                    jint fd);

#ifdef __cplusplus
}
#endif

#endif  // INTERLUX_PTY_H
