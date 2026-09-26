---
name: alpine-guest
description: Work inside the on-device Alpine proot guest - install packages, run the pentest suite, and handle proot quirks (iroot, pkginstall.sh, pentest.sh).
tools: shell
---

## Run commands in the guest

- One-shot: `sh /data/local/tmp/igexec.sh '<cmd>'` (proot root = `$HOME/.rootfs`, busybox sh).
- Long installs MUST run in the foreground: background jobs die when proot exits.
- Guest downloads cache at `/tmp/pkgs` (DL= resumes; size mismatch self-heals).

## Packages

- Install: `sh /data/local/tmp/igexec.sh 'sh /root/pkginstall.sh <pkg>...'`
- v2.9 resolver = one awk pass over the 13MB APKINDEX (~1-3s/lookup); follows
  plain-name deps and virtual provides (`p:` tokens), skips `!`/`pc:`/`if:`
  entries; prints dependencies first, the package last.
- Unknown package or missing provider -> non-zero exit + stderr reason
  (never silent failure).

## Pentest suite

- Verify all tools: `sh /data/local/tmp/igexec.sh 'sh /root/pentest.sh verify'`
- Proven green: nmap 7.99 (+NSE), python3 3.14, pip, git, curl, bash, tmux,
  dig (bind-tools), vim, whois, nikto 2.6.0, hydra 9.6, ffuf, john, tcpdump.
- nikto needed `perl-json` + `perl-xml-writer` (missing from Alpine's D: line).
- hydra needed its 99-package dependency closure; both are already installed.

## Limits and gotchas

- Rootless: no raw sockets, no monitor mode, no HID. Recon / web testing /
  scripting / connect scans only.
- A package that "is installed" but whose binary fails likely has a stale
  shared library: remove + reinstall it (this fixed llama-server once).
- Approvals and the audit trail still apply to every command you run here.
