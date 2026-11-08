// ── SerenSoma: is it alive, and its log for whoever's helping ────────
const $ = id => document.getElementById(id);
function why(e) { return /^401/.test(e.message) ? "This page needs the body's token (🔑 Token, top right). It's the file 'token' in the state folder." : e.message; }
function ago(s) { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; }

async function loadAbout() {
  let a;
  try { a = await api("/api/about"); } catch (e) { $("facts").textContent = why(e); return; }
  const rows = [
    ["Version", a.version], ["Up", ago(a.up_seconds)], ["Robot", a.robot], ["Tells it to call", a.advertise],
    ["Voice channel", a.channel_open ? (a.speaking ? "open, speaking" : a.deaf ? "open, not listening" : "open, listening") : "closed"],
    ["Night", a.night_now ? "on" : "off"],
    ["Overwhelm", a.overwhelm && a.overwhelm.running ? `${Math.round(a.overwhelm.progress * 100)}%` : "nothing running"],
    ["State folder", a.state],
  ];
  $("facts").innerHTML = rows.map(([k, v]) => `<span>${escapeHtml(k)}</span><b>${escapeHtml(v)}</b>`).join("");
}

let LOG = [];
async function loadLogs() {
  try {
    const r = await api(`/api/logs?lines=${$("logLines").value}`);
    LOG = r.lines;
    $("logText").textContent = LOG.join("\n") || "(the log is empty)";
    const pre = $("logText"); pre.scrollTop = pre.scrollHeight;
  } catch (e) { $("logText").textContent = why(e); }
}

async function copyLogs() {
  if (!LOG.length) await loadLogs();
  const text = `SerenSoma log, ${new Date().toISOString()}\n` + LOG.join("\n");
  try { await navigator.clipboard.writeText(text); $("copyMsg").textContent = `Copied ${LOG.length} lines.`; }
  catch (e) {   // no clipboard without https on some browsers: select it so ctrl+c works
    const r = document.createRange(); r.selectNodeContents($("logText"));
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    $("copyMsg").textContent = "Selected: press Ctrl+C.";
  }
}

document.addEventListener("DOMContentLoaded", () => { loadAbout(); setInterval(loadAbout, 5000); });
