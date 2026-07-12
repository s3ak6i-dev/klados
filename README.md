# Klados

> **The durable, forkable runtime for AI agents.** Snapshot any running agent — filesystem, processes, memory, browser, conversation state — in under a second. Resume it tomorrow. Fork it into ten parallel timelines. Rewind it to any prior step.

**Git for running agents.** Git made source code durable, forkable, and time‑travelable, and an industry grew on those three verbs. Klados does the same for *live execution state*.

![status](https://img.shields.io/badge/status-pre--implementation-orange) ![primitives](https://img.shields.io/badge/core%20primitives-validated-brightgreen) ![license](https://img.shields.io/badge/license-Apache--2.0-blue) ![platform](https://img.shields.io/badge/platform-Linux%2FKVM-lightgrey)

---

## The core idea

The primitive is the **snapshot**: a complete, consistent, content‑addressed capture of an agent's execution state. From a snapshot you can do three things:

1. **Resume** — continue execution exactly where it stopped, on any machine, at any time.
2. **Fork** — spawn N copy‑on‑write children that diverge from the same instant.
3. **Rewind** — restore any earlier snapshot in a run's timeline and re‑execute from there.

These solve four recurring failures in agent infrastructure: **crash amnesia** (a 35‑minute run dies and everything is lost), **serial exploration** (tree‑shaped agentic search can't cheaply duplicate a mid‑task starting point), **unreproducible debugging** (you have a transcript at step 47 but not the state), and **idle burn** (paying for warm VMs while agents wait on approvals or CI).

## Status

**Pre‑implementation.** This repository currently holds the product/technical spec and a suite of **de‑risking spikes** that validate the hardest technical claims on real Firecracker microVMs. The spikes were run on a laptop via WSL2 nested KVM — so *correctness and economics* results are trustworthy, while *absolute latency* awaits bare metal (nested virt distorts wall‑times; see below).

### What has been validated

| Claim | Result | Spike |
|---|---|---|
| **Copy‑on‑write memory fork economics** | 16 forks of a 2 GB base share memory at a **0.10** ratio (154 MiB vs 1,472 naive); ratio *improves* with fork count | `spike_b` |
| **Copy‑on‑write disk economics** | 16 forks, **fork‑time disk cost = 0** (empty overlay uppers), per‑fork cost = divergence only, **0.147** ratio | `spike_d_disk` |
| **Entropy divergence is real** | Forked children emit **identical** userspace RNG *and* OpenSSL/TLS client randoms (65/65 collisions) — a security hazard | `spike_c` |
| **Entropy fix — zero‑window, transparent** | Same **unmodified** app → **0 collisions** via the guest‑agent fork protocol (reseed before the workload resumes) | `spike_g` |
| **Clock fixup on restore** | Restored guest clock was ~9.7 s stale after an 8 s pause → **<1 ms skew** after the fork protocol steps it | `spike_e`, `spike_g` |
| **Branch‑context injection** | Each fork receives its distinct per‑branch prompt over vsock | `spike_g` |
| **Incremental snapshot algorithm** | Diff snapshot writes **6.1%** of a full snapshot's bytes (hardware‑independent) | `spike_f` |
| Absolute snapshot/restore/fork latency | *Bare‑metal‑blocked* — nested virt cannot give trustworthy wall‑times | `spike_a` |

## How it works

- **Firecracker microVMs** as the isolation and snapshot unit (production‑proven via AWS Lambda SnapStart). Requires `/dev/kvm` → a bare‑metal or nested‑virt Linux host.
- **Browser runs *inside* the guest**, so headless Chromium state (cookies, sessions, DOM) is captured by the memory+disk snapshot *for free*, instead of being serialized at the application layer.
- **Content‑addressed snapshots** (FastCDC chunking + BLAKE3 + zstd) give dedup across snapshots, forks, and tenants.
- **Fork via copy‑on‑write everywhere:** overlayfs upper layers for disk, page‑cache CoW / userfaultfd for memory — N children share one base image instead of N full copies.
- **A tiny guest agent** on vsock handles the correctness‑critical post‑fork protocol: re‑seed entropy, step the clock, regenerate network identity, and inject per‑branch context — *before* the workload resumes, so forked agents never share randomness or clocks.

See [`techSpec PRD .md`](./techSpec%20PRD%20.md) for the full product requirements and technical specification.

## Repository layout

```
techSpec PRD .md            Product Requirements Document + Technical Specification
pressure-test-and-derisk.md Ranked risk analysis + the spike "kill order" with pass/fail gates
robust-fixes.md             Deep-dive designs for the robust fixes (entropy, disk CoW, clock, latency)
spikes/                     De-risking harness (runs on a KVM Linux host)
  README.md                 How to run the spikes + how to read results
  setup/                    KVM check, Firecracker install, rootfs prep
  harness/                  fc.py (Firecracker client) + spike_a … spike_g
  guest/                    klados-guest-agent, sitecustomize RNG hook, init
  uffd/                     UFFD lazy-paging handler (Rust)
  results/                  Captured spike outputs (JSON)
```

## Running the spikes

**Requires a Linux host with KVM** — bare metal, a nested‑virt cloud VM, or Windows 11 WSL2 with nested virtualization. It will not run on stock macOS/Windows.

```bash
cd spikes
bash setup/check-kvm.sh              # verify /dev/kvm
sudo bash setup/install.sh           # install Firecracker + guest kernel + rootfs
sudo bash setup/prep_entropy_rootfs.sh   # build the entropy/protocol rootfs

# core results
python3 harness/spike_b.py --forks 16 --mem-mib 2048   # memory fork economics
sudo bash harness/spike_d_disk.sh 16 100               # disk CoW economics
python3 harness/spike_c.py --forks 8 --mode vanilla    # entropy divergence (the bug)
python3 harness/spike_g_protocol.py --forks 4          # guest-agent fork protocol (the fix)
```

See [`spikes/README.md`](./spikes/README.md) for the full runbook and how to interpret each result against its pass/fail gate.

## Roadmap (abridged, from the PRD)

- **M0 — Engine core:** `kladosd`, full+diff snapshots, local restore, overlayfs disk CoW, CLI (`klad run/snapshot/restore/log`).
- **M1 — Fork + Timeline:** same‑host N‑way fork, timeline DAG store, guest fork‑hooks, Python SDK alpha, open‑source launch.
- **M2 — Cloud alpha:** control plane, content‑addressed dedup storage, **Timeline UI**, metering.
- **M3 — Durability GA:** auto‑snapshot policies, crash recovery, idle auto‑pause, framework adapters, MCP server.
- **M4 — Scale + determinism:** cross‑host fork, lazy cold‑restore, record/replay network proxy, golden‑snapshot registry.

## Honest limitations

- **Side effects under fork/rewind are fundamental.** A sent email stays sent; forking a mid‑"charge card" agent can charge twice. Klados records where effects happen and the SDK exposes idempotency helpers, but no infrastructure can un‑send a real‑world action. Design for it.
- **In‑flight external TCP connections are reset on restore** (SDK auto‑reconnects; retrying clients ship on by default).
- **Bare‑metal / KVM required for self‑hosting; x86_64 snapshots restore only on x86_64.**
- **GPU‑resident state is not captured in v1** (v1 targets CPU sandboxes that call GPU inference over the network — ~95% of agent workloads).

## Naming

**Klados** (Greek *κλάδος*, "branch"; also Slavic *klad*, "hoard/buried treasure" — a fitting second reading for a durable store of saved states). CLI: `klad` · daemon: `kladosd` · packages: `klados`.

## License

Apache‑2.0 (the engine is intended as open core). See [`LICENSE`](./LICENSE).

---

*This is an early‑stage project. The spec is a draft for review and the spikes are research code, authored to falsify the riskiest assumptions before production build‑out.*
