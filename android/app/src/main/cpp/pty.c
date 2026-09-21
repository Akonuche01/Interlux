#define _GNU_SOURCE

#include "pty.h"
#include <errno.h>
#include <pty.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include <android/log.h>

#define TAG "interlux-pty"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

#include <signal.h>

// A native crash (bad pointer in the pty path) is invisible to Java's
// UncaughtExceptionHandler, so log it ourselves before the process dies.
static void on_native_crash(int sig) {
  LOGE("native crash: signal %d", sig);
  _exit(128 + sig);
}

__attribute__((constructor)) static void setup_crash_handler(void) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_native_crash;
  sigaction(SIGSEGV, &sa, NULL);
  sigaction(SIGABRT, &sa, NULL);
  sigaction(SIGBUS, &sa, NULL);
}

JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeCreate(JNIEnv *env, jobject thiz) {
  int master_fd = -1;
  pid_t pid = forkpty(&master_fd, NULL, NULL, NULL);

  if (pid < 0) {
    LOGE("forkpty failed: %s", strerror(errno));
    return -1;
  }

  if (pid == 0) {
    // Child: become the session leader's controlling shell. Close the master
    // side first so we hold no stale reference to it.
    close(master_fd);

    // A terminal wants sane defaults; raw mode is the pty's natural state but
    // echoing and signal handling should behave like a normal interactive
    // shell for the user.
    struct termios t;
    if (tcgetattr(STDIN_FILENO, &t) == 0) {
      t.c_lflag |= ISIG | ICANON | ECHO | ECHOCTL | ECHOE | ECHOK | ECHONL;
      t.c_iflag |= ICRNL;
      t.c_oflag |= ONLCR;
      tcsetattr(STDIN_FILENO, TCSANOW, &t);
    }

    // Give the shell a sane terminal type.
    setenv("TERM", "xterm-256color", 1);
    setenv("LANG", "C.UTF-8", 1);

    char *const argv[] = {"/system/bin/sh", NULL};
    execv(argv[0], argv);

    // Only reached if exec failed.
    LOGE("execv failed: %s", strerror(errno));
    _exit(127);
  }

  LOGI("forkpty ok: pid=%d master_fd=%d", pid, master_fd);
  return master_fd;
}

JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeRead(JNIEnv *env, jobject thiz,
                                                   jint fd, jbyteArray buf,
                                                   jint len) {
  if (fd < 0 || buf == NULL || len <= 0) {
    return -1;
  }

  jbyte *native_buf = (*env)->GetByteArrayElements(env, buf, NULL);
  if (native_buf == NULL) {
    return -1;
  }

  ssize_t n = read(fd, native_buf, (size_t)len);

  (*env)->ReleaseByteArrayElements(env, buf, native_buf, 0);
  return (jint)n;
}

JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeWrite(JNIEnv *env, jobject thiz,
                                                    jint fd, jbyteArray buf,
                                                    jint len) {
  if (fd < 0 || buf == NULL || len <= 0) {
    return -1;
  }

  jbyte *native_buf = (*env)->GetByteArrayElements(env, buf, NULL);
  if (native_buf == NULL) {
    return -1;
  }

  ssize_t n = write(fd, native_buf, (size_t)len);

  (*env)->ReleaseByteArrayElements(env, buf, native_buf, JNI_ABORT);
  return (jint)n;
}

JNIEXPORT void JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeResize(JNIEnv *env, jobject thiz,
                                                     jint fd, jint cols,
                                                     jint rows) {
  if (fd < 0) {
    return;
  }

  struct winsize ws;
  memset(&ws, 0, sizeof(ws));
  ws.ws_col = (unsigned short)cols;
  ws.ws_row = (unsigned short)rows;

  if (ioctl(fd, TIOCSWINSZ, &ws) != 0) {
    LOGE("TIOCSWINSZ failed: %s", strerror(errno));
  }
}

JNIEXPORT void JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeClose(JNIEnv *env, jobject thiz,
                                                    jint fd) {
  if (fd >= 0) {
    close(fd);
  }
}
