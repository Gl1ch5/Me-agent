// ─── transport: identical endpoints for local (`me web`) and cloud (/app) ───
async function api(path, body, method) {
  const h = {"Content-Type": "application/json"};
  if (token) h.Authorization = "Bearer " + token;
  const r = await fetch(path, {method: method || (body !== undefined ? "POST" : "GET"), headers: h, body: body !== undefined ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && MODE === "cloud") { location.href = "/account#cloud"; throw new Error("login"); }
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
const A = {
  async boot() {
    if (MODE === "local") {
      const k = new URLSearchParams(location.search).get("k"); if (k) { token = k; localStorage.setItem("me_token", k); }
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
    return {mode: "cloud", user: me.username, ctx: session?.ctx || 0, config: {model: cfg.model, models: cfg.models || [], prompt: cfg.prompt || "", autocompact: (cfg.settings || {}).autocompact ?? 80}, sessions, session, ws: w && w.ws, worker: w,
            agent: session?.agent || ag.current, agents: ag.agents, context, perm: "sandbox", preview: w && w.preview, push: me.push};
  },
  async load(id) {
    if (MODE === "local") { await api(base + "/load", {id}); return api(base + "/state"); }
    const w = await api("/api/cloud", {session: id}); return A.attach(w);
  },
  async newChat() {
    await api(base + "/new", {});
    if (MODE === "local") return A.state();
    // the sandbox starts the new session asynchronously: never reload the old one through the worker's previous session id
    const w = {...(state.worker || {}), session: null}, s = await A.state(w);
    return {...s, session: null, ctx: 0, after};  // keep the live event cursor: older events must not replay
  },
  async setAgent(name) {
    if (MODE === "local") return api(base + "/agent", {name});
    const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.agent = name;
    await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT"); await api(base + "/sync", {});
  },
  async setModel(name) {
    if (MODE === "local") return api(base + "/model", {name});
    const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.model = name;
    await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT"); await api(base + "/sync", {});
  },
  prompt: (text, images) => api(base + "/prompt", {text, images: images || []}),
  goal: (text) => api(base + "/goal", {text}),
  undo: () => api(base + "/undo", {}),
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
