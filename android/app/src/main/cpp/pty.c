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

// forkpty hands us the child pid, but nativeClose only ever receives the
// master fd -- the pid used to be dropped on the floor here, so nativeClose
// had to fall back to waitpid(-1, ...), which reaps ANY child. If another
// tab's shell had already exited, THAT one got reaped and this session's own
// shell was left as a zombie, unreachable, for the life of the process.
// Keep the pid per fd so we can wait on our own child. 0 means "none
// recorded" -- a real pid is always > 0.
#define PTY_MAX_FD 4096
static pid_t g_child_pid[PTY_MAX_FD];

#include <signal.h>

// A native crash (bad pointer in the pty path) is invisible to Java's
// UncaughtExceptionHandler, so log it ourselves before the process dies.
//
// The message must go out with write(2), not LOGE/__android_log_print:
// android_log_print takes internal locks and writes to logd, and it is NOT on
// the async-signal-safe list. When the crash was heap corruption or a
// lock-order problem touching that same allocator/logd state -- precisely the
// case this handler exists to diagnose -- the old log call could deadlock and
// leave the process hung with no diagnostic at all. write() on the already
// open STDERR_FILENO cannot deadlock. snprintf is unsafe here too, so the
// signal number is formatted by hand.
static void write_all(int fd, const char *buf, size_t len) {
  while (len > 0) {
    ssize_t w = write(fd, buf, len);
    if (w <= 0) {
      return;
    }
    buf += w;
    len -= (size_t)w;
  }
}

