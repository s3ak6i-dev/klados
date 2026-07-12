# Klados — Robust Fixes for the Open Spike Findings

Companion to [pressure-test-and-derisk.md](pressure-test-and-derisk.md). For each open item this states the **root cause**, the **robust fix** (the best mechanism, not the easiest), and what is **provable for free on the WSL2 laptop** vs. genuinely bare-metal-blocked.

Guiding principle: fix causes, not symptoms; require **zero application cooperation** wherever possible; prove every fix by observing behavior, not by assertion.

---

## 1. R2 — RNG divergence across forks (the only true *vulnerability*)

**Root cause.** N children resume from one memory image, so every process's userspace PRNG state is byte-identical. Spike C proved the kernel CSPRNG self-heals (RDRAND per read) but **userspace PRNGs collide totally — including OpenSSL's DRBG, i.e. TLS client randoms.** VM-fork keeps the guest PID constant, so OpenSSL's own PID-based fork detection never fires.

**Why the naive fixes fail.**
- Re-seeding the *kernel* pool (PRD §5.4.1) does nothing for userspace state.
- An LD_PRELOAD shim can wrap exported C symbols (OpenSSL `RAND_bytes`, glibc `random`) but **cannot reach RNG state internal to a runtime** (Python's Mersenne Twister lives inside libpython; Go `math/rand`, Java `SecureRandom` similarly). So no single mechanism covers everything.

**Robust fix = a layered post-fork reseed framework, triggered by an explicit event, requiring no app changes:**

1. **Trigger (deterministic):** the `klados-guest-agent` receives a `FORKED` event from the host and publishes a monotonically increasing *fork generation* to a well-known path (`/run/klados/forkgen`). Production channel is vsock (also carries branch index/context per §5.5). A **belt-and-suspenders fallback** — periodic rekey from the kernel CSPRNG — covers any process that misses the event, bounding exposure to the rekey interval.
2. **Delivery, layer A — runtime hooks (covers the dominant agent surface transparently):** base images ship an auto-loaded hook per runtime that watches `forkgen` and reseeds on change. Python: a `sitecustomize.py` on `PYTHONPATH` (auto-imported by *every* Python process) reseeds `random` and calls `ssl.RAND_add(os.urandom(32))` — which Spike C already proved clears OpenSSL. Node: a `--require` preload. No application code changes.
3. **Delivery, layer B — native code (belt-and-suspenders):** an LD_PRELOAD `libkladosfork.so` interposes exported RNG symbols (`RAND_bytes`, `RAND_priv_bytes`, `random`, `rand`, `arc4random*`) for statically-un-hookable native binaries, reseeding from the kernel CSPRNG on `forkgen` change. Must be compiled per cell (glibc ABI), so it's the secondary layer, not the primary one.

**Provable on this machine (and proven below):** the app (`entropy_gen.py`, unmodified — uses `random` + `ssl.RAND_bytes`) runs under the runtime hook; after the fork event, all three streams go collision-free. That demonstrates a *transparent* fix for the security-critical path (Python + OpenSSL/TLS), which is ~the whole realistic agent surface. Layer B is designed here and left for per-cell compilation.

**Residual (documented, not hidden):** runtimes with internal RNG state and no auto-load hook (Go `math/rand`, some JVM PRNGs). Mitigations: Go's `crypto/rand` uses getrandom (safe); `math/rand` is non-crypto; JVM needs a `-javaagent`. These go on the base-image compatibility checklist, and the periodic-rekey fallback bounds any gap.

---

## 2. Disk CoW — forks need private writable disks, not a shared read-only rootfs

**Root cause.** Spike B held the rootfs read-only to share it safely. Real forks must each write. WSL's ext4 has no reflink, so `cp --reflink` degrades to full copies (slow, not CoW).

**Robust fix (matches PRD §3.3 / §5.1): overlayfs per-fork upper layers.** One read-only base image, shared by all forks; each fork gets a private `overlayfs` upper dir. Zero-copy at fork time; storage cost = only the bytes a fork actually writes. This is exactly the production design and it works on stock WSL ext4 (overlayfs needs no reflink). Provable here: N forks each get an independent writable `/`, sharing the base, with per-fork disk cost ≈ divergence only.

---

## 3. UFFD lazy paging — the stubbed handler

**Root cause.** The Spike B `uffd` mechanism was stubbed (`parse_region` TODO) because the Firecracker→handler handoff protocol is version-specific.

**Robust fix.** Implement the real handoff against the installed Firecracker (v1.16.1): accept the UDS connection, receive the guest-region layout (JSON) + the userfaultfd via SCM_RIGHTS, then serve `UFFDIO_COPY` from a single shared mmap of the base memory file. This is needed for *lazy* cold-restore and *cross-host* fork (§4.2, §5.3), not for same-host fork (which file-CoW already nails). Buildable on this machine; latency numbers from it are still nested-virt-limited.

---

## 4. Clock jump on restore (R8)

**Root cause.** A restored VM's guest clock is stale by the pause duration; pending timers/deadlines may misfire. Firecracker does not auto-inject wall time.

**Robust fix.** The guest agent, on the same `FORKED`/resume event, steps `CLOCK_REALTIME` to host-provided true time and emits a `time-jumped` notification (PRD §6), with a `clock_policy` flag for `step|freeze|replay`. Provable here: pause a VM, restore after a simulated long gap, observe the guest clock before/after the fixup.

---

## 5. Latency (R3) — the one item that is genuinely bare-metal-blocked

**Root cause.** Nested virtualization adds overhead and jitter to every VM-exit, so absolute snapshot/restore/fork wall-times measured under WSL are pessimistic and noisy. No software trick removes this on a nested host.

**What is still provable for free (the *algorithm*, not the clock):** diff snapshots write only dirty pages (measure bytes written, hardware-independent); warm-set prefetch predicts the working set (measure hit rate); fork memory-sharing holds (Spike B, a ratio, hardware-independent). Absolute p50/p99 latency vs. the PRD thresholds must wait for a few hours of bare metal — and only that.

---
*The fixes in §1–4 are demonstrated on the WSL2 laptop in the sections that follow. §5 is characterized, not "fixed," because it cannot be.*
