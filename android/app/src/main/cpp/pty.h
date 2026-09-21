#ifndef INTERLUX_PTY_H
#define INTERLUX_PTY_H

#include <jni.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Create a new pseudo-terminal and spawn a shell as its child.
 *
 * @return the master fd (>= 0) on success, or -1 on failure. The child side
 *         of the pty is exec'd into /system/bin/sh and never returns.
 */
JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeCreate(JNIEnv *env, jobject thiz);

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
