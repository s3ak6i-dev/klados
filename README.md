# Klados

> **The durable, forkable runtime for AI agents.** Snapshot any running agent — filesystem, processes, memory, browser, conversation state — in under a second. Resume it tomorrow. Fork it into ten parallel timelines. Rewind it to any prior step.

**Git for running agents.** Git made source code durable, forkable, and time‑travelable, and an industry grew on those three verbs. Klados does the same for *live execution state*.

![status](https://img.shields.io/badge/status-M0%20engine%20working-brightgreen) ![primitives](https://img.shields.io/badge/core%20primitives-validated-brightgreen) ![license](https://img.shields.io/badge/license-Apache--2.0-blue) ![platform](https://img.shields.io/badge/platform-Linux%2FKVM-lightgrey)

---

## The core idea

The primitive is the **snapshot**: a complete, consistent, content‑addressed capture of an agent's execution state. From a snapshot you can do three things:

1. **Resume** — continue execution exactly where it stopped, on any machine, at any time.
2. **Fork** — spawn N copy‑on‑write children that diverge from the same instant.
3. **Rewind** — restore any earlier snapshot in a run's timeline and re‑execute from there.

These solve four recurring failures in agent infrastructure: **crash amnesia** (a 35‑minute run dies and everything is lost), **serial exploration** (tree‑shaped agentic search can't cheaply duplicate a mid‑task starting point), **unreproducible debugging** (you have a transcript at step 47 but not the state), and **idle burn** (paying for warm VMs while agents wait on approvals or CI).

## Status

Early‑stage, but past the risky part. This repo contains the product/technical spec, a suite of **de‑risking spikes** that falsify the hardest technical claims on real Firecracker microVMs, and a **working M0 engine** (`kladosd` daemon + `klad` CLI) that runs the full flow: boot an agent, snapshot it, concurrently fork it, and render the timeline. Everything was validated on a laptop via WSL2 nested KVM — so *correctness, economics, and fidelity* results are trustworthy, while *absolute latency* awaits bare metal (nested virt distorts wall‑times).

**Every headline claim in the spec now has working, tested evidence.** The remaining work is productization (Rust rewrite, content‑addressed dedup storage, control plane, SDKs, Timeline UI) and the one spend‑gated item (bare‑metal latency) — not open research.

### What has been validated

| Claim | Result | Spike |
|---|---|---|
| **CoW memory fork economics** | 16 forks of a 2 GB base share memory at a **0.10** ratio (154 MiB vs 1,472 naive); improves with fork count | `spike_b` |
| **CoW disk economics** | 16 forks, **fork‑time disk cost = 0** (empty overlay uppers), per‑fork cost = divergence only, **0.147** ratio | `spike_d_disk` |
| **Entropy divergence is real** | Forked children emit **identical** userspace RNG *and* OpenSSL/TLS client randoms (65/65 collisions) — a security hazard | `spike_c` |
| **Entropy fix — zero‑window, transparent** | Same **unmodified** app → **0 collisions** via the guest‑agent fork protocol (reseed before the workload resumes) | `spike_g` |
| **Clock fixup on restore** | ~9.7 s stale after an 8 s pause → **<1 ms skew** after the fork protocol steps the clock | `spike_e`, `spike_g` |
| **Branch‑context injection** | Each fork receives its distinct per‑branch prompt over vsock | `spike_g` |
| **Incremental snapshot algorithm** | Diff snapshot writes **6.1%** of a full snapshot's bytes (hardware‑independent) | `spike_f` |
| **Concurrent N‑way fork** | 6 forks alive **simultaneously**, each with its own remapped vsock, full protocol on all | `spike_i` |
| **Multi‑process + DB fidelity** | 3‑process SQLite workload snapshotted **mid‑write** survives fork crash‑consistently (`integrity_check=ok`) | `spike_h` |
| **Full browser fidelity (D2)** | A **live headless Chrome** session (V8 renderer + CDP + JS heap + localStorage) survives snapshot + 3‑way fork and keeps executing JS | `spike_j` |
| Absolute snapshot/restore/fork latency | *Bare‑metal‑blocked* — nested virt cannot give trustworthy wall‑times | `spike_a` |

## The engine (M0)

`kladosd` owns the timeline DAG (SQLite) and live instances; `klad` is the Git‑shaped CLI. A full run:

```console
$ klad run
run  bf1b7f1fcdbf
instance f36451fd0bcd  (booting agent…)

$ klad snapshot f36451fd0bcd --label before-fork
snapshot 2c21d7ac6b2d  (536.9 MB)

$ klad fork 2c21d7ac6b2d -n 4
forked 4 children:  [branch 0 of 4] … [branch 3 of 4]

$ klad log bf1b7f1fcdbf
run bf1b7f1fcdbf
  ● genesis  instance f36451fd0bcd  [RUNNING]
  ◆ 2c21d7ac6b2d  "before-fork"  (536.9 MB)
      ↳ fork beeb058a5b23  [branch 0 of 4]  RUNNING
      ↳ fork e15d3b2bf0f0  [branch 1 of 4]  RUNNING
      ↳ fork 74cc72b02e08  [branch 2 of 4]  RUNNING
      ↳ fork efcc72206b37  [branch 3 of 4]  RUNNING
```

Each fork is a concurrent copy‑on‑write child running the zero‑window correctness protocol. Concurrent fork works because each fork's Firecracker runs in its own **mount namespace** so the snapshot‑baked device paths resolve per‑instance (jailer‑lite).

## How it works

- **Firecracker microVMs** as the isolation and snapshot unit (production‑proven via AWS Lambda SnapStart). Requires `/dev/kvm` → a bare‑metal or nested‑virt Linux host.
- **Browser runs *inside* the guest**, so headless Chrome state (JS heap, cookies, sessions, localStorage) is captured by the memory+disk snapshot *for free* — validated: a live session survives a fork and keeps running (`spike_j`).
- **Content‑addressed snapshots** (FastCDC chunking + BLAKE3 + zstd) for dedup across snapshots, forks, and tenants *(design; not yet built — M1)*.
- **Fork via copy‑on‑write everywhere:** overlayfs upper layers for disk, page‑cache CoW for memory — N children share one base image instead of N full copies.
- **A tiny guest agent** on vsock runs the correctness‑critical post‑fork protocol — reseed entropy, step the clock, inject per‑branch context — with the workload **held stopped until reseed completes**, so forked agents never share randomness or clocks (zero‑window).

See [`techSpec PRD .md`](./techSpec%20PRD%20.md) for the full spec, [`pressure-test-and-derisk.md`](./pressure-test-and-derisk.md) for the risk analysis, and [`robust-fixes.md`](./robust-fixes.md) for the fix designs.

## Repository layout

```
techSpec PRD .md            Product Requirements Document + Technical Specification
pressure-test-and-derisk.md Ranked risk analysis + the spike "kill order" with pass/fail gates
robust-fixes.md             Deep-dive designs for the robust fixes (entropy, disk CoW, clock, latency)
spikes/
  engine/                   kladosd (daemon), klad (CLI), demo_flow.sh   ← the M0 engine
  harness/                  fc.py (Firecracker client) + spike_a … spike_j
  guest/                    klados-guest-agent, sitecustomize RNG hook, browser/fidelity workloads, inits
  setup/                    KVM check, Firecracker install, rootfs prep, browser rootfs build
  uffd/                     UFFD lazy-paging handler (Rust)
  results/                  Captured spike outputs (JSON)
```

## Running it

**Requires a Linux host with KVM** — bare metal, a nested‑virt cloud VM, or Windows 11 WSL2 with nested virtualization. It will not run on stock macOS/Windows.

```bash
cd spikes
bash setup/check-kvm.sh                     # verify /dev/kvm
sudo bash setup/install.sh                  # Firecracker + guest kernel + rootfs
sudo bash setup/prep_entropy_rootfs.sh      # build the entropy/protocol rootfs

# the M0 engine (daemon + CLI)
sudo python3 engine/kladosd.py serve &      # start the engine
sudo bash engine/demo_flow.sh               # run -> snapshot -> fork x4 -> log

# individual spikes (a selection)
python3 harness/spike_b.py --forks 16 --mem-mib 2048   # memory fork economics
sudo bash harness/spike_d_disk.sh 16 100               # disk CoW economics
python3 harness/spike_c.py --forks 8 --mode vanilla    # entropy divergence (the bug)
python3 harness/spike_g_protocol.py --forks 4          # guest-agent fork protocol (the fix)
sudo python3 harness/spike_i_concurrent.py --forks 6   # concurrent N-way fork
# full browser fidelity (builds a Chrome rootfs first; long):
sudo bash setup/build_browser_rootfs.sh
sudo python3 harness/spike_j_browser.py --forks 3
```

See [`spikes/README.md`](./spikes/README.md) for the full runbook and how to read each result against its gate.

## Roadmap (from the PRD)

- **M0 — Engine core:** ✅ working reference impl — `kladosd`, full snapshots, restore, concurrent fork, `klad run/snapshot/fork/log`. *(Production = Rust; content‑addressed dedup storage and overlay‑root disk still to wire.)*
- **M1 — Fork + Timeline:** timeline DAG store (done in M0), fs‑diff, guest fork‑hooks (done), Python SDK alpha, open‑source launch.
- **M2 — Cloud alpha:** control plane, content‑addressed dedup storage, **Timeline UI**, metering.
- **M3 — Durability GA:** auto‑snapshot policies, crash recovery, idle auto‑pause, framework adapters, MCP server.
- **M4 — Scale + determinism:** cross‑host fork, lazy cold‑restore, record/replay network proxy, golden‑snapshot registry.

## Honest limitations

- **Side effects under fork/rewind are fundamental.** A sent email stays sent; forking a mid‑"charge card" agent can charge twice. Klados can record where effects happen and expose idempotency helpers, but no infrastructure can un‑send a real‑world action. Design for it.
- **In‑flight external TCP connections are reset on restore** (SDK auto‑reconnects; retrying clients ship on by default).
- **Bare‑metal / KVM required for self‑hosting; x86_64 snapshots restore only on x86_64.**
- **GPU‑resident state is not captured in v1** (v1 targets CPU sandboxes that call GPU inference over the network — ~95% of agent workloads).
- **Absolute latency is unmeasured** — nested‑virt testing can't produce trustworthy p50/p99; the *algorithm* is validated, the wall‑clock numbers need bare metal.

## Naming

**Klados** (Greek *κλάδος*, "branch"; also Slavic *klad*, "hoard/buried treasure" — a fitting second reading for a durable store of saved states). CLI: `klad` · daemon: `kladosd` · packages: `klados`.

## License

Apache‑2.0 (the engine is intended as open core). See [`LICENSE`](./LICENSE).

---

*Early‑stage project. The spec is a draft for review; the spikes and engine are reference code, authored to falsify the riskiest assumptions and stand up a working M0 before production build‑out.*
