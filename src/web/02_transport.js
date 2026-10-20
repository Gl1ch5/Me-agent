// ─── transport: identical endpoints for local (`me web`) and cloud (/app) ───
async function api(path, body, method) {
  const h = {"Content-Type": "application/json"};
  if (token) h.Authorization = "Bearer " + token;
  const r = await fetch(path, {method: method || (body !== undefined ? "POST" : "GET"), headers: h, body: body !== undefined ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && MODE === "cloud") { location.href = "/account#cloud"; throw new Error("login"); }
  if (!r.ok) { clientLog(`${r.status} ${path}`, j.detail || r.statusText); throw new Error(j.detail || r.statusText); }
  return j;
}
// cloud: errors go to the account's debug log on the server (kept only for accounts the admin traces)
let logged = 0;
function clientLog(msg, where, stack) {
  if (MODE !== "cloud" || !token || logged++ > 40 || /\/api\/clientlog/.test(where || "")) return;
  fetch("/api/clientlog", {method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer " + token},
    body: JSON.stringify({msg: String(msg).slice(0, 500), where: String(where || "").slice(0, 300), stack: String(stack || "").slice(0, 2000)})}).catch(() => {});
}
addEventListener("error", e => clientLog(e.message, `${e.filename}:${e.lineno}:${e.colno}`, e.error && e.error.stack));
addEventListener("unhandledrejection", e => { const r = e.reason || {}; if (r.message !== "login") clientLog(r.message || String(r), "promise", r.stack); });
const lane = () => +((base.match(/\/w\/(\d+)/) || [])[1] || 0);
const A = {
  async boot() {
    if (MODE === "local") {
      const k = new URLSearchParams(location.search).get("k"); if (k) { token = k; store.set("me_token", k); }
      base = "/api/agent"; return api(base + "/state");
    }
    if (!token) { location.href = "/account#cloud"; return; }
    const q = new URLSearchParams(location.search);
    let w;
    if (q.get("s") || q.get("ws")) w = await api("/api/cloud", {session: q.get("s"), ws: q.get("ws")});
    else w = (await api("/api/cloud"))[0] || await api("/api/cloud", {});
    return A.attach(w);
  },
  async attach(w) { base = "/api/cloud/" + w.id; const st = await A.state(w); st.after = w.seq || 0; return st; },
  async state(w) {
    if (MODE === "local") return api(base + "/state");
    const [cfg, sessions, me] = await Promise.all([api("/api/doc?key=config").then(d => JSON.parse(d.content)).catch(() => ({})), api("/api/sessions"), api("/api/me")]);
    let session = null;
    if (w && w.session) session = await api("/api/doc?key=" + encodeURIComponent("session/" + w.session)).then(d => JSON.parse(d.content)).catch(() => null);
    const [ag, man] = await Promise.all([api("/api/agents").catch(() => ({agents: [], current: "code"})),
      w && w.ws ? api(`/api/ws/${w.ws}/manifest`).catch(() => ({files: {}})) : {files: {}}]);
    const context = ["AGENTS.md", "ME.md", "CLAUDE.md", "DESIGN.md", "PLAN.md"].filter(f => f in man.files);
    return {mode: "cloud", user: me.username, ctx: session?.ctx || 0, config: {model: (session?.model && (cfg.models || []).some(m => m.name === session.model)) ? session.model : cfg.model, models: cfg.models || [], prompt: cfg.prompt || "", autocompact: (cfg.settings || {}).autocompact ?? 80, search: searchCounts(cfg), search_asked: (cfg.settings || {}).search_asked || []}, sessions, session, ws: w && w.ws, worker: w,
            lang: (cfg.settings || {}).lang || "", behavior: {...BEHAVIOR_DEFAULTS, ...((cfg.settings || {}).no_verify ? {tests: false} : {}), ...((cfg.settings || {}).behavior || {})},
            agent: session?.agent || ag.current, agents: ag.agents, context, perm: "sandbox", preview: w && w.preview, push: me.push};
  },
  async load(id) {  // local: the chat may already be running in another lane — attach to it instead of reloading
    if (MODE === "local") { base = (await api("/api/agent/lanes", {session: id, from: lane()})).base; return api(base + "/state"); }
    const w = await api("/api/cloud", {session: id}); return A.attach(w);
  },
  async newChat(ws) {
    if (MODE === "cloud" && ws && ws !== state.ws) {  // another project: a sandbox on that workspace
      const s = await A.attach(await api("/api/cloud", {ws}));
      return {...s, session: null, ctx: 0};
    }
    if (MODE === "cloud") {  // the sandbox is still working on the previous chat: the new one gets its own, so both can run
      const mine = await api("/api/cloud").catch(() => []), w0 = mine.find(x => x.id === state.worker?.id);
      if (!w0 || w0.busy) {
        const w = await api("/api/cloud", {ws: state.ws}), s = await A.attach(w);  // no session = a new sandbox
        return {...s, session: null, ctx: 0};
      }
    }
    await A.free();
    await api(base + "/new", {});
    if (MODE === "local") return A.state();
    // the sandbox starts the new session asynchronously: never reload the old one through the worker's previous session id
    const w = {...(state.worker || {}), session: null}, s = await A.state(w);
    return {...s, session: null, ctx: 0, after};  // keep the live event cursor: older events must not replay
  },
  async free() { if (MODE === "local") base = (await api("/api/agent/lanes", {from: lane()})).base; },  // an idle lane: this one, or another while it works
  async running() {  // ids of the chats being worked on right now, in any lane / sandbox
    const l = MODE === "local" ? await api("/api/agent/lanes") : await api("/api/cloud");
    return l.filter(x => x.busy && x.session).map(x => x.session);
  },
  async setAgent(name) {
    if (MODE === "local") return api(base + "/agent", {name});
    const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.agent = name;
    await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT"); await api(base + "/sync", {});
  },
  async setModel(name) {
    if (MODE === "local") return api(base + "/model", {name});
    // the chat keeps this model (other chats keep theirs); it is also the default for new chats
    const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.model = name;
    await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT"); await api(base + "/sync", {});
    await api(base + "/model", {name}).catch(() => {});
  },
  prompt: (text, images) => api(base + "/prompt", {text, images: images || []}),
  goal: (text) => api(base + "/goal", {text}),
  undo: () => api(base + "/undo", {}),
  async listFiles() {  // every project file, for @ completion
    if (MODE === "local") return (await api(base + "/files", {all: true})).paths;
    return state.ws ? Object.keys((await api(`/api/ws/${state.ws}/manifest`)).files || {}) : [];
  },
  async upload(name, b64) {  // non-image attachments go into the project so the agent can read them
    if (MODE === "local") return (await api(base + "/upload", {name, b64})).path;
    const path = "uploads/" + name.replace(/[^\w.-]+/g, "_").slice(0, 80);
    await api(`/api/ws/${state.ws}/files`, {put: [{path, b64, mtime: Date.now() / 1000}]});
    await api(base + "/sync", {}).catch(() => {});
    return path;
  },
  async setPerm(mode) {
    if (MODE === "local") return api(base + "/perm", {mode});
    const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.settings = {...(cfg.settings || {}), perm: mode, yolo: false};
    await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT"); await api(base + "/sync", {});
  },
  voice: (blob) => fetch(MODE === "local" ? "/api/agent/voice" : "/api/voice", {method: "POST", headers: {"Content-Type": blob.type, ...(token ? {Authorization: "Bearer " + token} : {})}, body: blob}).then(r => r.json()),
};
