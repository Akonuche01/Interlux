#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <string.h>

static volatile int g_caught;
static void handler(int sig, siginfo_t *si, void *u) {
    char buf[128];
    g_caught = 1;
    int n = snprintf(buf, sizeof buf, "BLOCKED(si_code=%d si_syscall=%d)\n", si->si_code, si->si_syscall);
    write(2, buf, n);
}
struct t { const char *name; long nr; };
static const struct t tests[] = {
  {"set_robust_list",__NR_set_robust_list},{"get_robust_list",__NR_get_robust_list},
  {"faccessat2",__NR_faccessat2},{"clone3",__NR_clone3},{"execveat",__NR_execveat},
  {"openat2",__NR_openat2},{"bpf",__NR_bpf},{"perf_event_open",__NR_perf_event_open},
  {"userfaultfd",__NR_userfaultfd},{"ptrace",__NR_ptrace},{"process_vm_readv",__NR_process_vm_readv},
  {"process_vm_writev",__NR_process_vm_writev},{"seccomp",__NR_seccomp},{"personality",__NR_personality},
  {"unshare",__NR_unshare},{"setns",__NR_setns},{"chroot",__NR_chroot},{"mount",__NR_mount},
  {"pivot_root",__NR_pivot_root},{"reboot",__NR_reboot},{"swapon",__NR_swapon},
  {"nfsservctl",__NR_nfsservctl},{"quotactl",__NR_quotactl},{"capset",__NR_capset},
  {"sethostname",__NR_sethostname},{"setdomainname",__NR_setdomainname},
  {"io_uring_setup",__NR_io_uring_setup},{"pidfd_open",__NR_pidfd_open},
  {"landlock_create_ruleset",__NR_landlock_create_ruleset},{"process_mrelease",__NR_process_mrelease},
  {"futex",__NR_futex},{"clone",__NR_clone},{"execve",__NR_execve},{"rt_sigaction",__NR_rt_sigaction},
  {"mmap",__NR_mmap},{"openat",__NR_openat},{"close",__NR_close},{"getdents64",__NR_getdents64},
  {"socket",__NR_socket},{"connect",__NR_connect},{"sendto",__NR_sendto},{"recvfrom",__NR_recvfrom},
  {"setsockopt",__NR_setsockopt},{"getsockopt",__NR_getsockopt},{"pipe2",__NR_pipe2},
  {"dup3",__NR_dup3},{"ioctl",__NR_ioctl},{"ppoll",__NR_ppoll},{"pselect6",__NR_pselect6},
  {"fchmodat",__NR_fchmodat},{"fchownat",__NR_fchownat},{"renameat",__NR_renameat},
  {"unlinkat",__NR_unlinkat},{"linkat",__NR_linkat},{"symlinkat",__NR_symlinkat},
  {"readlinkat",__NR_readlinkat},{"fstat",__NR_fstat},{"statfs",__NR_statfs},{"fstatfs",__NR_fstatfs},
  {"getcwd",__NR_getcwd},{"chdir",__NR_chdir},{"uname",__NR_uname},{"sysinfo",__NR_sysinfo},
  {"times",__NR_times},{"gettimeofday",__NR_gettimeofday},{"clock_gettime",__NR_clock_gettime},
  {"getrandom",__NR_getrandom},{"nanosleep",__NR_nanosleep},{"madvise",__NR_madvise},
  {"mincore",__NR_mincore},{"mlock",__NR_mlock},{"munlock",__NR_munlock},{"mremap",__NR_mremap},
  {"brk",__NR_brk},{"mprotect",__NR_mprotect},{"munmap",__NR_munmap},{"set_tid_address",__NR_set_tid_address},
  {"prctl",__NR_prctl},{"kill",__NR_kill},{"tgkill",__NR_tgkill},{"tkill",__NR_tkill},
  {"wait4",__NR_wait4},{"setuid",__NR_setuid},{"setgid",__NR_setgid},{"setresuid",__NR_setresuid},
  {"sched_getaffinity",__NR_sched_getaffinity},{"sched_setaffinity",__NR_sched_setaffinity},
  {"fadvise64",__NR_fadvise64},{"fallocate",__NR_fallocate},{"ftruncate",__NR_ftruncate},
  {"fsync",__NR_fsync},{"fdatasync",__NR_fdatasync},{"msync",__NR_msync},{"sigaltstack",__NR_sigaltstack},
  {"sched_yield",__NR_sched_yield},{"getcpu",__NR_getcpu},{"getppid",__NR_getppid},
  {"setsid",__NR_setsid},{"setpgid",__NR_setpgid},{"umask",__NR_umask},
  {"setpriority",__NR_setpriority},{"getpriority",__NR_getpriority},{"setitimer",__NR_setitimer},
  {"timer_create",__NR_timer_create},
  {0,0}
};
int main(int argc, char **argv) {
    int only = (argc > 1) ? atoi(argv[1]) : -1;
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = handler;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSYS, &sa, 0);
    for (int i = 0; tests[i].name; i++) {
        if (only >= 0 && i != only) continue;
        printf("%-22s ", tests[i].name); fflush(stdout);
        pid_t p = fork();
        if (p == 0) {
            long r = syscall(tests[i].nr, 0, 0, 0, 0);
            printf("ok ret=%ld\n", r); fflush(stdout);
            _exit(0);
        }
        int st; waitpid(p, &st, 0);
        if (WIFSIGNALED(st)) printf(">>> KILLED by signal %d (%s)\n", WTERMSIG(st),
            WTERMSIG(st)==31?"SIGSYS": WTERMSIG(st)==4?"SIGILL":"other");
        fflush(stdout);
    }
    return 0;
}
