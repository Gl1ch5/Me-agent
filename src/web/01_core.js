const MODE = "__MODE__";
const $ = id => document.getElementById(id);
const side = $("side");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const human = n => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(1).replace(".0", "") + "k" : (n / 1e6).toFixed(1).replace(".0", "") + "M";
const money = x => !x ? "$0" : x < 0.01 ? "$" + x.toFixed(4) : x < 1 ? "$" + x.toFixed(3) : "$" + x.toFixed(2);
const dur = s => s < 60 ? s.toFixed(1) + "s" : Math.floor(s / 60) + "m" + String(Math.floor(s % 60)).padStart(2, "0") + "s";
const COLORS = {red:"#ff6b7a",green:"#5ad18a",blue:"#6ba6ff",magenta:"#d27cff",cyan:"#4fd1c5",yellow:"#e7c66b",orange:"#ffb454",purple:"#8b7cff",pink:"#ff8fc7"};
const I = {
  check: '<svg viewBox="0 0 24 24" style="color:var(--ok)"><path d="M5 12l5 5 9-11"/></svg>',
  x: '<svg viewBox="0 0 24 24" style="color:var(--err)"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  stop: '<path d="M7 7h10v10H7z" fill="currentColor"/>', up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  think: '<svg viewBox="0 0 24 24"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5V16h8v-2.5A6 6 0 0 0 12 3z"/></svg>',
};
var atts = [];  // pending attachments: {name, image, url | b64}
let token = localStorage.getItem("me_token") || "", base = "", after = 0, state = {}, busy = false, live = {}, cur = {}, polling = 0, turnT0 = 0;
