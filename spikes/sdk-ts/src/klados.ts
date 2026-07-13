// Klados TypeScript SDK (M2 alpha) — a faithful port of the Python SDK, talking to kladosd.
//
//   import { Sandbox } from "./klados";
//   const sb = await Sandbox.create("klados/agent-base:py3.12");
//   const snap = await sb.snapshot("before-risky-refactor");
//   const children = await snap.fork(4);
//
// Config via env: KLADOS_API (default http://127.0.0.1:7070), KLADOS_API_KEY.

const API = process.env.KLADOS_API ?? "http://127.0.0.1:7070";
const KEY = process.env.KLADOS_API_KEY ?? "";

async function call(method: string, path: string, body?: unknown): Promise<any> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (KEY) headers["X-Api-Key"] = KEY;
  const res = await fetch(API + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

export class Timeline {
  constructor(private readonly data: any) {}
  get runId(): string { return this.data.run_id; }
  get snapshots(): any[] { return this.data.snapshots; }
  get instances(): any[] { return this.data.instances; }
}

export class Snapshot {
  constructor(public readonly id: string, public readonly runId: string) {}

  /** Create N copy-on-write children that diverge from this instant. */
  async fork(n = 1): Promise<Sandbox[]> {
    const r = await call("POST", `/v1/snapshots/${this.id}/fork`, { n });
    return r.children.map((c: any) => new Sandbox(c.instance_id, this.runId, c.branch));
  }

  /** Filesystem diff of the /data layer against another snapshot. */
  async diff(other: Snapshot): Promise<any> {
    return call("GET", `/v1/snapshots/${this.id}/diff/${other.id}`);
  }
}

export class Sandbox {
  constructor(
    public readonly instanceId: string,
    public readonly runId: string,
    public readonly branch?: string,
  ) {}

  static async create(image = "klados/agent"): Promise<Sandbox> {
    const r = await call("POST", "/v1/runs", { image });
    return new Sandbox(r.instance_id, r.run_id, "genesis");
  }

  async snapshot(label = "snap"): Promise<Snapshot> {
    const r = await call("POST", `/v1/instances/${this.instanceId}/snapshot`, { label });
    return new Snapshot(r.snapshot_id, this.runId);
  }

  async timeline(): Promise<Timeline> {
    return new Timeline(await call("GET", `/v1/runs/${this.runId}/timeline`));
  }

  async destroy(): Promise<void> {
    await call("POST", `/v1/instances/${this.instanceId}/destroy`, {});
  }
}