static void on_native_crash(int sig) {
  char msg[64];
  size_t n = 0;
  const char *prefix = "native crash: signal ";
  while (prefix[n] != '\0') {
    msg[n] = prefix[n];
    n++;
  }
  char digits[12];
  int nd = 0;
  int v = sig;
  if (v <= 0) {
    digits[nd++] = '0';
  } else {
    while (v > 0 && nd < (int)sizeof(digits)) {
      digits[nd++] = (char)('0' + (v % 10));
      v /= 10;
    }
  }
  while (nd > 0 && n + 1 < sizeof(msg)) {
    msg[n++] = digits[--nd];
  }
  if (n + 1 < sizeof(msg)) {
    msg[n++] = '\n';
  }
  write_all(STDERR_FILENO, msg, n);
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

  // ---------------------------------------------------------------------
  // Everything the shell needs is resolved HERE, in the parent, before
  // forkpty.
  //
  // After a fork in a multi-threaded process the child may only call
  // async-signal-safe functions. This JVM has many live threads, and if one
  // of them held the malloc arena lock, a libc lock or the logd mutex at the
  // instant of the fork, the child inherits that lock HELD FOREVER -- and
  // what it used to do first was setenv (mallocs), snprintf and
  // LOGE/__android_log_print (takes the logd lock). Any of those could
  // deadlock the child before it ever reached exec: an intermittent,
  // unloggable hang at terminal start, which is precisely the "terminal dead
  // on arrival" symptom the comments below exist to prevent.
  //
  // So: build a complete envp and choose the shell up here, and let the child
  // do nothing but fix the termios and execve().
  // ---------------------------------------------------------------------

  // "NAME=VALUE" entries. These live in this frame, which forkpty copies
  // into the child, so the child itself allocates and locks nothing.
  static char e_term[] = "TERM=xterm-256color";
  static char e_lang[] = "LANG=C.UTF-8";
  static char e_openssl[] = "OPENSSL_CONF=/dev/null";
  static char e_ps1_bundled[] = "PS1=interlux:\\w\\$ ";
  static char e_ps1_system[] = "PS1=\\$ ";

  // Declared here, not inside the userland branch: `shell` points into one
  // of these, and it must outlive that block.
  char bash[PATH_MAX], busybox[PATH_MAX];
  char e_path[PATH_MAX * 2], e_ld[PATH_MAX * 2], e_terminfo[PATH_MAX];
  char e_pyhome[PATH_MAX], e_home[PATH_MAX], e_prefix[PATH_MAX];
  char e_tmp[PATH_MAX], e_profile[PATH_MAX], e_ssl[PATH_MAX];
  char e_curl[PATH_MAX], e_req[PATH_MAX], e_bash_env[PATH_MAX];

  char *envp[24];
  int envc = 0;

  const char *shell = "/system/bin/sh";
  char *const argv_system[] = {"/system/bin/sh", NULL};
  char *const argv_bash[] = {(char *)"bash", (char *)"-i", NULL};
  char *const argv_ash[] = {(char *)"sh", (char *)"-i", NULL};
  char *const *shell_argv = argv_system;
  int shell_is_bundled = 0;

  envp[envc++] = e_term;
  envp[envc++] = e_lang;

  if (userland != NULL) {
    // Prefer bash as the login shell (Phase 1.4); fall back to the bundled
    // busybox ash, then to the system shell, so a broken userland never
    // leaves the terminal dead on arrival. argv[0]="sh" makes the multicall
    // launcher dispatch to the ash applet, and standalone shell mode
    // resolves coreutils applets in-process (no symlink forest required).
    snprintf(bash, sizeof(bash), "%s/bin/bash", userland);
    snprintf(busybox, sizeof(busybox), "%s/%s", userland, BUSYBOX_NAME);
    if (access(bash, X_OK) == 0) {
      LOGI("exec bundled shell: %s", bash);
      shell = bash;
      shell_argv = argv_bash;
      shell_is_bundled = 1;
    } else if (access(busybox, X_OK) == 0) {
      LOGI("exec bundled shell: %s", busybox);
      shell = busybox;
      shell_argv = argv_ash;
      shell_is_bundled = 1;
    } else {
      LOGE("bundled bash not executable: %s", bash);
      LOGE("bundled busybox not executable: %s", busybox);
    }

    snprintf(e_path, sizeof(e_path),
             "PATH=%s/bin:%s:%s/home/.local/bin:/vendor/bin:/system/xbin:/system/bin",
             userland, userland, userland);
    // The launchers dlopen their .so files out of these paths (flat root
    // for the stage-2/3 libs, lib/ for the Option A power set).
    snprintf(e_ld, sizeof(e_ld), "LD_LIBRARY_PATH=%s/lib:%s", userland, userland);
    snprintf(e_terminfo, sizeof(e_terminfo), "TERMINFO=%s/share/terminfo", userland);
    snprintf(e_pyhome, sizeof(e_pyhome), "PYTHONHOME=%s", userland);
    // Stage 3: give tools a Termux-like layout. HOME is the writable home,
    // PREFIX points at the userland root, TMPDIR is app-private scratch,
    // and ENV makes `sh -i` source our profile (PATH/PS1/fetch helper).
    // 3a: SSL_CERT_FILE (+ aliases) points at the bundled Mozilla CA bundle
    // so HTTPS verifies even though the app sandbox has no system CA store.
    snprintf(e_home, sizeof(e_home), "HOME=%s/home", userland);
    snprintf(e_prefix, sizeof(e_prefix), "PREFIX=%s", userland);
    snprintf(e_tmp, sizeof(e_tmp), "TMPDIR=%s/tmp", userland);
    snprintf(e_profile, sizeof(e_profile), "ENV=%s/etc/profile", userland);
    snprintf(e_ssl, sizeof(e_ssl),
             "SSL_CERT_FILE=%s/etc/ssl/certs/ca-certificates.crt", userland);
    snprintf(e_curl, sizeof(e_curl),
             "CURL_CA_BUNDLE=%s/etc/ssl/certs/ca-certificates.crt", userland);
    snprintf(e_req, sizeof(e_req),
             "REQUESTS_CA_BUNDLE=%s/etc/ssl/certs/ca-certificates.crt", userland);
    // Bash reads BASH_ENV for interactive non-login shells, so point it at
    // our profile too (ENV covers ash/dash).
    snprintf(e_bash_env, sizeof(e_bash_env), "BASH_ENV=%s/etc/profile", userland);
    // OpenSSL's default config lives at a Termux-baked path that is
    // unreadable here; node treats that as FATAL at crypto init. Point it
    // at /dev/null (empty config, system defaults). Users can export their
    // own OPENSSL_CONF to override.

    envp[envc++] = e_path;
    envp[envc++] = e_ld;
    envp[envc++] = e_terminfo;
    envp[envc++] = e_pyhome;
    envp[envc++] = e_home;
    envp[envc++] = e_prefix;
    envp[envc++] = e_tmp;
    envp[envc++] = e_profile;
    envp[envc++] = e_ssl;
    envp[envc++] = e_curl;
    envp[envc++] = e_req;
    envp[envc++] = e_bash_env;
    envp[envc++] = e_openssl;
  }

  // ash only expands \w/\$ PS1 when it is a login/interactive shell with a
  // profile; keep the prompt simple and predictable.
  envp[envc++] = shell_is_bundled ? e_ps1_bundled : e_ps1_system;
  envp[envc] = NULL;

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
    // Child. Nothing in here may allocate, lock or log -- see the comment
    // above the envp construction: close(), tcsetattr()/ioctl and execve()
    // are all async-signal-safe, and that is now the entire path. The shell,
    // its argv and its whole environment were resolved in the parent.

    // Become the session leader's controlling shell. Close the master side
    // first so we hold no stale reference to it.
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

    execve(shell, (char *const *)shell_argv, envp);

    // Only reached if exec failed. There is nothing safe to report it with
    // from a forked child, so just leave the documented 127.
    _exit(127);
  }

  LOGI("forkpty ok: pid=%d master_fd=%d", pid, master_fd);
  // Remember which child belongs to this master fd so nativeClose can reap
  // exactly that one instead of whichever child exited first. See g_child_pid.
  if (master_fd >= 0 && master_fd < PTY_MAX_FD) {
    g_child_pid[master_fd] = pid;
  }
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

  // Clamp to the array we were actually handed. `len` arrives from the caller
  // and, until now, was only checked for > 0 -- never against the real array
  // length. A stale or mismatched length from any future caller would make
  // read() write straight past the pinned array into the Java heap, so the
  // bound belongs here at the JNI edge rather than being trusted from Kotlin.
  jsize capacity = (*env)->GetArrayLength(env, buf);
  if (capacity <= 0) {
    return -1;
  }
  if (len > capacity) {
    len = capacity;
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

  // Same clamp as nativeRead: bound `len` by the array we were handed rather
  // than trusting the caller's value (see the comment there).
  jsize capacity = (*env)->GetArrayLength(env, buf);
  if (capacity <= 0) {
    return -1;
  }
  if (len > capacity) {
    len = capacity;
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
  // stays as a zombie for the life of the process. Reap OUR child (recorded
  // at nativeCreate, see g_child_pid) rather than whichever child happened to
  // exit first, but do not block forever: a wedged shell should not pin the
  // caller.
  pid_t child = 0;
  if (fd < PTY_MAX_FD) {
    child = g_child_pid[fd];
    g_child_pid[fd] = 0;
  }
  int status;
  for (int i = 0; i < 20; i++) {  // up to ~2s
    pid_t w = waitpid(child > 0 ? child : -1, &status, WNOHANG);
    if (w > 0) {
      break;
    }
    if (w < 0 && errno != EINTR) {
      break;
    }
    usleep(100000);  // 100ms
  }
}
