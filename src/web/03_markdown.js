// ─── markdown ───
function md(src) {
  const blocks = [];
  src = src.replace(/```(\w*)\n?([\s\S]*?)(```|$)/g, (_, lang, code) => { blocks.push([lang, code]); return `\u0000${blocks.length - 1}\u0000`; });
  let h = esc(src)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|\W)\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(?:^|\n)((?:[-*] .*(?:\n|$))+)/g, (_, l) => "\n<ul>" + l.trim().split("\n").map(x => "<li>" + x.slice(2) + "</li>").join("") + "</ul>\n");
  h = h.replace(/(?:^|\n)((?:\d+[.)] .*(?:\n|$))+)/g, (_, l) => "\n<ol>" + l.trim().split("\n").map(x => "<li>" + x.replace(/^\d+[.)] /, "") + "</li>").join("") + "</ol>\n");
  h = h.split(/\n{2,}/).map(p => /^\s*<(h\d|ul|ol|blockquote|\u0000)/.test(p) || p.startsWith("\u0000") ? p : "<p>" + p.replace(/\n/g, "<br>") + "</p>").join("");
  return h.replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><div class="bar"><span>${esc(blocks[i][0] || "code")}</span><button onclick="copyCode(this)">${tt("copy")}</button></div><code>${esc(blocks[i][1])}</code></pre>`);
}
