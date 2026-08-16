// Local smoke test for the Cloudflare relay (Durable Object logic) WITHOUT
// deploying. Mocks the DO storage in memory and drives the real fetch()
// handlers, asserting the exact behaviors that fix the bug report:
//   1. score upsert
//   2. per-username DEDUP (the "3 of me / 2 of Rygel" bug)
//   3. board ranking by ops
//   4. chat append + cap
//
// Run:  node relay/test_relay.js   (from the repo root)

import { OfficeDO } from "./src/worker.js";

function memStorage() {
  const m = new Map();
  let alarm = null;
  return {
    async get(k) { return m.has(k) ? structuredClone(m.get(k)) : undefined; },
    async put(k, v) { m.set(k, structuredClone(v)); },
    async getAlarm() { return alarm; },
    async setAlarm(t) { alarm = t; },
  };
}

function mkDO(env = {}) {
  return new OfficeDO({ storage: memStorage() }, env);
}

async function call(do_, method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await do_.fetch(new Request("https://x.test" + path, opts));
  return { status: res.status, json: await res.json() };
}

let pass = 0, fail = 0;
function ok(cond, name) {
  if (cond) { pass++; console.log(`[PASS] ${name}`); }
  else { fail++; console.log(`[FAIL] ${name}`); }
}

const do_ = mkDO();

// --- 1. upsert a score ---
await call(do_, "POST", "/api/score", {
  entry: { id: "uuid-a", github: "DrGekoz", name: "DrGekoz", ops: 1000, tools: 50, counter: 1 },
});
let board = (await call(do_, "GET", "/api/board")).json.board;
ok(board.length === 1 && board[0].github === "DrGekoz" && board[0].ops === 1000, "upsert: one DrGekoz row, ops 1000");

// --- 2. DEDUP: same github from a DIFFERENT browser (new uuid) must NOT add a row ---
await call(do_, "POST", "/api/score", {
  entry: { id: "uuid-b", github: "DrGekoz", name: "DrGekoz", ops: 1050, tools: 55, counter: 2 },
});
await call(do_, "POST", "/api/score", {
  entry: { id: "uuid-c", github: "DrGekoz", name: "DrGekoz", ops: 900, tools: 40, counter: 3 },
});
board = (await call(do_, "GET", "/api/board")).json.board;
ok(board.filter((r) => r.github === "DrGekoz").length === 1, "dedup: 3 uuids of DrGekoz -> exactly 1 row");
ok(board[0].ops === 1050, "dedup: keeps highest ops (1050, not the later 900)");

// --- 3. ranking: second user sorts below ---
await call(do_, "POST", "/api/score", {
  entry: { id: "uuid-r", github: "Rygel", name: "Rygel", ops: 500, tools: 10, counter: 1 },
});
board = (await call(do_, "GET", "/api/board")).json.board;
ok(board.length === 2, "two distinct users -> two rows");
ok(board[0].github === "DrGekoz" && board[0].rank === 1, "ranking: higher ops ranks first");
ok(board[1].github === "Rygel" && board[1].rank === 2, "ranking: Rygel ranks second");

// --- 4. chat append + cap ---
await call(do_, "POST", "/api/chat", { user: "DrGekoz", text: "hello office" });
let chat = (await call(do_, "GET", "/api/chat/list")).json.chat;
ok(chat.length === 1 && chat[0].text === "hello office", "chat: append works");
for (let i = 0; i < 310; i++) await call(do_, "POST", "/api/chat", { user: "x", text: "m" + i });
chat = (await call(do_, "GET", "/api/chat/list")).json.chat;
ok(chat.length === 300, "chat: capped at 300");

// --- 5. gist snapshot requires secrets ---
const g = (await call(do_, "POST", "/api/gist", {})).json;
ok(g.ok === false && g.reason, "gist: returns no-secrets reason without token");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
