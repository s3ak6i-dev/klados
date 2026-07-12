# Klados — Pressure Test & De-risk Plan

**Companion to:** techSpec PRD .md (v0.9)
**Purpose:** Falsify the riskiest assumptions before writing production code. Ordered so the cheapest experiment that can kill the thesis runs first.

---

## How to read this

Each risk has: the **claim** (what the PRD asserts, with §refs), the **attack** (the specific reason it might be false), and a **falsifiable test** (what result would prove it wrong). Risks are grouped by blast radius:

- **Tier 0 — Thesis-killers.** If wrong, there is no company. Test these first, before anything else.
- **Tier 1 — Margin/scale killers.** The thing works on a bench but doesn't close economically or at fleet scale.
- **Tier 2 — Expensive-but-survivable.** Real grind; won't kill you, will eat your quarter if underestimated.

The de-risk plan (Part B) maps each Tier-0/1 risk to a spike with a go/no-go gate.

---

## Part A — The Pressure Test

### Tier 0 — Thesis-killers

**R1 — Copy-on-write memory fork actually delivers the claimed economics.**
*Claim (§5.2, §UC2, pricing "forks are free"):* 16 children of a 2 GB parent consume ≈ 2 GB + divergent pages, not 32 GB, with fork latency dominated by VM setup (~100–150 ms each), because all children are UFFD-backed against one immutable memory image in host page cache.
*Attack:* This is the single load-bearing mechanism for the crown-jewel use case *and* the pricing headline. Firecracker restores each VM with its own memory backing; sharing one immutable base across N VMs with per-VM copy-on-fault is not a checkbox — it requires either a UFFD server that serves shared read-faults and vends private pages on write, or backing guest RAM with a `MAP_PRIVATE` mmap of the shared base file and letting the kernel's own page-cache CoW do the divergence. If neither cleanly works with Firecracker's memory model, forks cost ~N×base RAM, the demo becomes "watch one agent become one," and "forks are free" becomes "forks cost a full VM each."
*Falsifiable test:* Fork 16 children from a live 2 GB base; measure wall-time to all-ready **and** actual host memory committed. Fails if total RSS approaches 16×base, or if fork wall-time exceeds ~2 s.

**R2 — Post-fork divergence is reliably correctable (no duplicate randomness).**
*Claim (§5.4.1, §6):* Re-seeding kernel entropy via `RNDADDENTROPY` + getrandom-backed CSPRNGs prevents N children from generating identical UUIDs, TLS client randoms, and session tokens. The PRD itself calls this "the most dangerous correctness bug class in the whole system."
*Attack:* Re-seeding the *kernel* pool does not reach userspace PRNG state already cached in memory at the snapshot instant. A Python process that seeded `random`/`ssl` at import, a Node process, an OpenSSL `RAND` context, a JVM `SecureRandom` — each holds in-memory state that is byte-identical across all forks and will emit identical streams unless that specific runtime is forced to re-seed. The failure is *silent*: two forks mint the same UUID or the same TLS client-random and nothing crashes — you just get corrupted, colliding results. A product that silently corrupts is worse than one that doesn't exist.
*Falsifiable test:* Fork 64 children; each generates 10k UUIDs / random ints / TLS handshakes in Python, Node, and raw OpenSSL. Fails on any cross-child collision after the §5.4 protocol runs.

**R3 — The snapshot/restore hot loop is genuinely sub-second.**
*Claim (§FR1.2, §FR2.1):* <250 ms workload pause for an incremental snapshot of a 2 GB VM; warm resume p50 <500 ms.
*Attack:* The pause window (steps 1–4 of §3.1) is dominated by writing dirty memory pages to the memory file. A snapshot is only cheap if the *dirty set since the parent* is small — but an active agent running Chromium + a Python runtime can dirty hundreds of MB between snapshots. Writing 500 MB even to page cache is ~100–200 ms of the budget before any flush-hook or device state. If the flight-recorder cadence (auto-snapshot every K tool-calls) lands on a heavy-churn moment, the "invisible" pause becomes a visible stall, and the demo's "one node per tool call" loop drags.
*Falsifiable test:* On target hardware, snapshot a 2 GB VM under realistic churn (browser + interpreter active); measure pause time for full and diff snapshots across randomized instants. Fails if diff-snapshot p50 pause >250 ms or warm restore p50 >500 ms.

### Tier 1 — Margin / scale killers

