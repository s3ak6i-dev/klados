// TS SDK demo — the PRD north-star flow via the Klados TypeScript SDK.
import { Sandbox } from "./klados";

(async () => {
  console.log("Sandbox.create(...)");
  const sb = await Sandbox.create("klados/agent-base:py3.12");
  console.log("  instance", sb.instanceId);

  const snap = await sb.snapshot("before-risky-refactor");
  console.log("sb.snapshot():", snap.id);

  const children = await snap.fork(4);
  console.log(`snap.fork(4) -> ${children.length} children:`);
  for (const c of children) console.log(`    ${c.instanceId}  [${c.branch}]`);

  const tl = await sb.timeline();
  console.log(`sb.timeline(): ${tl.snapshots.length} snapshots, ${tl.instances.length} instances`);

  for (const c of children) await c.destroy();
  await sb.destroy();
  console.log("\nOK — TypeScript SDK works end to end");
})().catch((e) => {
  console.error("error:", e.message);
  process.exit(1);
});
