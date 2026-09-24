/* libpathfix.so — rewrite Termux-baked absolute paths at runtime.
 *
 * Many Termux binaries (emacs, etc.) compile with
 *   --prefix=/data/data/com.termux/files/usr
 * baked into .rodata. Under Interlux the real tree lives at
 *   /data/user/0/<pkg>/files/userland
 * which is a different length, so we cannot patch strings in place.
 *
 * LD_PRELOAD this shim to rewrite every path syscall that starts with the
 * Termux prefix onto our prefix. Covers open/stat/access/mkdir/unlink/
 * rename/readlink/opendir/execve — enough for emacs dump generation and
 * normal batch runs.
 *
 * Build: aarch64-linux-android24-clang -shared -fPIC -O2 -o libpathfix.so
 *        pathfix.c -ldl   (16KB max-page-size)
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <dirent.h>
#include <errno.h>

static const char *OLD = "/data/data/com.termux/files/usr";
static const size_t OLD_LEN = 31; /* strlen("/data/data/com.termux/files/usr") */

static char NEWP[PATH_MAX];

static void init_paths(void) {
    if (NEWP[0]) return;
    const char *env = getenv("INTERLUX_PREFIX");
    if (env && env[0] == '/') {
        snprintf(NEWP, sizeof(NEWP), "%s", env);
    } else {
        snprintf(NEWP, sizeof(NEWP),
                 "/data/user/0/com.keneristudios.interlux/files/userland");
    }
}

__attribute__((constructor))
static void pathfix_init(void) {
    init_paths();
    /* Marker in our own files dir (app can always write there). */
    char marker[PATH_MAX];
    snprintf(marker, sizeof(marker), "%s/tmp/pathfix.loaded", NEWP);
    /* Direct syscall path: use openat to avoid re-entering via open(). */
    static int (*real_open)(const char *, int, ...) = NULL;
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    if (real_open) {
        int fd = real_open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) {
            const char *m = "pathfix loaded\n";
            ssize_t n = write(fd, m, 15);
            (void)n;
            close(fd);
        }
    }
}

/* Returns rewritten path. *tmp must be caller-provided PATH_MAX buffer.
 * If no rewrite needed, returns original pointer. */
static const char *fix(const char *p, char *tmp) {
    if (!p) return p;
    init_paths();
    if (strncmp(p, OLD, OLD_LEN) != 0) return p;
    /* Ensure boundary: next char is '/' or '\0' (avoid matching /usrfoo) */
    char c = p[OLD_LEN];
    if (c != '/' && c != '\0') return p;
    snprintf(tmp, PATH_MAX, "%s%s", NEWP, p + OLD_LEN);
    return tmp;
}

/* ---- open family ---- */
int open(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "open");
    char buf[PATH_MAX];
    const char *q = fix(path, buf);
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
    }
    return real(q, flags, mode);
}

int open64(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "open64");
    char buf[PATH_MAX];
    const char *q = fix(path, buf);
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
    }
    return real(q, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int, const char *, int, ...) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "openat");
    char buf[PATH_MAX];
    const char *q = fix(path, buf);
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
    }
    return real(dirfd, q, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int, const char *, int, ...) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "openat64");
    char buf[PATH_MAX];
    const char *q = fix(path, buf);
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
    }
    return real(dirfd, q, flags, mode);
}

/* bionic fortify */
int __open_2(const char *path, int flags) {
    static int (*real)(const char *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "__open_2");
    char buf[PATH_MAX];
    return real(fix(path, buf), flags);
}

int __openat_2(int dirfd, const char *path, int flags) {
    static int (*real)(int, const char *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "__openat_2");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), flags);
}

/* ---- stat family ---- */
int stat(const char *path, struct stat *st) {
    static int (*real)(const char *, struct stat *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "stat");
    char buf[PATH_MAX];
    return real(fix(path, buf), st);
}

int stat64(const char *path, struct stat64 *st) {
    static int (*real)(const char *, struct stat64 *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "stat64");
    char buf[PATH_MAX];
    return real(fix(path, buf), st);
}

