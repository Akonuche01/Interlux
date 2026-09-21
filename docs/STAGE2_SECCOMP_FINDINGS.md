# Stage 2 — Device Seccomp Findings (authoritative)

Tested on the actual device: Android 15, API 35, `untrusted_app_27` SELinux context.
Method: `native/seccomp-probe/probe8.c` — normal (bionic, dynamic) C program that
forks per syscall, installs a `SIGSYS` handler, and reports which syscalls the
kernel's seccomp filter traps. Each syscall runs in its own child process.

## Blocked syscalls (SIGSYS, catchable)

| syscall | nr | consequence |
|---|---|---|
| `set_robust_list` | 99 | glibc AND musl call this in pthread/TLS init -> static binaries die instantly |
| `get_robust_list` | 100 | same family |
| `faccessat2` | 439 | newer libc path checks must fall back to `faccessat` |
| `openat2` | 437 | must use `openat` |
| `chroot` | 51 | no classic chroot isolation |
| `mount` | 40 | no mounting |
| `pivot_root` | ok (EPERM) | - |
| `reboot` | 142 | - |
| `swapon` | 224 | - |
| `nfsservctl` | 42 | - |
| `sethostname` | 161 | - |
| `setdomainname` | 162 | - |
| `io_uring_setup` | 425 | no io_uring |
| `landlock_create_ruleset` | 444 | no landlock |

## Allowed (return EPERM/ENOSYS, not trapped)

`ptrace`, `clone3`, `execveat`, `bpf`, `perf_event_open`, `userfaultfd`,
`process_vm_readv/writev`, `seccomp`, `personality`, `unshare`, `setns`,
`quotactl`, `capset`, `pidfd_open`, plus all normal file/network/memory syscalls.

**`ptrace` is allowed** -> proot-style syscall interception is viable (this is how
Termux runs glibc-based software).

## What this decides for the Interlux userland

Tested on device, same clean environment (`env -i`):

| binary | result |
|---|---|
| static glibc BusyBox (1.29.3 "polaco") | DIES - SIGSYS from `set_robust_list` |
| static musl BusyBox (Alpine 1.37.0) | DIES - SIGSYS from `set_robust_list` |
| **bionic dynamic BusyBox (Termux 1.38.0)** | **WORKS** - full `ash` shell, all applets |

The bionic BusyBox runs using `/system/bin/linker64` (Android's own linker, present
on every device) and depends only on system libs: `libc.so`, `libm.so`,
`libandroid-selinux.so`.

### Decision

Interlux ships **bionic-linked dynamic binaries**, never static glibc/musl.
A fully self-contained userland = two files:

- `busybox` (bionic, interpreter `/system/bin/linker64`)
- `libbusybox.so.1.38.0`

run with `LD_LIBRARY_PATH` pointing at the app's extracted dir. Verified working in
a clean `env -i` environment with no Termux on PATH.

### Reproduce

```
clang -O2 -o probe8 native/seccomp-probe/probe8.c && ./probe8
```
