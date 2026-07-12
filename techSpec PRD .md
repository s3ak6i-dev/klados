# Klados — Product Requirements Document & Technical Specification

**Version:** 0.9 (Draft for review)
**Status:** Pre-implementation
**Codename:** Klados (Greek: κλάδος, "branch"; also Slavic *klad*, "hoard/buried treasure" — a fitting second reading for a durable store of saved states)
**Naming convention:** `klados` = product & packages (PyPI/npm) · `klad` = CLI binary · `kladosd` = host daemon (verified free on PyPI and npm; `klad` is not an existing shell command)
**One-liner:** The durable, forkable runtime for AI agents. Snapshot any running agent — filesystem, processes, memory, browser, conversation state — in under a second. Resume it tomorrow. Fork it into ten parallel timelines. Rewind it to any prior step.

---

## Part I — Product Requirements Document

### 1. Vision

Agents are becoming long-running programs. A coding agent works a repo for forty minutes; a research agent browses for two hours; an ops agent runs indefinitely. Yet the infrastructure underneath them still treats every run as ephemeral: if the process dies, the sandbox is garbage-collected, the browser session evaporates, and the only artifact left is a chat log that cannot be re-executed.

Klados makes agent execution **durable, addressable, and branchable**. The core primitive is the **snapshot**: a complete, consistent, content-addressed capture of an agent's execution state. From a snapshot you can do three things:

1. **Resume** — continue execution exactly where it stopped, on any machine, at any time.
2. **Fork** — spawn N copy-on-write children that diverge from the same instant.
3. **Rewind** — restore any earlier snapshot in a run's timeline and re-execute from there.

The analogy we will use everywhere: **Git for running agents.** Git made source code durable, forkable, and time-travelable, and an entire industry grew on top of those three verbs. Klados does the same for live execution state.

### 2. Problem statement

Teams building agents today experience four concrete, recurring failures:

**P1 — Crash amnesia.** An agent 35 minutes into a task hits an OOM, a provider timeout, a spot-instance preemption, or a bug. Everything is lost. The only recovery is replaying the conversation from message zero against a fresh sandbox, which is slow, expensive (full re-inference of the context), and frequently non-deterministic — the replayed run diverges from the original because the world (or the model's sampling) changed.

**P2 — Serial exploration.** Agentic search is naturally tree-shaped: "try fix A; if it fails, try fix B from the same starting point." Today the starting point cannot be preserved, so teams either run explorations serially (slow), or spin up N fresh sandboxes and replay the prefix N times (expensive, divergent), or simply don't explore (worse results). Best-of-N, tree search, MCTS-style agent scaffolds, and parallel test-time compute are all bottlenecked on cheap state duplication.

**P3 — Unreproducible debugging.** When an agent misbehaves at step 47, the developer has a transcript but not the state. They cannot inspect the filesystem as it was, re-run step 47 with a different prompt, or bisect the run to find where it went off the rails. Debugging agents today is debugging from printf logs in 1995.

**P4 — Idle burn.** Long-running agents spend most of their wall-clock time waiting — for a human approval, a CI run, a scheduled trigger, or an LLM response. Keeping a warm VM alive during those waits is pure waste; tearing it down loses state. Teams pay for idle compute because pausing is not an option.

The consequence: agent infrastructure spend is dominated by redundant re-execution and idle capacity, agent quality is capped by serial exploration, and agent reliability is capped by crash amnesia. Every serious agent team has built (or is planning to build) a fragile in-house approximation of snapshotting — usually "tar the workspace + save the message list" — that fails to capture process state, browser state, or in-flight work.

### 3. Why now, and why the incumbents won't build it

- **Workload shift.** Agent runtimes crossed from seconds to hours of execution during 2025. Durability only matters when runs are long enough to be worth saving; that threshold has just been crossed at scale.
- **The primitives finally exist.** Firecracker's snapshot/restore is production-grade (it underpins AWS Lambda SnapStart). CRIU is mature for the container path. `overlayfs`, reflink-capable filesystems (XFS/Btrfs), and userfaultfd-based lazy restore make copy-on-write forking of both disk and memory practical with sub-second latencies.
- **Structural gap.** Model providers optimize the inference layer; hyperscalers sell generic compute; sandbox vendors (E2B, Modal, Daytona, Morph) optimize *cold-start* and *isolation*, treating the sandbox as disposable. Durability-as-a-primitive — with fork semantics, timelines, and an addressable snapshot graph — is nobody's roadmap centerpiece. (Morph Labs' Infinibranch is the closest prior art and validates the direction; the whitespace is the full timeline/graph model, agent-framework-native integration, and self-hostable open core. See §12, Competitive landscape.)
- **RL and evals tailwind.** Agentic RL training and deterministic evaluation both need exactly this primitive: resettable, forkable environments. The same engine serves production agents and training infrastructure.

### 4. Target users and personas

**U1 — Agent product engineer ("Priya").** Builds a coding/support/research agent product on top of Claude/GPT/open models. Uses E2B or a homegrown Docker sandbox today. Pain: P1 and P4. Wants: `agent.pause()` / `agent.resume()` that actually works, and crash recovery that doesn't replay from zero. Buys via SDK quality and a pricing page.

