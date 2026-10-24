// ─── themes: same tokens as the terminal (see 14a_themes.py) ───
const THEME_VARS = {bg: "--bg", bg2: "--bg2", bg3: "--bg3", line: "--line", fg: "--fg", dim: "--dim", faint: "--faint", accent: "--a", accent2: "--b", ok: "--ok", err: "--err", warn: "--warn", user: "--user", code_bg: "--code", sel: "--sel"};
const hex = v => /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(v || "") ? v : "transparent";  // a synced theme's colors go into style="": hex only
let themeList = [], curTheme = "";
function onAccent(hex) {  // black or white text, whichever reads better on the accent color
  const n = parseInt(hex.slice(1), 16), [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return .2126 * r + .7152 * g + .0722 * b > .35 ? "#0a0a0a" : "#ffffff";
}
function applyTheme(t) {
  if (!t) return;
  const r = document.documentElement;
  for (const [k, v] of Object.entries(THEME_VARS)) if (hex(t.colors[k]) !== "transparent") r.style.setProperty(v, t.colors[k]);
  r.style.colorScheme = t.dark === false ? "light" : "dark";
  r.dataset.theme = t.dark === false ? "light" : "dark";
  r.style.setProperty("--on-a", onAccent(t.colors.accent));
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t.colors.bg);
  try { localStorage.setItem("me_theme_cache", JSON.stringify(t)); } catch (e) {}
}
try { applyTheme(JSON.parse(localStorage.getItem("me_theme_cache"))); } catch (e) {}
async function loadThemes() {
  const r = await api(MODE === "local" ? "/api/agent/themes" : "/api/themes");
  themeList = r.themes; curTheme = r.current; applyTheme(themeList.find(t => t.name === r.current));
}
function pickTheme() {
  $("pbody").innerHTML = themeList.map(t => `<button onclick="setTheme('${esc(t.name)}')"${t.name === curTheme ? ' class="on"' : ""}><span class="sw" style="background:${hex(t.colors.bg)};border-color:${hex(t.colors.line)}"><i style="background:${hex(t.colors.accent)}"></i><i style="background:${hex(t.colors.accent2)}"></i></span>${esc(t.label || t.name)}<span class="d">${esc(t.dark === false ? window.t("light") : window.t("dark"))}</span></button>`).join("")
    + `<a class="more" href="${MODE === "cloud" ? "/account#market" : "#"}" onclick="${MODE === "local" ? "alert(t('Install more with /theme in the terminal or the marketplace on the site'));return false" : ""}">${esc(window.t("more themes in the marketplace →"))}</a>`;
  $("popup").classList.remove("hidden");
}
async function setTheme(name) {
  applyTheme(themeList.find(t => t.name === name)); curTheme = name; $("popup").classList.add("hidden");
  if (MODE === "local") return api("/api/agent/theme", {name});
  const d = await api("/api/doc?key=config"); const cfg = JSON.parse(d.content); cfg.theme = name;
  await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT");
}
