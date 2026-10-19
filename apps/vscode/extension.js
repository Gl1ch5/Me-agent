// me for VS Code: runs `me web` (the local agent) in the workspace folder and shows its UI in the side bar (or an editor tab).
// The agent works on the real files of the open folder; edits show up in the editor, /undo reverts them.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const http = require("http");
const https = require("https");
const net = require("net");
const os = require("os");
const path = require("path");

let proc = null, port = 0, panel = null, status = null, out = null, folder = "";

function cfg(k) { return vscode.workspace.getConfiguration("me").get(k); }

function meHome() { return process.env.ME_HOME || path.join(os.homedir(), ".me"); }

function findPython() {
  const cands = [cfg("python"), process.platform === "win32" ? "py" : "python3", "python3", "python"].filter(Boolean);
  for (const c of cands) {
    try {
      const args = c === "py" ? ["-3", "--version"] : ["--version"];
      const r = cp.spawnSync(c, args, {encoding: "utf8", windowsHide: true});
      const v = (r.stdout || r.stderr || "").match(/Python (\d+)\.(\d+)/);
      if (r.status === 0 && v && (+v[1] > 3 || (+v[1] === 3 && +v[2] >= 8))) return c === "py" ? ["py", "-3"] : [c];
    } catch (e) { /* try the next one */ }
  }
  return null;
}

function download(url, dest) {
  return new Promise((res, rej) => {
    https.get(url, r => {
      if (r.statusCode >= 300 && r.statusCode < 400 && r.headers.location) return download(r.headers.location, dest).then(res, rej);
      if (r.statusCode !== 200) return rej(new Error("HTTP " + r.statusCode));
      const tmp = dest + ".tmp", f = fs.createWriteStream(tmp);
      r.pipe(f); f.on("finish", () => f.close(() => { fs.renameSync(tmp, dest); res(dest); }));
    }).on("error", rej);
  });
}

async function agentPath(ctx) {
  if (cfg("agentPath") && fs.existsSync(cfg("agentPath"))) return cfg("agentPath");
  const installed = path.join(meHome(), "me.py");
  if (fs.existsSync(installed)) return installed;
  fs.mkdirSync(ctx.globalStorageUri.fsPath, {recursive: true});
  const dest = path.join(ctx.globalStorageUri.fsPath, "me.py");
  if (!fs.existsSync(dest)) {
    await vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: "me: downloading the agent…"},
      () => download((cfg("server") || "https://me-agent.ru").replace(/\/$/, "") + "/me.py", dest));
  }
  return dest;
}

function freePort() {
  return new Promise(res => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); }); });
}

function waitUp(p, ms = 15000) {
  const end = Date.now() + ms;
  return new Promise((res, rej) => {
    const tryOnce = () => http.get({host: "127.0.0.1", port: p, path: "/api/agent/state", headers: {Host: "localhost:" + p}}, r => { r.resume(); res(); })
      .on("error", () => Date.now() > end ? rej(new Error("the agent did not start (see Output → me)")) : setTimeout(tryOnce, 250));
    tryOnce();
  });
}

function call(pathname, body) {
  return new Promise((res, rej) => {
    const data = Buffer.from(JSON.stringify(body || {}));
    const r = http.request({host: "127.0.0.1", port, path: "/api/agent" + pathname, method: "POST",
      headers: {Host: "localhost:" + port, "Content-Type": "application/json", "Content-Length": data.length}}, resp => {
      let b = ""; resp.on("data", c => b += c); resp.on("end", () => resp.statusCode < 300 ? res(b) : rej(new Error(b)));
    });
    r.on("error", rej); r.end(data);
  });
}