**U2 — Agent-scaffold researcher ("Marcus").** Works on test-time compute, tree search over agent trajectories, best-of-N sampling with real environments. Pain: P2. Wants: `snapshot.fork(n=16)` in <1s with copy-on-write economics. Cares about determinism guarantees and fork latency above all.

**U3 — RL/evals infra engineer ("Chen").** Builds training environments or benchmark harnesses. Pain: P2 + reproducibility. Wants: golden snapshots as versioned, content-addressed artifacts ("env v3.2 = snapshot sha256:ab12…") that can be restored thousands of times per hour with identical initial state.

**U4 — Platform/DevOps engineer at an agent-heavy company ("Sofia").** Owns the fleet the agents run on. Pain: P4 and cost. Wants: automatic pause-on-idle, live migration off spot instances, storage lifecycle policies, and an audit trail of what every agent did. Buys via SOC 2, VPC deployment, and a Terraform provider.

**Primary wedge persona for v1: U1 + U2** — they feel the pain daily, adopt bottom-up, and produce the public demos that drive the flywheel.

### 5. Core use cases (ranked)

| # | Use case | Persona | Verb | Success metric |
|---|----------|---------|------|----------------|
| UC1 | Crash/preemption recovery: agent resumes from last auto-snapshot after any failure | U1, U4 | resume | <5s from failure detection to running agent; zero lost tool-calls beyond last checkpoint |
| UC2 | Parallel exploration: fork a mid-task agent into N branches trying different strategies; keep the winner | U2, U1 | fork | 16-way fork in <2s total; per-fork marginal storage <5% of base |
| UC3 | Pause/resume across human-in-the-loop waits: agent hibernates during approval, resumes on webhook | U1, U4 | pause/resume | $0 compute while paused; resume <1s warm, <5s cold |
| UC4 | Time-travel debugging: rewind a failed run to step k, inspect filesystem/processes, re-execute with modifications | U1 | rewind | Any historical step restorable in <5s; diff view between any two snapshots |
| UC5 | Golden environments: versioned base snapshots restored at high fan-out for evals/RL | U3 | restore | 1,000+ restores/hour from one snapshot; bit-identical initial state |
| UC6 | Live migration: move a running agent between hosts (spot→on-demand, region→region) | U4 | migrate | <10s blackout window |
| UC7 | Agent state archive & audit: every run's full timeline retained per policy, inspectable later | U4 | inspect | Timeline query API; storage cost <$0.02/GB-month effective after dedup |

### 6. Product definition

Klados ships as three layers:

**L1 — Engine (open source).** A daemon (`kladosd`) that runs on a Linux host and manages agent workloads inside Firecracker microVMs (primary isolation) or runc/gVisor containers (compatibility path). Exposes snapshot/restore/fork/rewind over a local gRPC + REST API. Apache-2.0. This is the credibility and adoption layer.

**L2 — Control plane (cloud / self-hosted enterprise).** Multi-tenant orchestration: fleet scheduling, the snapshot graph database, content-addressed blob storage with dedup, timeline UI, auth/audit, usage metering. The monetization layer.

**L3 — SDKs & integrations.** Python and TypeScript SDKs; first-class adapters for the dominant agent frameworks (Claude Agent SDK, OpenAI Agents SDK, LangGraph, CrewAI, Vercel AI SDK); an MCP server so any MCP-capable agent can self-snapshot; a CLI (`klad run`, `klad fork`, `klad timeline`).

**The primary UX object is the Timeline** — a DAG of snapshots per run, rendered in the dashboard like a Git commit graph. Every node is restorable and forkable with one click. This is the demo, the debugger, and the mental model in one artifact.

#### 6.1 Functional requirements

**FR1 — Snapshot.** Capture a consistent point-in-time image of a running workload: guest memory, vCPU state, device state, disk (as a CoW layer against its parent), and Klados metadata (run ID, step index, parent snapshot, user labels, conversation-state blob if provided by the SDK). Snapshots are content-addressed (BLAKE3 of the manifest) and immutable.
- FR1.1: Triggerable via API, CLI, SDK hook (e.g., after every tool call), cron-style policy, or pre-termination signal (spot interruption handler).
- FR1.2: Snapshot of a 2 GB-RAM microVM completes with <250 ms workload pause (target: diff snapshots after first full snapshot).
- FR1.3: Incremental by default — only dirty memory pages and new disk blocks since the parent snapshot are stored.

**FR2 — Resume.** Restore any snapshot into a running workload on any compatible host.
- FR2.1: Warm resume (snapshot in host page cache / local NVMe): p50 <500 ms, p99 <1.5 s.
- FR2.2: Cold resume (pull from object storage): p50 <5 s for 2 GB working set via lazy page loading (userfaultfd), i.e., the VM starts before all memory has arrived.
- FR2.3: Network identity handling: resumed workload receives fresh outbound identity by default; optional sticky egress IP per run (see FR7).

