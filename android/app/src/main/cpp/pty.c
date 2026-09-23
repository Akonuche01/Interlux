#define _GNU_SOURCE

#include "pty.h"
#include <errno.h>
#include <limits.h>
#include <pty.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
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

// Path of the bundled busybox multicall launcher relative to the userland dir.
// It is a bionic binary whose interpreter is /system/bin/linker64, so the only
// runtime requirement is LD_LIBRARY_PATH pointing at the userland dir.
#define BUSYBOX_NAME "busybox"

JNIEXPORT jint JNICALL
Java_com_keneristudios_interlux_pty_Pty_nativeCreate(JNIEnv *env, jobject thiz,
                                                     jstring userland_path) {
  const char *userland = NULL;
  if (userland_path != NULL) {
    userland = (*env)->GetStringUTFChars(env, userland_path, NULL);
  }

  int master_fd = -1;
  pid_t pid = forkpty(&master_fd, NULL, NULL, NULL);

  if (pid < 0) {
    LOGE("forkpty failed: %s", strerror(errno));
    if (userland != NULL) {
      (*env)->ReleaseStringUTFChars(env, userland_path, userland);
    }
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

    // Prefer the bundled bionic userland. argv[0]="sh" makes the multicall
    // launcher dispatch to the ash applet, and standalone shell mode resolves
    // coreutils applets in-process (no symlink forest required).
    if (userland != NULL) {
      char busybox[PATH_MAX];

      char path_env[PATH_MAX * 2];
      snprintf(path_env, sizeof(path_env),
               "%s/bin:%s:%s/home/.local/bin:/vendor/bin:/system/xbin:/system/bin",
               userland, userland, userland);
      setenv("PATH", path_env, 1);

      // The launchers dlopen their .so files out of these paths (flat root
      // for the stage-2/3 libs, lib/ for the Option A power set).
      {
        char ld[PATH_MAX * 2], terminfo[PATH_MAX], pyhome[PATH_MAX];
        snprintf(ld, sizeof(ld), "%s/lib:%s", userland, userland);
        snprintf(terminfo, sizeof(terminfo), "%s/share/terminfo", userland);
        snprintf(pyhome, sizeof(pyhome), "%s", userland);
        setenv("LD_LIBRARY_PATH", ld, 1);
        setenv("TERMINFO", terminfo, 1);
        setenv("PYTHONHOME", pyhome, 1);
      }

      // Stage 3: give tools a Termux-like layout. HOME is the writable home,
      // PREFIX points at the userland root, TMPDIR is app-private scratch,
      // and ENV makes `sh -i` source our profile (PATH/PS1/fetch helper).
      // 3a: SSL_CERT_FILE (+ aliases) points at the bundled Mozilla CA bundle
      // so HTTPS verifies even though the app sandbox has no system CA store.
      {
        char home[PATH_MAX], tmp[PATH_MAX], env[PATH_MAX], ca[PATH_MAX];
        snprintf(home, sizeof(home), "%s/home", userland);
        snprintf(tmp, sizeof(tmp), "%s/tmp", userland);
        snprintf(env, sizeof(env), "%s/etc/profile", userland);
        snprintf(ca, sizeof(ca), "%s/etc/ssl/certs/ca-certificates.crt",
                 userland);
        setenv("HOME", home, 1);
        setenv("PREFIX", userland, 1);
        setenv("TMPDIR", tmp, 1);
        setenv("ENV", env, 1);
        setenv("SSL_CERT_FILE", ca, 1);
        setenv("CURL_CA_BUNDLE", ca, 1);
        setenv("REQUESTS_CA_BUNDLE", ca, 1);
      }

      // Bash reads BASH_ENV for interactive non-login shells, so point it
      // at our profile too (ENV covers ash/dash).
      {
        char bash_env[PATH_MAX];
        snprintf(bash_env, sizeof(bash_env), "%s/etc/profile", userland);
        setenv("BASH_ENV", bash_env, 1);
      }

      // ash only expands \w/\$ PS1 when it is a login/interactive shell with
      // a profile; keep the prompt simple and predictable.
      setenv("PS1", "interlux:\\w\\$ ", 1);

      // Prefer bash as the login shell (Phase 1.4); fall back to the bundled
      // busybox ash, then to the system shell, so a broken userland never
      // leaves the terminal dead on arrival.
      {
        char bash[PATH_MAX];
        snprintf(bash, sizeof(bash), "%s/bin/bash", userland);
        if (access(bash, X_OK) == 0) {
          LOGI("exec bundled shell: %s", bash);
          char *const argv[] = {"bash", "-i", NULL};
          execv(bash, argv);
          LOGE("bash execv failed: %s", strerror(errno));
        } else {
          LOGE("bundled bash not executable: %s", bash);
        }
      }

      snprintf(busybox, sizeof(busybox), "%s/%s", userland, BUSYBOX_NAME);
      if (access(busybox, X_OK) == 0) {
        LOGI("exec bundled shell: %s", busybox);
        char *const argv[] = {"sh", "-i", NULL};
        execv(busybox, argv);
        // Fall through to the system shell if the bundled one cannot exec.
        LOGE("bundled execv failed: %s", strerror(errno));
      } else {
        LOGE("bundled busybox not executable: %s", busybox);
      }
    }

    // Fallback: Android's own shell, so a broken userland never leaves the
    // terminal dead on arrival.
    setenv("PS1", "\\$ ", 1);
    char *const argv[] = {"/system/bin/sh", NULL};
    execv(argv[0], argv);

    // Only reached if exec failed.
    LOGE("execv failed: %s", strerror(errno));
    _exit(127);
  }

  LOGI("forkpty ok: pid=%d master_fd=%d", pid, master_fd);
  if (userland != NULL) {
    (*env)->ReleaseStringUTFChars(env, userland_path, userland);
  }
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
  if (fd < 0) {
    return;
  }

  // Closing the master fd sends SIGHUP to the child's foreground process
  // group, which is what actually terminates the shell. Closing first also
  // unblocks a reader thread stuck in read() on this fd.
  close(fd);

  // forkpty leaves us as the parent of the shell; if we never wait the child
  // stays as a zombie for the life of the process. Reap it, but do not block
  // forever: a wedged shell should not pin the caller.
  int status;
  for (int i = 0; i < 20; i++) {  // up to ~2s
    pid_t w = waitpid(-1, &status, WNOHANG);
    if (w > 0) {
      break;
    }
    if (w < 0 && errno != EINTR) {
      break;
    }
    usleep(100000);  // 100ms
  }
}