async function start(ctx) {
  const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || os.homedir();
  if (proc && folder === ws) return;
  stop();
  const py = findPython();
  if (!py) {
    const pick = await vscode.window.showErrorMessage("me needs Python 3.8+.", "Download Python");
    if (pick) vscode.env.openExternal(vscode.Uri.parse("https://www.python.org/downloads/"));
    throw new Error("no python");
  }
  const me = await agentPath(ctx);
  port = await freePort();
  folder = ws;
  out.appendLine(`starting: ${py.join(" ")} ${me} web ${port} --no-open   (in ${ws})`);
  proc = cp.spawn(py[0], [...py.slice(1), me, "web", String(port), "--no-open"], {cwd: ws, windowsHide: true,
    env: {...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1"}});
  proc.stdout.on("data", d => out.append(d.toString()));
  proc.stderr.on("data", d => out.append(d.toString()));
  proc.on("exit", code => { out.appendLine(`agent exited (${code})`); proc = null; setStatus("$(debug-disconnect) me"); });
  await waitUp(port);
  setStatus("$(comment-discussion) me");
}

function stop() {
  if (proc) { try { process.platform === "win32" ? cp.spawnSync("taskkill", ["/F", "/T", "/PID", String(proc.pid)]) : proc.kill(); } catch (e) {} }
  proc = null;
}

function setStatus(text) { if (status) { status.text = text; status.show(); } }

// the chat page: an iframe of the local web UI. The extension talks to it through postMessage (insert text, send, new chat).
async function chatHtml(webview) {
  const uri = await vscode.env.asExternalUri(vscode.Uri.parse(`http://localhost:${port}/?embed=vscode`));
  const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${uri.scheme}://${uri.authority}; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100%;overflow:hidden;background:#0b0c10}</style></head>
<body><iframe id="f" src="${uri.toString(true)}" allow="clipboard-read; clipboard-write; microphone"></iframe>
<script nonce="${nonce}">
const f = document.getElementById("f"), host = acquireVsCodeApi(); let ready = false; const q = [];
f.addEventListener("load", () => { ready = true; setTimeout(() => q.splice(0).forEach(m => f.contentWindow.postMessage(m, "*")), 600); });
window.addEventListener("message", e => { if (e.source === f.contentWindow) { if (e.data && e.data.meOut) host.postMessage(e.data.meOut); return; }
  if (!e.data || !e.data.me) return; ready ? f.contentWindow.postMessage(e.data.me, "*") : q.push(e.data.me); });
</script></body></html>`;
}

const views = new Set();  // live chat webviews (the side bar view and/or an editor tab)

function post(msg) { for (const w of views) w.postMessage({me: msg}); return views.size > 0; }

async function attach(ctx, webview, onGone) {
  webview.options = {enableScripts: true};
  webview.html = `<body style="font:13px sans-serif;color:#9aa;padding:14px">starting the me agent…</body>`;
  try { await start(ctx); } catch (e) { webview.html = `<body style="font:13px sans-serif;color:#f88;padding:14px">me: ${String(e.message).replace(/</g, "&lt;")}</body>`; return; }
  webview.html = await chatHtml(webview);
  webview.onDidReceiveMessage(m => m && m.type === "open" && openFile(m.path, m.line));
  views.add(webview);
  onGone(() => views.delete(webview));
}

class ChatView {
  constructor(ctx) { this.ctx = ctx; }
  resolveWebviewView(view) { this.view = view; attach(this.ctx, view.webview, f => view.onDidDispose(f)); }
}

async function focusChat() {
  if (panel) { panel.reveal(undefined, true); return; }
  await vscode.commands.executeCommand("me.chat.focus");
}

async function openPanel(ctx) {  // the chat as an editor tab beside the code
  if (panel) { panel.reveal(vscode.ViewColumn.Beside, true); return panel; }
  panel = vscode.window.createWebviewPanel("me", "me", {viewColumn: vscode.ViewColumn.Beside, preserveFocus: false}, {enableScripts: true, retainContextWhenHidden: true});
  panel.iconPath = vscode.Uri.joinPath(ctx.extensionUri, "media", "me.svg");
  await attach(ctx, panel.webview, f => panel.onDidDispose(() => { f(); panel = null; }));
  return panel;
}

async function insert(ctx, text) {  // into the chat input: the user finishes the question there
  await focusChat();
  for (let i = 0; i < 40 && !views.size; i++) await new Promise(r => setTimeout(r, 250));
  post({type: "insert", text});
}

async function ask(ctx, text) {
  await focusChat();
  for (let i = 0; i < 40 && !views.size; i++) await new Promise(r => setTimeout(r, 250));
  if (post({type: "send", text})) return;
  try { await start(ctx); await call("/prompt", {text}); } catch (e) { vscode.window.showWarningMessage("me: " + (String(e.message).includes("still working") ? "the agent is busy — try again when it finishes" : e.message)); }
}

async function openFile(p, line) {  // a path the agent read or changed, clicked in the chat
  const root = (vscode.workspace.workspaceFolders || [])[0];
  const uri = path.isAbsolute(p) || !root ? vscode.Uri.file(p) : vscode.Uri.joinPath(root.uri, p);
  try {
    const ed = await vscode.window.showTextDocument(uri, {viewColumn: vscode.ViewColumn.One, preview: true});
    if (line > 0) { const pos = new vscode.Position(line - 1, 0); ed.selection = new vscode.Selection(pos, pos); ed.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter); }
  } catch (e) { vscode.window.showWarningMessage("me: cannot open " + p); }
}

function ref(uri, sel) {
  const rel = vscode.workspace.asRelativePath(uri);
  return sel && !sel.isEmpty ? `@${rel}:${sel.start.line + 1}-${sel.end.line + (sel.end.character ? 1 : 0)}` : `@${rel}`;
}

function activate(ctx) {
  out = vscode.window.createOutputChannel("me");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "me.open"; status.tooltip = "Open the me agent"; setStatus("$(comment-discussion) me");
  ctx.subscriptions.push(out, status,
    vscode.window.registerWebviewViewProvider("me.chat", new ChatView(ctx), {webviewOptions: {retainContextWhenHidden: true}}),
    vscode.commands.registerCommand("me.open", () => focusChat()),
    vscode.commands.registerCommand("me.openPanel", () => openPanel(ctx)),
    vscode.commands.registerCommand("me.newChat", async () => { await focusChat(); post({type: "new"}); }),
    vscode.commands.registerCommand("me.restart", async () => {
      stop(); folder = "";
      try { await start(ctx); } catch (e) { return vscode.window.showErrorMessage("me: " + e.message); }
      for (const w of views) w.html = await chatHtml(w);
    }),
    vscode.commands.registerCommand("me.askSelection", async () => {
      const ed = vscode.window.activeTextEditor; if (!ed) return;
      const sel = ed.document.getText(ed.selection);
      const r = ref(ed.document.uri, ed.selection);
      await insert(ctx, sel && sel.length < 4000 ? `${r}\n\`\`\`${ed.document.languageId}\n${sel}\n\`\`\`\n` : r + " ");
    }),
    vscode.commands.registerCommand("me.addFile", async (uri, uris) => {
      const list = (uris && uris.length ? uris : [uri || vscode.window.activeTextEditor?.document.uri]).filter(Boolean);
      if (list.length) await insert(ctx, list.map(u => ref(u)).join(" ") + " ");
    }),
    vscode.commands.registerCommand("me.fixFile", async () => {
      const ed = vscode.window.activeTextEditor; if (!ed) return;
      const diags = vscode.languages.getDiagnostics(ed.document.uri).filter(d => d.severity <= vscode.DiagnosticSeverity.Warning);
      const rel = vscode.workspace.asRelativePath(ed.document.uri);
      const list = diags.slice(0, 40).map(d => `- line ${d.range.start.line + 1}: ${d.message}`).join("\n");
      await ask(ctx, diags.length ? `Fix these problems in ${rel}, then verify:\n${list}` : `Review ${rel} for bugs and fix real ones, then verify.`);
    }),
    vscode.commands.registerCommand("me.terminal", async () => {
      const py = findPython(); if (!py) return vscode.window.showErrorMessage("me needs Python 3.8+.");
      const t = vscode.window.createTerminal({name: "me"});
      t.sendText(`${py.join(" ")} "${await agentPath(ctx)}"`); t.show();
    }),
    {dispose: stop});
}

function deactivate() { stop(); }

module.exports = {activate, deactivate};