**FR3 — Fork.** Create N children from one snapshot with copy-on-write semantics for both disk and memory.
- FR3.1: 16-way fork p50 <2 s total on one host; forks may be placed across hosts (each host pulls the base lazily).
- FR3.2: Per-fork marginal storage at creation ≈ metadata only; divergence accrues per-fork deltas.
- FR3.3: Fork-safety hooks: guest agent regenerates machine-id, host keys, DHCP lease, and (critically) re-seeds guest entropy and notifies the workload via a `KLADOS_FORKED` event so SDKs can rotate any session tokens and re-establish provider connections.

**FR4 — Timeline & rewind.** Every run maintains an append-only DAG of snapshots. Rewind = restore(ancestor) + optionally mark the abandoned suffix. API supports: list timeline, diff two snapshots (filesystem diff, process-table diff, metadata diff), tag/label nodes, GC policies (keep-last-N, keep-tagged, TTL).

**FR5 — State completeness tiers.** Not all state is equally capturable; the product is explicit about tiers rather than pretending:
- Tier A (always captured): memory, CPU, disk, process tree, open files, in-guest network stack.
- Tier B (captured via in-guest cooperation): headless browser sessions (Chromium runs *inside* the guest, so it snapshots for free — this is a key design decision), language-runtime state, background daemons.
- Tier C (captured via SDK contract): conversation/message history, pending tool-call bookkeeping, provider stream cursors. The SDK writes these to a well-known path (`/klados/state/`) so they ride along in Tier A.
- Tier D (explicitly NOT captured, documented): in-flight external TCP connections (they are reset on resume; SDK auto-reconnects), external-world side effects (an email already sent stays sent), GPU device state in v1 (see Non-goals).

**FR6 — Determinism controls (for U2/U3).** Optional per-run flags: pinned CPU template (mask ISA feature drift), frozen guest clock on restore (or clock-skip injection), fixed entropy seed mode, and record/replay of guest-initiated network I/O via a recording proxy (v1.5 feature; see Tech Spec §9).

**FR7 — Networking.** Per-run virtual NIC with NAT egress; optional egress allow-lists (domain/CIDR) enforced at the host; optional reserved static egress IPs (paid add-on — many real-world agent tasks need IP continuity for sessions); inbound via per-run HTTPS proxy URLs (for exposing dev servers the agent starts).

**FR8 — Observability.** Structured event stream per run (snapshot created, forked, resumed, crashed); resource metrics; OpenTelemetry export; the Timeline UI; a "flight recorder" mode that auto-snapshots every K tool-calls with ring-buffer retention.

**FR9 — Security & tenancy.** Hard multi-tenancy via microVM boundary; per-tenant encryption keys for snapshot blobs (AES-256-GCM, envelope encryption); snapshot sharing is explicit and audit-logged; SOC 2 Type II on the cloud roadmap (month 9–12).

**FR10 — Lifecycle & policy.** Auto-pause after configurable idle threshold (default 5 min); auto-snapshot cadence; storage tiering (hot NVMe cache → object storage → archive); GC with legal-hold override.

#### 6.2 Non-goals (v1)

- **GPU-resident workloads.** Snapshotting GPU memory/state (CUDA contexts) is a research project (cuda-checkpoint is promising but immature). v1 targets CPU sandboxes that *call* GPU inference over the network — which is ~95% of agent workloads. GPU snapshot is on the v2 exploration list, not the v1 critical path.
- **Being an inference gateway.** Klados does not proxy or bill LLM tokens. It integrates with whatever provider the agent uses.
- **Windows/macOS guests.** Linux guests only in v1.
- **Cross-architecture restore.** x86_64 snapshots restore on x86_64 hosts with a compatible CPU template; no x86↔ARM portability (memory images are architecture-bound). We mitigate with fleet-wide CPU templates, not translation.
- **A new agent framework.** Klados is runtime infrastructure under existing frameworks, never a competing scaffold.

#### 6.3 UX requirements

- **Five-minute wow:** `pip install klados && klad run "claude-agent fix ./repo"` → dashboard shows a live timeline → user clicks any node → "Fork ×4" → four live agents visible in <5 s. This exact flow is the north-star onboarding path and the demo video.
- CLI ergonomics mirror Git deliberately: `klad log`, `klad checkout <snap>`, `klad branch`, `klad diff <a> <b>`.
- SDK is decorator/context-manager simple:

```python
from klados import Sandbox

sb = Sandbox.create(image="klados/agent-base:py3.12")
snap = sb.snapshot(label="before-risky-refactor")
children = snap.fork(4)
results = await asyncio.gather(*[run_strategy(c, s) for c, s in zip(children, STRATEGIES)])
best = pick(results); best.promote(); [c.destroy() for c in children if c is not best]
```

- Timeline UI requirements: DAG view, per-node metadata panel, one-click restore/fork/download-fs-diff, live terminal attach to any running node, shareable read-only run links.

### 7. Success metrics

