---
name: osint
description: OSINT + recon recipes on this device - username search (sherlock, maigret), email recon (holehe, h8mail), domain harvest (theHarvester), SQLi (sqlmap), and the one-tap recon.sh flow with findings.
tools: shell
---

## One-tap recon (preferred)

- `sh /data/local/tmp/igexec.sh 'sh /root/recon.sh <target>'` — nmap
  connect scan + theHarvester crt.sh into `/root/scans/<target>/<ts>/`
  with `FINDINGS.md`, plus a JSONL line in `/root/.scan-history.jsonl`.
- Attested targets only. `scanme.nmap.org` is the sanctioned test target;
  `example.com` + crt.sh is passive (API query, no packets to target).

## Tool recipes (all installed in the guest, run via igexec)

- Usernames: `sherlock <name>` / `maigret <name>` (first run builds the
  site list; slow — allow minutes).
- Email: `holehe <addr>` (fast); `h8mail -t <addr> -c <config>` (needs
  API keys for most sources — without keys it still runs keyless checks).
- Domains: `PYTHONPATH=/root/tools/theHarvester python3 -u -c "import
  sys; sys.argv=['th','-d','<domain>','-l','20','-b','crtsh']; from
  theHarvester import __main__; import asyncio;
  asyncio.run(__main__.entry_point())"` (keyless; `-u` matters; the
  `-m theHarvester.theHarvester` form silently does nothing upstream —
  use this exact recipe).
- SQLi: `python3 /root/tools/sqlmap/sqlmap.py -u <url> --batch
  --level 1` (raise level only against your own targets).
- Binary triage: `python3 -c "from pwn import ELF; print(ELF('/bin/busybox').arch)"`
  (pwntools; installed `--no-deps` + deps — see below).

## Platform notes (Alpine guest, musl/aarch64)

- `pip install --break-system-packages` (PEP 668; disposable guest).
- PyPI `theHarvester` 0.0.1 is a fossil — real tool is git source at
  `/root/tools/theHarvester` (run with PYTHONPATH, `-u`).
- `playwright` has no musl wheels: a stub makes imports resolve; calling
  it raises loudly. Screenshot-taking sources are unavailable (their
  browsers couldn't install here either) — documented, not silent.
- pwntools: `unicorn` 2.1.4 ships musl wheels; the pinned older unicorn
  does not — keep 2.1.4 and install the rest around it.
- `apk` cannot write system paths under proot — use `pkginstall.sh`
  (wget+extract) or `apk add --no-cache` for userland paths only.

## Reporting

- Findings live as `FINDINGS.md` next to raw outputs; history is
  `/root/.scan-history.jsonl` (`ts`, `target`, `dir`).
- Report lists: target, evidence (file:line), impact, next step.