int lstat(const char *path, struct stat *st) {
    static int (*real)(const char *, struct stat *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "lstat");
    char buf[PATH_MAX];
    return real(fix(path, buf), st);
}

int lstat64(const char *path, struct stat64 *st) {
    static int (*real)(const char *, struct stat64 *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "lstat64");
    char buf[PATH_MAX];
    return real(fix(path, buf), st);
}

int fstatat(int dirfd, const char *path, struct stat *st, int flags) {
    static int (*real)(int, const char *, struct stat *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "fstatat");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), st, flags);
}

int fstatat64(int dirfd, const char *path, struct stat64 *st, int flags) {
    static int (*real)(int, const char *, struct stat64 *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "fstatat64");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), st, flags);
}

int access(const char *path, int mode) {
    static int (*real)(const char *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "access");
    char buf[PATH_MAX];
    return real(fix(path, buf), mode);
}

int faccessat(int dirfd, const char *path, int mode, int flags) {
    static int (*real)(int, const char *, int, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "faccessat");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), mode, flags);
}

/* ---- mkdir / unlink / rename ---- */
int mkdir(const char *path, mode_t mode) {
    static int (*real)(const char *, mode_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "mkdir");
    char buf[PATH_MAX];
    return real(fix(path, buf), mode);
}

int mkdirat(int dirfd, const char *path, mode_t mode) {
    static int (*real)(int, const char *, mode_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "mkdirat");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), mode);
}

int unlink(const char *path) {
    static int (*real)(const char *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "unlink");
    char buf[PATH_MAX];
    return real(fix(path, buf));
}

int unlinkat(int dirfd, const char *path, int flags) {
    static int (*real)(int, const char *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "unlinkat");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), flags);
}

int rename(const char *old, const char *newp) {
    static int (*real)(const char *, const char *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "rename");
    char b1[PATH_MAX], b2[PATH_MAX];
    return real(fix(old, b1), fix(newp, b2));
}

int renameat(int olddirfd, const char *old, int newdirfd, const char *newp) {
    static int (*real)(int, const char *, int, const char *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "renameat");
    char b1[PATH_MAX], b2[PATH_MAX];
    return real(olddirfd, fix(old, b1), newdirfd, fix(newp, b2));
}

/* ---- readlink ---- */
ssize_t readlink(const char *path, char *linkbuf, size_t bufsiz) {
    static ssize_t (*real)(const char *, char *, size_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "readlink");
    char buf[PATH_MAX];
    return real(fix(path, buf), linkbuf, bufsiz);
}

ssize_t readlinkat(int dirfd, const char *path, char *linkbuf, size_t bufsiz) {
    static ssize_t (*real)(int, const char *, char *, size_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "readlinkat");
    char buf[PATH_MAX];
    return real(dirfd, fix(path, buf), linkbuf, bufsiz);
}

/* ---- opendir ---- */
DIR *opendir(const char *path) {
    static DIR *(*real)(const char *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "opendir");
    char buf[PATH_MAX];
    return real(fix(path, buf));
}

/* ---- execve (absolute Termux interpreters etc.) ---- */
int execve(const char *path, char *const argv[], char *const envp[]) {
    static int (*real)(const char *, char *const[], char *const[]) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "execve");
    char buf[PATH_MAX];
    return real(fix(path, buf), argv, envp);
}

int execv(const char *path, char *const argv[]) {
    static int (*real)(const char *, char *const[]) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "execv");
    char buf[PATH_MAX];
    return real(fix(path, buf), argv);
}

/* ---- chmod / chown (postinst-ish) ---- */
int chmod(const char *path, mode_t mode) {
    static int (*real)(const char *, mode_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "chmod");
    char buf[PATH_MAX];
    return real(fix(path, buf), mode);
}

int chown(const char *path, uid_t u, gid_t g) {
    static int (*real)(const char *, uid_t, gid_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "chown");
    char buf[PATH_MAX];
    return real(fix(path, buf), u, g);
}