| Category | Metric | 6-month target | 12-month target |
|----------|--------|----------------|-----------------|
| Performance | Snapshot pause time (2 GB VM, incremental) | p50 <250 ms | p50 <100 ms |
| Performance | Warm resume | p50 <500 ms | p50 <200 ms |
| Performance | 16-way same-host fork | p50 <2 s | p50 <750 ms |
| Adoption | GitHub stars on engine | 3,000 | 10,000 |
| Adoption | Weekly active cloud orgs | 150 | 1,000 |
| Adoption | Snapshots created/week (cloud) | 1 M | 25 M |
| Revenue | ARR | $150k | $1.5 M |
| Reliability | Resume success rate | 99.5% | 99.95% |
| Efficiency | Storage dedup ratio across tenant snapshots | ≥8× | ≥15× |

### 8. Pricing (initial hypothesis, to be validated)

- **Free tier:** 100 sandbox-hours/mo, 20 GB snapshot storage, 7-day retention, community support.
- **Pro ($99/seat-ish, usage-based core):** compute at ~$0.09/vCPU-hr + $0.35/GB-RAM-hr (paused = storage only), snapshot storage $0.05/GB-mo *post-dedup*, forks billed as compute only (fork operation itself free — this is the pricing headline: **"forks are free"**), static egress IP $2/mo.
- **Enterprise:** self-hosted control plane or dedicated cells, SSO/SCIM, audit export, SOC 2 report, private-VPC deployment; $60k+ ACV.
- Pricing philosophy: never tax the primitive we're evangelizing (snapshot/fork operations are free; you pay for compute-seconds and stored bytes), so usage of the differentiator compounds.

### 9. Rollout plan & milestones

**M0 (weeks 0–6) — Engine core.** Firecracker snapshot/restore wrapped in `kladosd`; full + diff snapshots; local restore; overlayfs-backed disk CoW; CLI (`run/snapshot/restore/log`). Exit criterion: the five-minute wow works on one machine, filmed.

**M1 (weeks 6–12) — Fork + Timeline.** Same-host N-way fork with CoW memory (see Tech Spec §5); timeline DAG store (SQLite→Postgres); fs-diff; guest fork-hooks; Python SDK alpha; launch open-source repo + demo video ("watch one agent become sixteen"). Exit: 16-way fork <2 s, HN/X launch.

**M2 (weeks 12–20) — Cloud alpha.** Control plane MVP: auth, org/projects, hosted fleet (start on bare-metal providers with nested-virt-free instances — e.g., Latitude/OVH/Hetzner dedicated — since Firecracker needs /dev/kvm), object-storage snapshot tiering with content-addressed dedup, Timeline UI, TS SDK, usage metering + Stripe. 25 design partners.

**M3 (weeks 20–28) — Durability GA.** Auto-snapshot policies, crash recovery, idle auto-pause, spot-interruption handler, LangGraph + Claude Agent SDK + OpenAI Agents adapters, MCP server. Public pricing. Exit: first paying teams running production agents.

**M4 (weeks 28–40) — Scale + determinism.** Cross-host fork placement, lazy cold-resume at fleet scale, record/replay network proxy (v1.5), golden-snapshot registry for evals/RL customers, live migration beta, SOC 2 audit begins.

### 10. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Incumbent fast-follow (E2B/Modal/Daytona ship "fork") | High | High | Speed + depth: they'll ship pause/resume; the moat is the *graph* (timelines, dedup storage economics, determinism, framework-native DX). Open-source engine makes Klados the default vocabulary. |
| Morph Labs (Infinibranch) is directly on this | Medium | High | Differentiate on open core, self-hosting, timeline/debugging UX, and evals/RL golden-snapshot workflow; move faster on framework integrations. Watch closely; don't pretend they don't exist. |
| Memory-image restore correctness (clock skew, entropy reuse, TLS session reuse, license daemons) breaks real workloads | High | Medium | This is the hard engineering. Guest agent + fork-hooks contract (§FR3.3, Tech Spec §6); compatibility test suite across top 50 packages/daemons; document Tier D honestly. |
| CPU heterogeneity breaks cross-host restore | Medium | Medium | Enforced fleet CPU templates (Firecracker CPU templates / named model masks); restore-compatibility checker refuses unsafe placements. |
| Snapshot storage costs eat margin | Medium | Medium | Content-addressed chunking (FastCDC) + zstd + shared base-image layers; measured dedup target ≥8×; aggressive GC defaults. |
| Agents mostly stay short-lived; durability demand is early | Low→Medium | High | The fork/evals use case (U2/U3) pays even for short runs; RL environment demand is here today. |
| KVM access constraints limit cloud footprint | Medium | Low | Bare-metal fleet strategy; container/CRIU fallback path for restricted environments (degraded guarantees, documented). |
| Security incident via snapshot sharing (secrets baked into images) | Medium | High | Secret-scanning on snapshot share, per-tenant KMS, share requires explicit flag + warning, redaction tooling. |

### 11. Open questions

