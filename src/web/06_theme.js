// ─── themes: same tokens as the terminal (see 14a_themes.py) ───
const THEME_VARS = {bg: "--bg", bg2: "--bg2", bg3: "--bg3", line: "--line", fg: "--fg", dim: "--dim", faint: "--faint", accent: "--a", accent2: "--b", ok: "--ok", err: "--err", warn: "--warn", user: "--user"};
let themeList = [];
function applyTheme(t) {
  if (!t) return;
  const r = document.documentElement;
  for (const [k, v] of Object.entries(THEME_VARS)) if (t.colors[k]) r.style.setProperty(v, t.colors[k]);
  r.style.colorScheme = t.dark === false ? "light" : "dark";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t.colors.bg);
  try { localStorage.setItem("me_theme_cache", JSON.stringify(t)); } catch (e) {}
}
try { applyTheme(JSON.parse(localStorage.getItem("me_theme_cache"))); } catch (e) {}
async function loadThemes() {
  const r = await api(MODE === "local" ? "/api/agent/themes" : "/api/themes");
  themeList = r.themes; applyTheme(themeList.find(t => t.name === r.current));
}
function pickTheme() {
  $("pbody").innerHTML = themeList.map(t => `<button onclick="setTheme('${esc(t.name)}')"><span class="sw" style="background:${t.colors.bg};border-color:${t.colors.line}"><i style="background:${t.colors.accent}"></i><i style="background:${t.colors.accent2}"></i></span>${esc(t.label || t.name)}<span class="d">${t.dark === false ? "light" : "dark"}</span></button>`).join("")
    + `<a class="more" href="${MODE === "cloud" ? "/account#market" : "#"}" onclick="${MODE === "local" ? "alert('Install more with /theme in the terminal or the marketplace on the site');return false" : ""}">more themes in the marketplace →</a>`;
  $("popup").classList.remove("hidden");
}
async function setTheme(name) {
  applyTheme(themeList.find(t => t.name === name)); $("popup").classList.add("hidden");
  if (MODE === "local") return api("/api/agent/theme", {name});
  const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.theme = name;
  await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT");
}