**R4 — Storage dedup actually hits ≥8× on real workloads.**
*Claim (§7, pricing §8):* ≥8× (6-mo) / ≥15× (12-mo) dedup across tenant snapshots; storage priced $0.05/GB-mo *post-dedup* with healthy margin.
*Attack:* Dedup is high across *base image layers* (shared, public) but the interesting bytes are per-tenant divergent memory + disk, which are encrypted per-tenant (§10 explicitly makes chunk dedup *per-tenant* for private layers — a stated margin trade-off). If real agent workloads churn memory heavily, effective dedup on the paid bytes could be 2–3×, not 8×, and the hot-NVMe cache needed for fast resume is a second, uncounted cost. Margin evaporates precisely as usage grows.
*Falsifiable test:* Build the dedup/economics model from real divergence data captured in Spikes B and D. Fails if realistic dedup <8× or modeled gross margin at target pricing is negative at the p50 workload.

**R5 — The bare-metal / `/dev/kvm` constraint doesn't strangle cloud economics.**
*Claim (§D1, §M2):* Bare-metal fleet (Latitude/OVH/Hetzner) is an accepted trade-off for Firecracker.
*Attack:* Bare metal doesn't autoscale in seconds — you pre-provision and pay for idle headroom, which is *ironic for a product whose pitch is eliminating idle burn*. It also caps geographic reach and complicates enterprise self-host (customers need KVM-capable metal too). This is a structural GTM tax, not a bug, but it must be quantified before pricing is set.
*Falsifiable test:* Model fleet utilization vs. demand burstiness; check whether target compute margins survive the idle-headroom overhead on bare metal. Fails if required headroom pushes effective compute cost above the $0.09/vCPU-hr + $0.35/GB-RAM-hr pricing.

**R6 — Cold-restore lazy-paging has a genuinely "brief" tail.**
*Claim (§4.2, §UC5):* VM executing in low single-digit seconds via UFFD demand-fetch, with dirty-page-bitmap warm-set prefetch predicting the working set; 1,000+ restores/hr from one snapshot.
*Attack:* If warm-set prediction is poor, the VM thrashes on demand-faults served from object storage at tens-of-ms per chunk, and the "brief tail" becomes a sustained stall. The high-fan-out eval use case (UC5) is the one most sensitive to this, and it's a persona (U3) the PRD says pays *today*.
*Falsifiable test:* Cold-restore a 2 GB-working-set VM from an S3-compatible store with prefetch on; measure time-to-first-useful-work and steady-state fault latency. Fails if p50 time-to-productive >5 s.

### Tier 2 — Expensive but survivable

**R7 — Real-workload restore fidelity, especially browser-in-guest.**
*Claim (§D2, §12):* Chromium inside the guest snapshots "for free"; top-50 packages + Postgres/Redis/Chromium survive snapshot→restore→16-fork.
*Attack:* Multi-process Chromium (GPU process, dozens of threads, timers, live WebSockets) is the hardest thing in the guest to resurrect coherently. "Captured for free" is true for the *bytes*; *coherence* on resume (session still authenticated, no internal-state corruption, no collision across forks) is unproven at their bar. This is the differentiator, so it must actually work — but it's a grind, not a thesis-killer.
*Falsifiable test:* Part of Spike D — authenticated browser session must survive restore and diverge correctly across forks.

**R8 — Clock-jump tolerance across runtimes.**
*Claim (§6):* `clock_policy: step|freeze|replay` handles the guest clock jumping forward (possibly hours/days) on restore.
*Attack:* Large forward jumps break pending timers, monotonic-deadline logic, TCP keepalives, and cert-validity windows across asyncio/Node/JVM/Go. The flag names the problem; it doesn't prove the runtimes behave.
*Falsifiable test:* Part of Spike D — pause a VM for a simulated 24 h, restore, assert timers/supervisors behave per policy.

**R9 — The CRIU fallback path is a maintenance trap.**
*Claim (§D4):* CRIU/containers as a secondary path for KVM-restricted environments with "degraded guarantees."
*Attack:* CRIU is brittle across kernel/device versions; maintaining a second restore path doubles the correctness-test surface (already the "unglamorous 40%") for a minority of environments. High risk of becoming a support sink.
*Recommendation:* Cut from v1 or ring-fence hard behind an explicit "unsupported/experimental" flag. Not a spike — a scope decision to make now.

**R10 — Feature-vs-company: incumbent fast-follow, unproven graph moat.**
*Claim (§10, §12):* The moat is the *graph* (timelines, dedup economics, determinism, framework-native DX) + OSS vocabulary capture, not fork alone.
*Attack:* E2B already has pause. If sandbox vendors ship "fork," the bet is that timeline/dedup/determinism are a moat rather than a checkbox. Unvalidated until design partners say the *graph* is why they'd pay.
*Falsifiable test:* Design-partner calls (Track 2) — do U2/U3 cite the timeline/graph, or just fork?