1. Should conversation state (Tier C) be a first-class typed object in the API (enabling provider-aware features like context re-priming) or stay an opaque blob in v1? (Lean: opaque blob v1, typed v1.5.)
2. Fork billing at extreme fan-out (1,000-way for RL) — per-fork floor or pure compute?
3. Does the MCP server expose `fork` to the *agent itself* (self-forking agents)? Powerful but footgun-rich; likely behind a capability flag.
4. Multi-container guests (agent + Postgres + Redis in one VM via compose) — v1 or v1.5? (Lean: v1, it's just processes in the guest, and it demos beautifully.)
5. Name the paused state: "hibernated" vs "frozen" vs "parked."

### 12. Competitive landscape (summary)

- **E2B / Daytona / Modal Sandboxes:** excellent ephemeral sandboxes; E2B has basic pause/persistence; none have fork graphs, CoW memory forking, timelines, or determinism as product pillars. They compete on cold-start; we compete on *state*.
- **Morph Labs (Infinibranch):** closest conceptual competitor (snapshot/branch VMs for agents). Differentiation: open-source engine, self-host path, timeline debugging UX, evals/RL registry, framework-native SDKs.
- **Temporal / Restate / Inngest:** durable *workflow* state (application-level event sourcing), not durable *execution environments*. Complementary; an adapter is on the roadmap ("Temporal activity runs inside a Klados sandbox").
- **CRIU / Firecracker / Cloud Hypervisor:** underlying tech, not products. We are to them what Docker was to LXC/cgroups.
- **AWS Lambda SnapStart / Fargate:** proves snapshot-restore at massive scale, but sealed inside AWS's FaaS model; no fork, no user-visible graph.
- **Antithesis:** deterministic hypervisor for *testing*; validates the determinism market; different buyer and workload. Long-term inspiration for §FR6.

---

## Part II — Technical Specification

### 1. Architecture overview

```
┌────────────────────────── Control Plane (cloud) ──────────────────────────┐
│  API Gateway (REST/gRPC)   AuthN/Z (orgs, keys, RBAC)   Metering/Billing  │
│  Scheduler (placement, fork fan-out, migration)   Timeline/Graph Service  │
│  Snapshot Registry (manifests, content index)     Dashboard (Timeline UI) │
│                    Postgres (graph, metadata)  ·  ClickHouse (events)     │
└──────────────┬────────────────────────────────────────────┬───────────────┘
               │ mTLS gRPC (host agent ↔ control plane)     │
┌──────────────▼───────────── Host (bare-metal) ────────────▼───────────────┐
│  kladosd (host agent)                                                     │
│   ├─ VM Manager: Firecracker per workload (jailer, seccomp)               │
│   ├─ Snapshot Engine: full/diff mem snapshots, manifest builder           │
│   ├─ Fork Engine: CoW memory (UFFD server) + CoW disk (overlay/reflink)   │
│   ├─ Blob Agent: chunking (FastCDC) → zstd → S3; NVMe LRU cache           │
│   ├─ Net Manager: per-VM tap, nftables NAT, egress policy, proxy          │
│   └─ Telemetry: OTel metrics/events                                       │
│  Guest (per workload): minimal kernel + klados-guest-agent (vsock)        │
│   └─ user workload: agent process, headless browser, services, /klados/  │
└───────────────────────────────────────────────────────────────────────────┘
        Object storage (S3-compatible): content-addressed chunk store
```

**Key decisions up front:**

- **D1 — Firecracker microVMs as the primary isolation and snapshot unit.** Rationale: production-proven snapshot/restore (Lambda), minimal device model (small snapshot surface), hard tenancy boundary, ~125 ms cold boots when we need fresh VMs. Trade-off accepted: requires /dev/kvm → bare-metal fleet.
- **D2 — Browser runs inside the guest.** Chromium + CDP inside the VM means browser state (cookies, DOM, sessions, downloads) is captured by memory+disk snapshot *for free*, instead of the hopeless task of serializing browser state at the application layer. This single decision is a major differentiator vs. transcript-replay approaches.
- **D3 — Snapshots are content-addressed manifests referencing chunked blobs.** Enables dedup across snapshots, forks, tenants (base layers), and cheap timeline retention.
- **D4 — CRIU/containers is a secondary compatibility path**, not the core. CRIU is powerful but brittle across kernel versions and device types; we confine it to environments where KVM is unavailable, with documented reduced guarantees.
- **D5 — The guest agent is mandatory and tiny.** A static Rust binary on vsock handles: exec, health, fork-hooks, clock/entropy fixups, Tier C state flush signals. No SSH in the base image.

### 2. Workload model

A **Run** is the top-level object: one logical agent task. A Run owns a **Timeline** (DAG of **Snapshots**) and zero or more live **Instances** (running VMs). An Instance always has a lineage pointer to the Snapshot it was restored/forked from (or `genesis` for fresh boots from an **Image**). Images are OCI-derived root filesystems converted at registry-push time into Klados base layers (ext4 on a sparse file, pre-chunked).

State machine per Instance:

```
            create(image)                    snapshot()          
  ┌────────┐ ─────────▶ ┌─────────┐ ──────────────────▶ (Snapshot node)
  │ PENDING│            │ RUNNING │ ◀────────────────── restore()/fork()
  └────────┘            └─┬───┬───┘
                          │   │ pause()/idle-policy
                 crash    │   ▼
                          │ ┌────────┐   (VM destroyed; resume = restore
                          │ │ PAUSED │    of implicit pause-snapshot)
                          ▼ └────────┘
                    ┌──────────┐
                    │ CRASHED  │ → auto-restore(last snapshot) if policy
                    └──────────┘
```

`PAUSED` is implemented as *snapshot + destroy VM*: a paused instance consumes zero compute and only storage. This unifies pause/resume with the snapshot machinery instead of maintaining a separate suspend path.

### 3. Snapshot engine

**3.1 Mechanism.** Built on Firecracker's snapshot API with Klados orchestration around it:

1. `PATCH /vm {state: Paused}` — vCPUs stop (guest sees a time gap, handled in §6).
2. Optional pre-snapshot guest hook via vsock: `klados-guest-agent flush` — fsync dirty files, ask SDK to flush Tier C state to `/klados/state/`, quiesce optional daemons. Budget: 50 ms, best-effort, non-blocking beyond budget.
3. `PUT /snapshot/create` — Firecracker writes memory file + device/vCPU state file. For all snapshots after the first, use **diff mode** (dirty-page tracking enabled at boot) so only pages touched since the parent snapshot are written.
4. Disk: the workload's writable layer is an overlayfs upper dir (or reflinked file on XFS) over the base image; snapshot seals the current upper layer (rename + new empty upper via remount trick, or device-mapper thin snapshot on the block path — see 3.3) and records it.
5. Manifest build: memory chunks + disk layer chunks + device state + metadata → FastCDC chunking (avg 1 MiB, min 256 KiB, max 4 MiB) → BLAKE3 per chunk → zstd-3 → upload missing chunks to object store (dedup check against content index first) → write manifest; snapshot ID = BLAKE3(manifest).
6. `PATCH /vm {state: Resumed}` — total pause window = steps 1–4 only (upload happens after resume, from sealed copies). Target: p50 <250 ms for ≤2 GB RAM with dirty-page diff.

**3.2 Consistency model.** Snapshot is crash-consistent by construction (memory and sealed disk captured at the same paused instant) and *application-consistent* when the flush hook succeeds. In-flight outbound TCP is Tier D: connections are dead on restore; the guest network stack sees RSTs/timeouts and the SDK contract requires idempotent/reconnecting HTTP clients (our base images ship retrying clients configured; docs are loud about this).

**3.3 Disk layering.** Two supported backends, chosen per host:
- **overlayfs path (default v1):** base ext4 image (read-only, shared, chunk-dedup'd across all tenants using the same base) + per-instance upper dir on XFS with reflink. Sealing = atomic upper-dir rotation. Simple, filesystem-level diffs are trivially extractable (feeds the `klad diff` feature).
- **dm-thin path (v1.5):** device-mapper thin-provisioned block snapshots for workloads that need raw block semantics (databases in the guest). Faster seal, opaque diffs.

**3.4 Storage layout.**

```
s3://klados-<cell>/chunks/<b3-prefix>/<blake3>           # zstd chunk, immutable
s3://klados-<cell>/manifests/<snapshot-id>.json          # signed manifest
postgres: snapshots(id, run_id, parent_id, kind, labels, size_logical,
          size_physical_delta, created_at, retention_class, ...)
          chunks_refcount(chunk_id, refcount)            # GC via refcounting
```

GC is mark-and-sweep over manifest references with a 24 h grace period; refcount table is an optimization, manifests are the source of truth.


### 4. Restore engine

**4.1 Warm restore (chunks on local NVMe cache).** Reconstruct memory file + disk layers from cache, `PUT /snapshot/load`, resume. p50 target <500 ms for 2 GB.

**4.2 Cold restore with lazy paging.** Do not wait for the full memory image. Firecracker supports loading memory backed by a **userfaultfd (UFFD) handler**: `kladosd` registers as the UFFD server and materializes pages on first guest touch, fetching chunks from NVMe cache → object store in priority order. Warm-set prefetch: the snapshot manifest records the guest's recently-dirty page bitmap; those pages are prefetched eagerly (they predict the working set). Result: VM is executing in low single-digit seconds even when the full image is tens of GB, with a brief tail of demand-fetch latency.

**4.3 Restore-compatibility check.** A snapshot manifest records: CPU template ID, host kernel major, Firecracker version, guest kernel build. The scheduler only places restores on hosts satisfying the compatibility matrix; the fleet is provisioned in uniform "cells" (identical CPU template + software versions) precisely to make this check trivially satisfiable. Cell upgrades use blue/green host pools; old-cell snapshots remain restorable until migrated (background re-materialize: restore on old cell → live-migrate to new cell → re-snapshot).

### 5. Fork engine (the crown jewel)

Fork = restore, N times, with maximal sharing. Three levels of sharing:

**5.1 Disk:** trivially CoW — every child gets a fresh empty upper layer over the parent's sealed layers. Zero copy at fork time. (This is why the overlayfs decision matters.)

**5.2 Memory, same-host fork:** all N children are backed by UFFD against the *same* immutable memory image in the host page cache. First write in a child triggers a page fault handled by giving that child a private copy of that page ("copy-on-fault"). Practical effect: a 2 GB parent forked 16 ways consumes ~2 GB + Σ(divergent pages) instead of 32 GB, and fork latency is dominated by VM setup (~100–150 ms each, parallelizable) not memory copy. Implementation detail: one UFFD server thread-pool in `kladosd` serves all children of a base image; hot chunk pages are kept decompressed in a shared arena.

**5.3 Cross-host fork:** the scheduler picks hosts, each host's blob agent lazily pulls the base (most chunks are usually already cached because base images and common snapshots are fleet-hot); children start under lazy paging per §4.2. Fan-out of 100+ is a scheduler batching problem, not a data problem, thanks to content addressing.

**5.4 Post-fork divergence protocol (correctness-critical).** Immediately after a child resumes, `kladosd` signals the guest agent (`vsock: FORKED {child_index, new_seed}`), which synchronously:
1. Re-seeds kernel entropy (`RNDADDENTROPY` with host-provided randomness) — prevents N children generating identical "random" numbers, identical TLS client randoms, identical UUIDs. **This is the most dangerous correctness bug class in the whole system and gets its own test suite.**
2. Steps the guest clock to true time (or to the deterministic virtual clock if the run is in determinism mode — §9).
3. Regenerates machine-id and renews DHCP (each child has its own tap/MAC/IP).
4. Emits `KLADOS_FORKED` to `/klados/events` so the SDK can rotate provider session state, re-open connections, and (optionally) inject a per-branch instruction ("you are branch 3 of 16; your strategy: …").
5. Only then does the API report the child `READY`. Budget: <100 ms.

**5.5 Fork semantics exposed to users:** `snapshot.fork(n, placement="spread|pack", branch_context=[...])` — `branch_context` is delivered via step 4.4 and is how tree-search scaffolds inject per-branch prompts without post-fork tool calls.

### 6. Time, entropy, and identity correctness (the unglamorous 40% of the work)

Restoring a memory image is easy; making the resurrected world *coherent* is the actual product. Enumerated fixups, all owned by the guest agent + base-image configuration:

- **Clock:** guest boots with kvm-clock; on restore, host injects true time; guest agent fires a `time-jumped` notification; base images configure common runtimes for step-tolerance (e.g., disable naive monotonic-deadline assumptions in supervisors; document behavior for `setTimeout`/`asyncio` timers — pending timers fire immediately or re-arm per policy flag `clock_policy: step|freeze|replay`).
- **Entropy:** §5.4.1, plus base images use getrandom-backed CSPRNGs everywhere and we patch/configure runtimes known to cache entropy at startup.
- **TLS/session reuse:** long-lived provider connections are dead post-restore (Tier D); SDK middleware auto-retries idempotently. TLS session tickets cached pre-snapshot are safe to reuse or discard; we discard.
- **DHCP/ARP/MAC:** per-child re-plumb (§5.4.3); host side allocates fresh tap+MAC at fork/restore.
- **License/uniqueness daemons in guest:** documented pattern + hook point (`/klados/hooks/post-restore.d/`).
- **File locks / PID stability:** preserved inside the guest by construction (it's the same kernel image resumed) — a major advantage over CRIU-style process-level restore.

### 7. Networking

Per-instance: dedicated tap device in a per-tenant Linux netns on the host; nftables NAT for egress; optional egress policy (domain allow-list enforced by a transparent SNI/DNS filter, or full HTTP(S) forward proxy for record/replay mode); inbound exposure via control-plane-managed reverse proxy issuing `https://<instance>.run.klados.dev` routes (agent dev-servers, VNC/CDP endpoints for the Timeline UI's "attach" feature). Static egress IPs implemented as reserved SNAT pools per tenant.

### 8. Guest agent & base images

- `klados-guest-agent`: static musl Rust binary, <3 MB, vsock JSON-RPC. Capabilities: exec/PTY (powers CLI attach + SDK `sandbox.run()`), file put/get, flush hook, fork/restore fixups, event stream, health.
- Guest kernel: pinned minimal build (virtio-{blk,net,vsock}, overlayfs, no modules) per cell version.
- Base images (OCI-ingested): `klados/base` (debian-slim+agent), `klados/agent-py`, `klados/agent-node`, `klados/browser` (Chromium+CDP, fonts, xvfb-less headless), `klados/full` (the everything image for demos). Image pipeline converts OCI layers → ext4 base + pre-chunked blobs at push time, so first-boot of a popular image is always warm.

### 9. Determinism mode (v1.5, spec'd now so v1 doesn't preclude it)

For U2/U3 workloads that need reproducible branches: (a) pinned CPU template with deterministic-unfriendly features masked (RDRAND trapped, TSC virtualized), (b) virtual clock that advances only with executed guest time, (c) syscall-level entropy interception returning seeded streams, (d) **network record/replay**: all egress forced through the recording proxy; recordings stored as content-addressed cassettes attached to the snapshot lineage; replay mode serves responses from cassette and flags divergence (request not in cassette → policy: fail | passthrough-and-record | stub). This is deliberately proxy-level determinism, not Antithesis-grade whole-hypervisor determinism — sufficient for eval reproducibility, honest about its limits (in-guest thread scheduling remains nondeterministic).

### 10. Control plane

- **API:** REST + gRPC; resources: `runs, instances, snapshots, images, forks (verb), timelines, policies, egress_ips, events`. Idempotency keys on all mutations. Webhooks for state transitions (powers UC3's resume-on-approval).
- **Scheduler:** bin-packing with fork-affinity (children prefer parent's host until memory pressure), spot-tier support with interruption-triggered auto-snapshot (2-minute AWS warning ≫ 250 ms snapshot), anti-affinity for tenant spread, cell-compatibility constraints (§4.3).
- **Data stores:** Postgres (graph/metadata; timeline queries are recursive-CTE friendly, ltree for lineage paths), ClickHouse (event firehose, usage metering), Redis (scheduler queues, host heartbeats), S3-compatible object store per cell.
- **Tenancy/security:** org → project → API keys with scoped RBAC; per-tenant KMS data keys (envelope encryption of chunks — note: chunk dedup is therefore *per-tenant* for private layers, *global* only for public base images, an explicit margin trade-off for security); audit log on every snapshot access/share; jailer + seccomp + dedicated netns per VM on hosts.
- **Metering:** per-second vCPU/RAM sampling → ClickHouse → hourly rollups → Stripe usage records. Paused instances meter storage only.

### 11. SDKs, CLI, integrations

- **Python/TS SDKs:** `Sandbox`, `Snapshot`, `Timeline` objects; context managers; async-first; automatic Tier C state flushing for supported frameworks (a LangGraph checkpointer that writes to `/klados/state/`, a Claude Agent SDK session-persistence hook, an OpenAI Agents `Session` adapter); retrying HTTP client middleware (Tier D mitigation) included and on by default in base images.
- **MCP server:** exposes `snapshot_self`, `list_timeline`, `fork_self` (capability-gated per PRD open question 3) so agents can checkpoint before risky actions — "save before the boss fight" as an agent-native behavior.
- **CLI:** `klad run|log|checkout|branch|fork|diff|attach|ps|pause|resume|push` (image push). Git-shaped on purpose.
- **Terraform provider + Helm chart** for enterprise self-hosting (M4).

### 12. Testing & correctness strategy

- **Restore-fidelity suite:** matrix of {top 50 pip/npm packages, Postgres/Redis/Chromium in guest, long-running asyncio/node event loops} × {snapshot at randomized instants under load} × {restore, 16-fork} → assert workload-level invariants (server still serves, DB passes integrity check, browser session still authenticated, no duplicate UUIDs across forks, no identical TLS client randoms across forks).
- **Entropy divergence tests:** fork 64 children, each generates 10k random values/UUIDs/TLS handshakes → zero cross-child collisions.
- **Chaos:** kill -9 kladosd mid-snapshot, host power-loss simulation (blob agent must never publish a manifest before all chunks are durable — manifest-last ordering), network partition during cross-host fork.
- **Performance CI:** every merge runs the latency benchmarks (snapshot pause, warm/cold restore, 16-fork) on reference hardware; regressions >10% block merge. Published publicly (openly benchmarked latency is part of the GTM).

### 13. Team & effort estimate (first 6 months)

- 2 × systems engineers (Rust; Firecracker/UFFD/kernel-adjacent) — engine, fork, guest agent.
- 1 × distributed-systems/backend — control plane, scheduler, storage.
- 1 × product engineer — SDKs, Timeline UI, docs, demos.
- Founder (you) — engine architecture + the demo + design partners.
- Infra budget: ~$3–6k/mo (4–8 bare-metal hosts across two providers + object storage) until design partners scale it.

### 14. Appendix A — API sketch

```
POST   /v1/runs                          {image, resources, policies}
POST   /v1/instances/{id}/snapshot       {label?, flush: bool}   → {snapshot_id}
POST   /v1/snapshots/{id}/restore        {placement?}            → {instance_id}
POST   /v1/snapshots/{id}/fork           {n, placement, branch_context[]}
                                          → {instance_ids[]}
GET    /v1/runs/{id}/timeline            → DAG (nodes, edges, labels, sizes)
GET    /v1/snapshots/{a}/diff/{b}        → {fs_changes[], proc_diff, meta}
POST   /v1/instances/{id}/pause|resume
GET    /v1/instances/{id}/attach         (websocket: PTY / CDP passthrough)
POST   /v1/images                        (OCI ref ingest)
```

### 15. Appendix B — The demo script (M1 launch)

1. `klad run klados/full -- claude "make the test suite pass in ./repo"` — timeline starts growing, one node per tool call (flight-recorder policy).
2. Agent hits a fork in the road (two plausible fixes). Presenter clicks node 23 → **Fork ×4**, injects four branch strategies. Four terminals go live in ~2 s.
3. Branch 3 wins; `promote`, others destroyed; timeline shows the tree with the winning path highlighted.
4. Presenter yanks power (kills the host process) on the running winner mid-edit → auto-restore from last snapshot in 4 s, agent continues mid-thought.
5. Close on the storage panel: "17 snapshots, 4 forks, 2.1 GB logical × 9.3× dedup = 230 MB stored. Forks were free."

*Total runtime: 3 minutes. Every beat maps to a use case: UC2, UC1, UC4, pricing.*

---
*End of document. Ready for review — tear it apart.*