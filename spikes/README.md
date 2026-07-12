# Klados Spikes — A & B

De-risk harness for the two thesis-defining spikes in [../pressure-test-and-derisk.md](../pressure-test-and-derisk.md):

- **Spike A — Firecracker baseline latency** (tests R3). Pass: diff-snapshot pause p50 <250 ms; warm restore p50 <500 ms.
- **Spike B — CoW memory fork** (tests R1, the Gate-1 fulcrum). Pass: 16-way fork <2 s **and** host memory ≈ base + divergent, not 16×base.

> **Provenance:** authored on a Windows workstation with no `/dev/kvm`, so **nothing here has been run on hardware yet.** Treat the first successful run on a KVM host as the real acceptance test. Every host-specific value is marked `TODO(host)`.

---

## 0. Prerequisites (the hard gate)

Firecracker needs a Linux host with **KVM** — bare metal, or a nested-virt-capable cloud VM. It will **not** run on Windows/macOS or in stock WSL2. The PRD names Latitude / OVH / Hetzner bare metal for exactly this reason (PRD §D1, §M2).

Minimum: Linux 5.10+ x86_64, `/dev/kvm` present and writable, ~8 GB RAM free (Spike B forks a 2 GB base ×16 → size the box for divergence + page cache), Python 3.10+, Rust (only for the optional UFFD mechanism in Spike B).

```bash
bash setup/check-kvm.sh        # verify virtualization + /dev/kvm
sudo bash setup/provision.sh   # install firecracker, fetch a kernel + rootfs
```

## 1. Run Spike A

```bash
python3 harness/spike_a.py --mem-mib 2048 --iterations 20 --churn-mib 512
```

Boots a 2 GB microVM, then over N iterations: pauses → full/diff snapshot → resume (measuring the **pause window**), and warm-restores a fresh VM from the snapshot (measuring **warm restore**). `--churn-mib` sizes the dirty set per iteration so the diff-snapshot number reflects a realistic working agent, not an idle VM — this is the number R3 actually hinges on. Results print as a table + `results/spike_a.json`.

## 2. Run Spike B

```bash
# Primary mechanism: file-backed CoW (no custom handler; kernel page-cache does the sharing)
python3 harness/spike_b.py --forks 16 --mem-mib 2048 --mechanism file-cow

# Lazy/cross-host variant: UFFD shared-base handler (build it first)
cargo build --release --manifest-path uffd/Cargo.toml
python3 harness/spike_b.py --forks 16 --mem-mib 2048 --mechanism uffd
```

Snapshots the base once, then launches N children from that one snapshot in parallel. It reports fork wall-time **and** the memory economics measured by **PSS** (proportional set size, from `/proc/<pid>/smaps_rollup`) — PSS is the honest metric because it charges each shared base page once across all children instead of counting it N times. See §4.

## 3. Reading the results

`harness/metrics.py` computes p50/p99 and the memory summary. The two numbers that decide the gates:

| Spike | Metric | Source | Pass |
|-------|--------|--------|------|
| A | diff-snapshot pause p50 | `pause_ms` distribution | <250 ms |
| A | warm restore p50 | `restore_ms` distribution | <500 ms |
| B | fork wall-time (all children ready) | `fork_wall_ms` | <2 s for 16 |
| B | memory savings ratio | `Σ PSS / (N × base_pss)` | ≪ 1.0 (ideally ~ base+divergent) |

## 4. Why PSS, and the mechanism subtlety (read before trusting Spike B)

The whole "forks are free" economics (PRD §5.2) rests on N children *sharing* the immutable base and only paying for divergence. There are two ways to get that, and they are **not** equivalent — Spike B measures both so you know which one Firecracker actually gives you:

- **`file-cow`:** every child's Firecracker process `mmap`s the *same* base memory file `MAP_PRIVATE`. The kernel keeps those file pages in page cache **once**; children read-share them and only copy a page on **write** (kernel CoW). Savings come from every page that stays read-only. This is the strong result and needs no custom code.
- **`uffd`:** a userfaultfd handler serves faults from one shared mmap of the base, `UFFDIO_COPY`-ing pages into each child on first *touch*. This materializes a private copy on read **or** write — so savings come only from **untouched** pages, and the working set is duplicated per child. Weaker on same-host, but it's the mechanism that enables **lazy** and **cross-host** fork (PRD §5.3, §4.2).

If `file-cow` PSS shows big sharing but `uffd` doesn't, that's expected and important: it tells you same-host fork should use file-CoW and UFFD is reserved for the lazy/cross-host path. If *neither* shares, R1 has failed and **Gate 1 is a NO-GO** — stop and rethink the memory model before building anything else.

## Layout

```
spikes/
  setup/check-kvm.sh · provision.sh
  harness/fc.py            # Firecracker HTTP-over-UDS client
         spike_a.py        # baseline latency
         spike_b.py        # CoW fork
         metrics.py        # stats + memory economics
         workload/churn.py # in-guest dirty-set generator (bake into rootfs)
  uffd/                    # optional Rust UFFD handler for the uffd mechanism
  results/                # JSON output (gitignored)
```