**R11 — Demand timing: are agent runs long enough yet.**
*Claim (§10):* Hedged by U2/U3 (fork/evals pay even for short runs).
*Attack:* If long-running production agents are still rare, the durability story (U1/U4) is early, and the whole near-term case rests on the RL/evals wedge being real *now*.
*Falsifiable test:* Track 2 — ≥2 U2/U3 partners commit to using it this quarter.

---

## Part B — The De-risk Plan (the kill order)

Five engineering spikes on one bare-metal box, plus one parallel non-engineering track. Sequenced so a hard failure is discovered as early and as cheaply as possible. **Do not build the control plane, Timeline UI, storage/dedup pipeline, SDK adapters, MCP server, networking, or the CRIU path until the gates below pass** — every one of those assumes the primitives work.

| Spike | Tests | Effort | Pass threshold |
|-------|-------|--------|----------------|
| **A — Firecracker baseline latency** | R3 | 1–2 d | Diff-snapshot pause p50 <250 ms; warm restore p50 <500 ms on target hardware |
| **B — CoW memory fork** ⭐ | R1 | 3–4 d | 16-way fork <2 s **and** host memory ≈ base + divergent (not 16×base) |
| **C — Entropy/identity divergence** | R2 | 3–4 d | Zero cross-child collisions across 64 forks × (Python, Node, OpenSSL) after §5.4 protocol |
| **D — Real-workload restore fidelity** | R7, R8, D2 | 4–5 d | Authed Chromium session + asyncio server + Postgres survive restore after 24 h simulated pause; correct divergence across forks |
| **E — Cold restore / lazy paging** | R6 | 2–3 d | 2 GB working-set VM productive <5 s cold from S3-compatible store |
| **Track 2 — Economics + design partners** | R4, R5, R10, R11 | parallel | Realistic dedup ≥8×; margin positive at target pricing; ≥2 U2/U3 partners commit this quarter |

### Decision gates

- **Gate 1 (after Spike B) — GO/NO-GO on the entire thesis.** If shared-base CoW memory forking doesn't hold its economics, the crown-jewel use case and the "forks are free" pricing both collapse. Everything downstream is wasted until this is green. *This is the most important hour in the plan.*
- **Gate 2 (after Spike C) — size the correctness grind.** Spike C is expected to *fail on the first pass*; its real output is the list of runtimes needing per-runtime re-seed mitigation. A short list means the "unglamorous 40%" is ~40%. A long list means it's closer to 70% and the M-milestone timeline in §9 needs to stretch.
- **Gate 3 (after Spike D) — green-light the M0 demo build.** Once primitives + browser fidelity are proven, the "five-minute wow" (§6.3) is a build task, not a research bet, and you can start the control plane / SDK work with confidence.

### Sequencing logic (why this order)

1. **A before B** because if the basic snapshot/restore loop isn't sub-second, the fork numbers are moot — and A is a one-day sanity check that de-risks the latency claims underpinning B, D, and E.
2. **B is the fulcrum.** It validates the crown jewel *and* the pricing headline in one experiment. It goes second because it's the highest-information, thesis-defining test — no reason to defer the go/no-go.
3. **C before D** because divergence correctness is a *thesis* risk (silent corruption), while D's fidelity work is a *grind* risk (known-hard, survivable). Learn the scarier thing first.
4. **E last** among engineering spikes: cold-restore tail matters for scale (U3/UC5) but not for the M0/M1 single-box demo, so it can trail the primitives.
5. **Track 2 runs in parallel from day one** — design-partner calls have long lead times and shouldn't block on the bench work; the economics model *consumes* real numbers from B and D as they land.

### What a failure buys you (each spike is worth running even if it fails)

- **B fails** → you learn the thesis is wrong for ~$0 of engineering instead of after building a control plane. Pivot or rework the memory model.
- **C fails (expected)** → you get the exact scope of the correctness grind, which is the single biggest unknown in the schedule and budget.
- **D partial** → you learn which workloads are v1-supportable and which go on the honest "not yet" list — feeds the Tier-D documentation the PRD already commits to.
- **Track 2 flat** → you learn the wedge is mispositioned *before* building for the wrong persona.

---
*End of pressure test & de-risk plan. Gate 1 is the decision that matters — everything else is sequencing around it.*
