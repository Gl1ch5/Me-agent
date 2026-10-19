// me for VS Code: runs `me web` (the local agent) in the workspace folder and shows its UI in an editor panel.
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

async function open(ctx) {
  try { await start(ctx); } catch (e) { return vscode.window.showErrorMessage("me: " + e.message); }
  if (panel) { panel.reveal(vscode.ViewColumn.Beside, true); return panel; }
  panel = vscode.window.createWebviewPanel("me", "me", {viewColumn: vscode.ViewColumn.Beside, preserveFocus: false},
    {enableScripts: true, retainContextWhenHidden: true});
  const uri = await vscode.env.asExternalUri(vscode.Uri.parse(`http://localhost:${port}/`));
  panel.webview.html = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${uri.scheme}://${uri.authority}; style-src 'unsafe-inline';">
<style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100%;overflow:hidden;background:#0b0c10}</style></head>
<body><iframe src="${uri.toString(true)}" allow="clipboard-read; clipboard-write; microphone"></iframe></body></html>`;
  panel.onDidDispose(() => panel = null);
  return panel;
}

async function ask(ctx, text) {
  await open(ctx);
  try { await call("/prompt", {text}); } catch (e) { vscode.window.showWarningMessage("me: " + (String(e.message).includes("still working") ? "the agent is busy — try again when it finishes" : e.message)); }
}

function activate(ctx) {
  out = vscode.window.createOutputChannel("me");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "me.open"; status.tooltip = "Open the me agent"; setStatus("$(comment-discussion) me");
  ctx.subscriptions.push(out, status,
    vscode.commands.registerCommand("me.open", () => open(ctx)),
    vscode.commands.registerCommand("me.restart", async () => { stop(); if (panel) panel.dispose(); await open(ctx); }),
    vscode.commands.registerCommand("me.askSelection", async () => {
      const ed = vscode.window.activeTextEditor; if (!ed) return;
      const sel = ed.document.getText(ed.selection);
      const rel = vscode.workspace.asRelativePath(ed.document.uri);
      const q = await vscode.window.showInputBox({prompt: "Ask me about the selection", placeHolder: "explain / refactor / find the bug…"});
      if (q === undefined) return;
      const a = ed.selection.start.line + 1, b = ed.selection.end.line + 1;
      await ask(ctx, `${q || "Explain this code."}\n\n${rel}:${a}-${b}\n\`\`\`${ed.document.languageId}\n${sel}\n\`\`\``);
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
