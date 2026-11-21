// ── SerenSoma: is it alive, and its log for whoever's helping ────────
const $ = id => document.getElementById(id);
function why(e) { return /^401/.test(e.message) ? "This page needs the body's token (🔑 Token, top right). It's the file 'token' in the state folder." : e.message; }
function ago(s) { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; }

function paintAbout(a) {
  const rows = [
    ["Version", a.version], ["Up", ago(a.up_seconds)], ["Robot", a.robot], ["Tells it to call", a.advertise],
    ["Voice channel", a.channel_open ? (a.speaking ? "open, speaking" : a.deaf ? "open, not listening" : "open, listening") : "closed"],
    ["Night", a.night_now ? "on" : "off"],
    ["Hand on the head", a.hand && a.hand.down ? `yes, ${((a.hand.to_ms - a.hand.from_ms) / 1000).toFixed(1)} s so far` : "no"],
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

document.addEventListener("DOMContentLoaded", () => { watch("/api/about", 5000, paintAbout, e => { $("facts").textContent = why(e); }); });

// ── Set up and Firmware: this box's own routes ──────────────────────
// Their own fetch: api() keeps only the status, and here the reason is the useful part
// ("this robot's firmware has no /api/config yet: ..."). This server answers a no in plain text.
function say(id, text, bad) { const m = $(id); m.textContent = text || ""; m.className = "msg tiny" + (text ? (bad ? " bad" : " ok") : ""); }

async function call(path, opts) {
  const headers = Object.assign({}, opts.headers || {}), tok = getToken();
  if (tok) headers.Authorization = "Bearer " + tok;
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (!r.ok) {
    const text = (await r.text()).trim();
    throw new Error(r.status === 401 ? "This page needs the body's token (🔑 Token, top right)." : text || `${r.status} ${r.statusText}`);
  }
  return r;
}
const post = (path, body) => call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function setupKeys() {
  const v = id => $(id).value.trim(), keys = {};
  if (v("suSsid")) keys.wifi = { ssid: v("suSsid"), password: $("suPass").value };
  keys.voice = v("suVoice") || "auto";
  if (v("suTsKey") || v("suTsName")) {
    keys.tailnet = {};
    if (v("suTsKey")) keys.tailnet.key = v("suTsKey");
    if (v("suTsName")) keys.tailnet.name = v("suTsName");
  }
  return keys;
}
function forgetSecrets() { $("suPass").value = ""; $("suTsKey").value = ""; }

async function setupSend() {
  const keys = setupKeys(), rotate = $("suRotate").checked;
  const what = [keys.wifi && `Wi-Fi to ${keys.wifi.ssid}`, `voice server ${keys.voice}`,
                keys.tailnet && "the tailnet", rotate && "a new token"].filter(Boolean).join(", ");
  if (!confirm(`Send ${what} to the robot? It saves them and reboots.` +
               (keys.wifi ? "\n\nA wrong Wi-Fi password takes it off the network until it gets a soma.yaml card." : ""))) return;
  say("suMsg", "Sending...");
  try {
    const r = await (await post("/robot/config", { ...keys, rotate_token: rotate })).json();
    forgetSecrets(); $("suRotate").checked = false;
    say("suMsg", `The robot saved: ${r.changed.join(", ") || "nothing new"}.` +
        (r.rebooting ? " It's rebooting; give it half a minute." : "") + (r.token_rotated ? " New token written down here." : ""));
  } catch (e) { say("suMsg", e.message, true); }
}

async function setupCard() {
  say("suMsg", "Making it...");
  try {
    const text = await (await post("/robot/soma.yaml", setupKeys())).text();
    const a = Object.assign(document.createElement("a"),
                            { href: URL.createObjectURL(new Blob([text], { type: "text/yaml" })), download: "soma.yaml" });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    forgetSecrets();
    say("suMsg", "Downloaded. Copy it to the root of the robot's microSD card and boot it; it reads it and deletes it. " +
                 "It holds the robot's token, so don't leave it lying around.");
  } catch (e) { say("suMsg", e.message, true); }
}

async function loadFirmware() {
  let f;
  try { f = await (await call("/firmware", {})).json(); } catch (e) { say("fwMsg", e.message, true); return; }
  $("fwFacts").innerHTML = [["The robot runs", f.robot || "?"], ["Armed", f.armed ? f.armed.version : "nothing"],
                            ["Releases from", f.repo || "?"]]
    .map(([k, v]) => `<span>${escapeHtml(k)}</span><b>${escapeHtml(v)}</b>`).join("");
  // Nothing falls off this list by itself. The newest few show; the rest wait under Previous versions.
  const staged = f.staged || [], armed = f.armed ? f.armed.version : null;
  const row = s => {
    const note = [s.version === f.robot ? "on the robot" : "", s.version === armed ? "armed" : ""].filter(Boolean).join(", ");
    return `<tr><td>${escapeHtml(s.version)}${note ? ` <span class="muted tiny">${note}</span>` : ""}</td>` +
      `<td>${escapeHtml(s.source)}</td><td>${Math.round(s.size / 1024)} KB</td>` +
      `<td class="num">${new Date(s.staged_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</td>` +
      `<td><button class="btn" data-fw="${escapeHtml(s.version)}">Install</button> ` +
      `<button class="btn" data-fwdrop="${escapeHtml(s.version)}"${s.version === armed ? " disabled" : ""}>Remove</button></td></tr>`;
  };
  $("fwRows").innerHTML = staged.slice(0, FW_SHOWN).map(row).join("") ||
    `<tr><td colspan="5" class="muted tiny">Nothing staged yet.</td></tr>`;
  const older = staged.slice(FW_SHOWN);
  $("fwOlder").hidden = !older.length;
  $("fwOlderCount").textContent = older.length;
  $("fwOldRows").innerHTML = older.map(row).join("");
  document.querySelectorAll("#firmware button[data-fw]").forEach(b => b.onclick = () => installFirmware(b.dataset.fw));
  document.querySelectorAll("#firmware button[data-fwdrop]").forEach(b => b.onclick = () => removeFirmware(b.dataset.fwdrop));
}
const FW_SHOWN = 3;

async function removeFirmware(version) {
  if (!confirm(`Remove ${version} from this box? The robot is not touched. A release can be fetched again; ` +
               "a build of your own is gone unless you still have the file.")) return;
  try {
    await call(`/firmware/staged/${encodeURIComponent(version)}`, { method: "DELETE" });
    say("fwMsg", `Removed ${version}.`); loadFirmware();
  } catch (e) { say("fwMsg", e.message, true); }
}

async function fetchFirmware() {
  say("fwMsg", "Fetching the latest release and checking it...");
  try {
    const s = await (await post("/firmware/fetch", { version: "latest" })).json();
    say("fwMsg", `Staged ${s.version} (${s.source}), checked.`); loadFirmware();
  } catch (e) { say("fwMsg", e.message, true); }
}

async function uploadFirmware() {
  const file = $("fwFile").files[0], version = $("fwVersion").value.trim();
  if (!file || !version) { say("fwMsg", "Pick the .bin and say its version.", true); return; }
  say("fwMsg", "Staging it...");
  try {
    const s = await (await call(`/firmware/upload?version=${encodeURIComponent(version)}`,
                                { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file })).json();
    say("fwMsg", `Staged ${s.version}.`); $("fwFile").value = ""; loadFirmware();
  } catch (e) { say("fwMsg", e.message, true); }
}

async function installFirmware(version) {
  if (!confirm(`Install firmware ${version}? The robot reboots, downloads it, and reboots again into it. ` +
               "Leave it plugged in until its face is back.")) return;
  try {
    const r = await (await post("/firmware/install", { version })).json();
    say("fwMsg", r.rebooting ? `${version} armed, and the robot is rebooting to take it. Give it a couple of minutes.`
                             : `${version} armed for the robot's next check-in (it didn't take the reboot: restart it by hand).`);
    loadFirmware();
  } catch (e) { say("fwMsg", e.message, true); }
}

// ── The dead-air timer: a live setting ──────────────────────────────
function secs(n) { return n >= 60 ? `${Math.floor(n / 60)}m${n % 60 ? " " + (n % 60) + "s" : ""}` : `${n}s`; }
async function loadDeadAir() {
  let s;
  try { s = await (await call("/settings", {})).json(); } catch (e) { say("deadAirMsg", e.message, true); return; }
  const v = s.channel.dead_air_seconds, el = $("deadAir");
  el.value = v; $("deadAirOut").textContent = secs(v);
  el.oninput = () => { $("deadAirOut").textContent = secs(Number(el.value)); };
  el.onchange = async () => {
    try {
      const r = await (await post("/settings", { channel: { dead_air_seconds: Number(el.value) } })).json();
      say("deadAirMsg", `Saved: ${secs(r.channel.dead_air_seconds)} of dead air hangs up.`);
    } catch (e) { say("deadAirMsg", e.message, true); }
  };
}

document.addEventListener("DOMContentLoaded", () => {
  loadDeadAir();
  $("suSend").onclick = setupSend; $("suCard").onclick = setupCard;
  $("fwFetch").onclick = fetchFirmware; $("fwUpload").onclick = uploadFirmware;
});

// ── The bench: the touch chip and the neck as they are ──────────────
// Everything goes through this box's /bench routes, which pass a short list of the robot's own
// routes through; the page never holds the robot's token. Loops run only while their tab is showing.
const getJson = async path => (await call(path, {})).json();
const sleep = ms => new Promise(r => setTimeout(r, ms));
// A diagnostics panel is showing only while it is the chosen one *and* Diagnostics is the open tab.
const showing = id => {
  const el = $(id), tab = el.closest(".view");
  return el.classList.contains("active") && (!tab || tab === el || tab.classList.contains("active"));
};
const hue = c => `hsl(${(c * 47) % 360} 75% 60%)`;     // one colour per channel, the same everywhere
const ZONE = ["back", "middle", "front"];
const facts = rows => rows.map(([k, v]) => `<span>${escapeHtml(k)}</span><b>${escapeHtml(v)}</b>`).join("");
function download(name, text, type) {
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// -- touch, live --
// Two sources, on purpose. The cells are the chip asked right now (/bench/touch-probe), about seven
// times a second. The strip and the chart are the robot's own record (/bench/touch-raw): every reading
// it took while something was touching, one each 50 ms, on its own clock. A quick tap falls between two
// of this page's questions, but the robot saw it, so it's in the record and it shows here.
const TB = { on: false, feed: [], since: 0, clock: null, last: null, flash: [null, null, null], stray: new Set(),
             recording: null, chart: { points: [], note: "", leds: Array(12).fill("") }, pin: null };
const STRIP_MS = 20000, SAMPLE_MS = 50, PADS = 3;
const getText = async path => (await call(path, {})).text();
const robotNow = () => TB.clock ? TB.clock.ms + (Date.now() - TB.clock.at) : 0;

function takeFeed(text) {
  // lines of "ms back middle front gesture"; gestures 10 and up are taps on the screen, not the pads
  const rows = text.split("\n").map(l => l.trim().split(/\s+/).map(Number))
    .filter(r => r.length === 5 && r.every(Number.isFinite) && r[4] < 10)
    .map(r => ({ ms: r[0], z: [r[1], r[2], r[3]], g: r[4] }));
  if (!rows.length) return;
  TB.since = rows[rows.length - 1].ms;
  TB.feed.push(...rows);
  if (TB.recording) TB.recording.rows.push(...rows);
  const until = Date.now() + 700;      // a tap shorter than one look still lights its cell for a moment
  rows.forEach(r => r.z.forEach((v, i) => { if (v && (!TB.flash[i] || v >= TB.flash[i].v)) TB.flash[i] = { v, until }; }));
}

function lastTouch() {
  // the most recent unbroken touch in the record: how long it was and which pads it crossed
  const rows = TB.feed.filter(r => r.z.some(Boolean));
  if (!rows.length) return null;
  let start = rows.length - 1;
  while (start > 0 && rows[start].ms - rows[start - 1].ms <= 120) start--;
  const run = rows.slice(start), pads = ZONE.filter((_, i) => run.some(r => r.z[i]));
  return { ms: run[run.length - 1].ms - run[0].ms + SAMPLE_MS, pads, ago: robotNow() - run[run.length - 1].ms };
}

function paintCells(p) {
  const now = Date.now();
  $("tbCells").innerHTML = ZONE.map((name, i) => {
    const f = TB.flash[i] && TB.flash[i].until > now ? TB.flash[i].v : 0, v = Math.max(p.channels[i], f);
    return `<div class="cell used" data-v="${v}"><b>${p.channels[i]}</b>${name}<br>channel ${i + 1}</div>`;
  }).join("");
  p.channels.slice(PADS).forEach((v, i) => { if (v) TB.stray.add(i + PADS + 1); });
  const [b, m, f] = p.channels, total = b + m + f, t = lastTouch();
  const hex = n => "0x" + n.toString(16).padStart(2, "0");
  $("tbFacts").innerHTML = facts([
    ["Position", total ? Math.round((f - b) * 100 / total) + " (-100 back, +100 front)" : "nothing touching"],
    ["Last touch", t ? `${t.ms} ms on ${t.pads.join(", ")}, ${(Math.max(0, t.ago) / 1000).toFixed(1)} s ago` : "none in the last 20 s"],
    ["Channels 4 to 12", TB.stray.size ? `channel ${[...TB.stray].join(", ")} read something, which the board's drawing says can't happen`
                                       : "no pad on them: grounded or unconnected on the board"],
    ["Sensitivity asked for", `${p.sensitivity.type}, level ${p.sensitivity.level}`],
    ["Sensitivity registers", p.sensitivity.registers.map(hex).join(" ")],
    ["Control 1, 2", `${hex(p.ctrl1)} ${hex(p.ctrl2)}`],
  ]);
}

function paintStrip() {
  const cv = $("tbStrip"), g = cv.getContext("2d"), now = robotNow(), rowH = cv.height / PADS, left = 56;
  const css = getComputedStyle(document.documentElement);
  g.clearRect(0, 0, cv.width, cv.height);
  g.font = "11px " + css.getPropertyValue("--mono"); g.textBaseline = "middle";
  g.fillStyle = css.getPropertyValue("--text-dim");
  ZONE.forEach((name, i) => g.fillText(name, 4, i * rowH + rowH / 2));
  const x = ms => left + (cv.width - left) * (1 - (now - ms) / STRIP_MS), w = Math.max(2, (cv.width - left) * SAMPLE_MS / STRIP_MS);
  TB.feed.forEach(r => {
    r.z.forEach((v, i) => {
      if (!v) return;
      g.globalAlpha = v / 3; g.fillStyle = hue(i);
      g.fillRect(x(r.ms) - w, i * rowH + 2, w, rowH - 4);
    });
    if (r.g === 3 || r.g === 4) {      // the robot read a swipe here: toward the front, or toward the back
      g.globalAlpha = 1; g.fillStyle = css.getPropertyValue("--text");
      g.fillText(r.g === 3 ? "↓" : "↑", x(r.ms) - 3, 7);
    }
  });
  g.globalAlpha = 1;
}

async function touchLoop() {
  let count = 0, since = Date.now();
  while (TB.on && showing("touch")) {
    const started = Date.now();
    try {
      // the first look only sets the clock, so the record can be asked for the last 20 s and no more
      const [p, raw] = await Promise.all([getJson("/bench/touch-probe"),
                                          TB.clock ? getText(`/bench/touch-raw?since=${TB.since}`) : "", screenRound()]);
      if (!TB.clock || p.uptimeMs < TB.clock.ms) {      // first look, or the robot rebooted and its clock with it
        TB.feed = []; TB.since = Math.max(0, p.uptimeMs - STRIP_MS);
      } else takeFeed(raw);
      TB.clock = { ms: p.uptimeMs, at: Date.now() };
      TB.last = p; count++;
      TB.feed = TB.feed.filter(r => r.ms > p.uptimeMs - STRIP_MS);
      if (TB.recording) TB.recording.looks++;
      paintCells(p); paintStrip();
      if (Date.now() - since > 2000) {
        $("tbRate").textContent = `asking ${(count * 1000 / (Date.now() - since)).toFixed(1)} times a second; the strip is the robot's own 20 a second`;
        count = 0; since = Date.now();
      }
      say("tbMsg", "");
    } catch (e) { say("tbMsg", e.message, true); await sleep(1500); }
    await sleep(Math.max(0, 150 - (Date.now() - started)));
  }
  TB.on = false; TB.clock = null; $("tbWatch").textContent = "Start watching"; $("tbRate").textContent = "";
}
function touchWatch(on) {
  if (on === TB.on) return;
  TB.on = on;
  if (on) { TB.stray = new Set(); $("tbWatch").textContent = "Stop watching"; touchLoop(); }
}

async function touchTune() {
  try {
    const p = await (await post("/bench/touch-probe", { type: $("tbType").value, level: Number($("tbLevel").value) })).json();
    paintCells(p); TB.last = p;
    say("tbMsg", `Running at ${p.sensitivity.type}, level ${p.sensitivity.level} until the next boot.`);
  } catch (e) { say("tbMsg", e.message, true); }
}

// -- touch, the chart --
function paintMap() {
  const dot = (p, i) => {
    const on = p.share.map((s, c) => [c, s]).filter(([, s]) => s > 0).sort((a, b) => b[1] - a[1]);
    const fill = on.length ? hue(on[0][0]) : "none";
    return `<g><circle cx="${p.x * 300}" cy="${p.y * 300}" r="9" fill="${fill}" fill-opacity="${on.length ? 0.35 + 0.65 * on[0][1] : 0}"` +
      ` stroke="${on.length ? fill : "var(--text-dim)"}" stroke-dasharray="${on.length ? "" : "2 2"}"/>` +
      `<text x="${p.x * 300}" y="${p.y * 300 + 3}" text-anchor="middle" font-size="9" fill="var(--text)">${on.map(([c]) => c + 1).join(",") || "–"}</text>` +
      `<title>${escapeHtml(p.label || "spot " + (i + 1))}</title></g>`;
  };
  $("tbMap").innerHTML =
    `<rect x="30" y="40" width="240" height="210" rx="34" fill="none" stroke="var(--border)" stroke-width="2"/>` +
    `<text x="150" y="24" text-anchor="middle" font-size="11" fill="var(--text-dim)">back</text>` +
    `<text x="150" y="282" text-anchor="middle" font-size="11" fill="var(--text-dim)">front (the face, toward you)</text>` +
    TB.chart.points.map(dot).join("") +
    (TB.pin ? `<circle cx="${TB.pin.x * 300}" cy="${TB.pin.y * 300}" r="11" fill="none" stroke="var(--accent)" stroke-width="2"/>` +
              `<circle cx="${TB.pin.x * 300}" cy="${TB.pin.y * 300}" r="2" fill="var(--accent)"/>` : "");
}

function paintChart(channels) {
  const names = Object.keys(channels || {});
  $("tbChannels").innerHTML = names.length ? names.map(n => {
    const c = channels[n];
    const where = `${c.y < 0.4 ? "back" : c.y > 0.6 ? "front" : "middle"}${c.x < 0.4 ? ", left" : c.x > 0.6 ? ", right" : ""}`;
    return `<span style="color:${hue(n - 1)}">${ZONE[n - 1] || "channel " + n}</span><b>${c.points} spot${c.points === 1 ? "" : "s"}, centred ${where} of the drawing, up to ${c.peak}</b>`;
  }).join("") : `<span class="muted">Nothing charted yet.</span><b></b>`;
  $("tbPoints").innerHTML = TB.chart.points.map((p, i) => {
    const on = p.share.map((s, c) => s > 0 ? `<span style="color:${hue(c)}">${ZONE[c] || c + 1}</span> ${Math.round(s * 100)}%` : "").filter(Boolean).join(", ");
    return `<tr><td>${escapeHtml(p.label || "spot " + (i + 1))}</td><td>${on || "<span class='muted'>none answered</span>"}</td>` +
      `<td class="tiny muted">${p.sensitivity ? p.sensitivity.type + " " + p.sensitivity.level : ""}</td>` +
      `<td><button class="btn" data-drop="${i}">Remove</button></td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted tiny">No spots yet.</td></tr>`;
  $("tbPoints").querySelectorAll("button[data-drop]").forEach(b => b.onclick = () => {
    TB.chart.points.splice(Number(b.dataset.drop), 1); saveChart();
  });
  paintMap();
}

async function loadChart() {
  try { const m = await getJson("/bench/map"); TB.chart = { points: m.points, note: m.note, leds: m.leds }; paintChart(m.channels); paintLeds(); }
  catch (e) { say("tbMapMsg", e.message, true); paintChart({}); }
}
async function saveChart() {
  try {
    const m = await (await call("/bench/map", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(TB.chart) })).json();
    TB.chart = { points: m.points, note: m.note, leds: m.leds }; paintChart(m.channels);
  } catch (e) { say("tbMapMsg", e.message, true); }
}

async function recordSpot() {
  if (!TB.pin) { say("tbMapMsg", "Click the drawing first, where your finger is going to be.", true); return; }
  if (TB.recording) return;
  touchWatch(true);
  $("tbRecord").disabled = true;
  // the window starts on the robot's own clock, as of its last answer: this page's clock is only a guess at it
  for (let waited = 0; !TB.clock && waited < 3000; waited += 100) await sleep(100);
  if (!TB.clock) { $("tbRecord").disabled = false; say("tbMapMsg", "The robot isn't answering. Is it on, with bench firmware?", true); return; }
  const ms = Number($("tbSecs").value);
  TB.recording = { rows: [], looks: 0, from: TB.clock.ms };
  for (let left = ms; left > 0; left -= 250) {
    say("tbMapMsg", `Recording: keep your finger there, ${(left / 1000).toFixed(1)} s...`); await sleep(250);
  }
  await sleep(350);      // one more look, so the last readings of the window have arrived
  const rec = TB.recording; TB.recording = null; $("tbRecord").disabled = false;
  if (rec.looks < 2) { say("tbMapMsg", "The robot didn't answer often enough to record that. Is it on, with bench firmware?", true); return; }
  // the robot's own readings, one each 50 ms while touched: a pad's share is how much of the window it was on
  const rows = rec.rows.filter(r => r.ms >= rec.from), expected = Math.round(ms / SAMPLE_MS);
  const on = c => c < PADS ? rows.filter(r => r.z[c] > 0) : [];
  const share = Array.from({ length: 12 }, (_, c) => Math.min(1, on(c).length / expected));
  const peak = Array.from({ length: 12 }, (_, c) => Math.max(0, ...on(c).map(r => r.z[c])));
  const point = { x: TB.pin.x, y: TB.pin.y, label: $("tbLabel").value.trim(), share, peak, samples: expected, at: Date.now() / 1000 };
  if (TB.last) point.sensitivity = { type: TB.last.sensitivity.type, level: TB.last.sensitivity.level };
  TB.chart.points.push(point);
  const pads = ZONE.filter((_, c) => share[c] > 0);
  say("tbMapMsg", pads.length ? `Kept: the ${pads.join(" and ")} pad${pads.length > 1 ? "s" : ""} answered there.` : "Kept: nothing answered there.");
  TB.pin = null; $("tbLabel").value = "";
  await saveChart();
}

// -- servos, live --
const SV = { on: false, last: null, sweep: null, running: false };
const AXES = ["yaw", "pitch"];

function paintServos(s) {
  const n = (v, d = 0) => v == null ? "–" : Number(v).toFixed(d);
  $("svRows").innerHTML = AXES.map(a => {
    const v = s[a];
    if (!v.ok) return `<tr><td>${a}</td><td class="num">${n(v.angle, 1)}°</td><td colspan="11" class="muted">the servo didn't answer</td></tr>`;
    const r = reach(v);
    return `<tr><td>${a}</td><td class="num">${n(v.angle, 1)}°</td><td class="num">${n(v.rawAngle, 1)}°</td><td class="num">${v.raw}</td>` +
      `<td class="num">${v.zero}</td><td class="num">${n(v.angleMin)}° to ${n(v.angleMax)}°<br><span class="muted">${v.rawMin} to ${v.rawMax}</span></td>` +
      `<td class="num"${r.short ? ' style="color:var(--danger)"' : ""}>${n(r.low, 1)}° to ${n(r.high, 1)}°<br><span class="muted">${r.why}</span></td>` +
      `<td class="num">${v.load}</td><td class="num">${v.current}</td><td class="num">${n(v.voltage, 1)}</td><td class="num">${v.temperature}</td>` +
      `<td>${v.torque ? "on" : "off"}</td><td>${v.moving ? "yes" : "no"}</td></tr>`;
  }).join("");
  $("svLock").checked = s.locked;
  $("svHold").checked = !s.yaw.autoRelease;
}

// How far a servo can really be sent each way from its zero. Three things hold it: the firmware's
// angle limits, the raw limits in force (0 to 1000 steps, less after a stall), and the limits the servo
// keeps in its own memory. Whichever is nearest wins, and `why` names it for the upper end.
function reach(v) {
  const deg = raw => (raw - v.zero) * 5 / 16, has = v.servoMin >= 0 && v.servoMax > v.servoMin;
  const lows = [[v.angleMin, "firmware limit"], [deg(v.rawMin), "step range"]], highs = [[v.angleMax, "firmware limit"], [deg(v.rawMax), "step range"]];
  if (has) { lows.push([deg(v.servoMin), "servo's own limit"]); highs.push([deg(v.servoMax), "servo's own limit"]); }
  const low = lows.reduce((a, b) => b[0] > a[0] ? b : a), high = highs.reduce((a, b) => b[0] < a[0] ? b : a);
  const short = high[0] < v.angleMax - 0.5 || low[0] > v.angleMin + 0.5;
  return { low: low[0], high: high[0], short, why: short ? `held by the ${high[0] < v.angleMax - 0.5 ? high[1] : low[1]}` : "the full range" };
}

async function setZero(servo, zero) {
  const what = zero === "here" ? `Make where the ${servo} servo is right now its 0°? The robot keeps this.`
                               : `Put the ${servo} servo's 0° back to the build's default? The robot keeps this.`;
  if (!confirm(what)) return;
  try {
    SV.last = await (await post("/bench/servo", { servo, zero })).json(); paintServos(SV.last);
    say("svZeroMsg", `${servo}: 0° is now step ${SV.last[servo].zero}.`);
  } catch (e) { say("svZeroMsg", e.message, true); }
}

async function servoLoop() {
  while (SV.on && showing("servos")) {
    const started = Date.now();
    if (!SV.running) {    // a sweep reads the servos itself; two readers would only slow it
      try { SV.last = await getJson("/bench/servo"); paintServos(SV.last); say("svMsg", ""); }
      catch (e) { say("svMsg", e.message, true); await sleep(1500); }
    }
    await sleep(Math.max(0, 250 - (Date.now() - started)));
  }
  SV.on = false; $("svWatch").textContent = "Start watching";
}
function servoWatch(on) {
  if (on === SV.on) return;
  SV.on = on;
  if (on) { $("svWatch").textContent = "Stop watching"; servoLoop(); }
}

async function servoSet(change) {
  try { SV.last = await (await post("/bench/servo", change)).json(); paintServos(SV.last); say("svMsg", ""); return true; }
  catch (e) { say("svMsg", e.message, true); return false; }
}
async function headTo(body) {
  try { await post("/bench/head", body); say("svMsg", ""); return true; }
  catch (e) { say("svMsg", e.message, true); return false; }
}
function sliderBody() { return { yaw: Number($("svYaw").value), pitch: Number($("svPitch").value), speed: Number($("svSpeed").value) }; }
function paintSliders() {
  $("svYawOut").textContent = $("svYaw").value + "°"; $("svPitchOut").textContent = $("svPitch").value + "°";
  $("svSpeedOut").textContent = $("svSpeed").value;
}

// -- servos, the sweep --
async function settle(axis, timeout) {
  // wait for the servo to say it has stopped, twice running, or give up at the timeout
  const end = Date.now() + timeout; let still = 0, v = null;
  await sleep(250);
  while (Date.now() < end) {
    v = (await getJson("/bench/servo"))[axis];
    if (!v.ok) throw new Error(`the ${axis} servo didn't answer`);
    still = v.moving ? 0 : still + 1;
    if (still >= 2) break;
    await sleep(120);
  }
  return v;
}

function sweepTargets(from, to, step) {
  // out from `from` to `to`, then back the same way: the same angle reached from both sides
  const out = [];
  for (let a = from; from < to ? a <= to : a >= to; a += from < to ? step : -step) out.push(a);
  if (out[out.length - 1] !== to) out.push(to);
  return { out: out.length, all: out.concat(out.slice(0, -1).reverse()) };
}

async function runSweep() {
  if (SV.running) { SV.running = false; return; }    // the button is Stop while it runs
  const axis = $("swAxis").value, lim = axis === "yaw" ? [-128, 128] : [0, 90];
  const held = v => Math.max(lim[0], Math.min(lim[1], v));
  const from = held(Number($("swFrom").value)), to = held(Number($("swTo").value));
  const step = Math.max(1, Math.abs(Number($("swStep").value) || 15)), speed = Number($("swSpeed").value) || 300;
  if (from === to) { say("swMsg", "From and to are the same angle.", true); return; }
  const targets = sweepTargets(from, to, step);
  SV.running = true; SV.sweep = { axis, rows: [] }; $("swRun").textContent = "Stop";
  try {
    if (!await servoSet({ lock: true, hold: true })) throw new Error($("svMsg").textContent);
    for (let i = 0; i < targets.all.length && SV.running; i++) {
      say("swMsg", `${axis} to ${targets.all[i]}° (${i + 1} of ${targets.all.length})...`);
      if (!await headTo({ [axis]: targets.all[i], speed })) throw new Error($("svMsg").textContent);
      const v = await settle(axis, 4000);
      SV.sweep.rows.push({ target: targets.all[i], back: i >= targets.out, angle: v.rawAngle, raw: v.raw, load: v.load,
                           current: v.current, moving: v.moving });
      paintSweep();
    }
    say("swMsg", SV.running ? "Done." : "Stopped.");
  } catch (e) { say("swMsg", e.message, true); }
  SV.running = false; $("swRun").textContent = "Run";
  await servoSet({ hold: false, lock: $("svLock").checked });
}

function paintSweep() {
  const cv = $("swChart"), g = cv.getContext("2d"), rows = SV.sweep ? SV.sweep.rows : [];
  const css = getComputedStyle(document.documentElement), dim = css.getPropertyValue("--text-dim");
  g.clearRect(0, 0, cv.width, cv.height);
  if (!rows.length) { $("swFacts").innerHTML = ""; return; }
  const ts = rows.map(r => r.target), lo = Math.min(...ts), hi = Math.max(...ts);
  const errs = rows.map(r => r.angle - r.target), maxErr = Math.max(2, ...errs.map(Math.abs));
  const maxLoad = Math.max(100, ...rows.map(r => Math.abs(r.load)));
  const x = t => 40 + (cv.width - 60) * (hi === lo ? 0.5 : (t - lo) / (hi - lo)), mid = cv.height / 2, half = mid - 22;
  g.strokeStyle = dim; g.lineWidth = 1; g.beginPath(); g.moveTo(40, mid); g.lineTo(cv.width - 20, mid); g.stroke();
  g.fillStyle = dim; g.font = "10px " + css.getPropertyValue("--mono");
  g.fillText(`+${maxErr.toFixed(1)}°`, 2, mid - half + 4); g.fillText(`-${maxErr.toFixed(1)}°`, 2, mid + half + 4);
  g.fillText(`${lo}°`, 40, cv.height - 4); g.fillText(`${hi}°`, cv.width - 44, cv.height - 4);
  rows.forEach((r, i) => {
    g.fillStyle = r.back ? hue(4) : hue(0); g.globalAlpha = 0.8;
    g.fillRect(x(r.target) + (r.back ? 1 : -7), mid, 6, -errs[i] / maxErr * half);
  });
  g.globalAlpha = 1; g.strokeStyle = css.getPropertyValue("--ok"); g.beginPath();
  rows.forEach((r, i) => {
    const y = mid + half - Math.abs(r.load) / maxLoad * 2 * half;
    if (i) g.lineTo(x(r.target), y); else g.moveTo(x(r.target), y);
  });
  g.stroke();
  const worst = rows.reduce((a, r) => Math.abs(r.angle - r.target) > Math.abs(a.angle - a.target) ? r : a);
  const heavy = rows.reduce((a, r) => Math.abs(r.load) > Math.abs(a.load) ? r : a);
  $("swFacts").innerHTML = facts([
    ["Steps", `${rows.length} on ${SV.sweep.axis}`],
    ["Furthest off", `${(worst.angle - worst.target).toFixed(1)}° at ${worst.target}°${worst.back ? " (coming back)" : ""}`],
    ["Heaviest load", `${heavy.load} at ${heavy.target}°${heavy.back ? " (coming back)" : ""}`],
    ["Still moving when read", String(rows.filter(r => r.moving).length)],
  ]);
}

function sweepCsv() {
  if (!SV.sweep || !SV.sweep.rows.length) { say("swMsg", "Run a sweep first.", true); return; }
  download(`sweep-${SV.sweep.axis}.csv`, "axis,target,direction,angle,raw,load,current,moving\n" + SV.sweep.rows.map(r =>
    [SV.sweep.axis, r.target, r.back ? "back" : "out", r.angle.toFixed(2), r.raw, r.load, r.current, r.moving ? 1 : 0].join(",")).join("\n") + "\n", "text/csv");
}

// A tab was opened: its chart, if it has one. The live loops wait for their button, so opening the
// page never starts calling the robot by itself.
function benchOpen() {
  if (showing("touch")) loadChart();
  if (showing("lights")) { loadChart(); readLeds(); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("tbLevel").innerHTML = [0, 1, 2, 3, 4, 5, 6, 7].map(n => `<option${n === 3 ? " selected" : ""}>${n}</option>`).join("");
  $("tbCells").innerHTML = ZONE.map((name, i) => `<div class="cell used"><b>–</b>${name}<br>channel ${i + 1}</div>`).join("");
  $("tbWatch").onclick = () => touchWatch(!TB.on);
  $("tbTune").onclick = touchTune; $("tbRecord").onclick = recordSpot;
  $("tbExport").onclick = () => download("touch_map.json", JSON.stringify(TB.chart, null, 1), "application/json");
  $("tbClear").onclick = () => {
    if (TB.chart.points.length && confirm(`Clear all ${TB.chart.points.length} charted spots?`)) { TB.chart.points = []; saveChart(); }
  };
  $("tbMap").addEventListener("pointerdown", e => {
    const r = $("tbMap").getBoundingClientRect(), unit = v => Math.max(0, Math.min(1, v));
    TB.pin = { x: unit((e.clientX - r.left) / r.width), y: unit((e.clientY - r.top) / r.height) };
    paintMap();
  });
  paintMap();

  $("svWatch").onclick = () => servoWatch(!SV.on);
  $("svLock").onchange = () => servoSet({ lock: $("svLock").checked });
  $("svHold").onchange = () => servoSet({ hold: $("svHold").checked });
  $("svRelease").onclick = () => servoSet({ hold: false, torque: false });
  $("svZeroYaw").onclick = () => setZero("yaw", "here"); $("svZeroPitch").onclick = () => setZero("pitch", "here");
  $("svZeroYawDef").onclick = () => setZero("yaw", "default"); $("svZeroPitchDef").onclick = () => setZero("pitch", "default");
  ["svYaw", "svPitch", "svSpeed"].forEach(id => $(id).oninput = () => {
    paintSliders(); if ($("svFollow").checked && id !== "svSpeed") headTo(sliderBody());
  });
  $("svGo").onclick = () => headTo(sliderBody());
  $("svCentre").onclick = () => { $("svYaw").value = 0; $("svPitch").value = 20; paintSliders(); headTo(sliderBody()); };
  $("swAxis").onchange = () => { const yaw = $("swAxis").value === "yaw"; $("swFrom").value = yaw ? -90 : 5; $("swTo").value = yaw ? 90 : 85; };
  $("swRun").onclick = runSweep; $("swCsv").onclick = sweepCsv;
  paintSliders();
});

// -- the screen, live: runs inside the touch loop --
const SC = { trail: [], none: false };

function paintScreen(s) {
  const w = s.width || 320, h = s.height || 240, now = Date.now();
  if (s.down) SC.trail.push({ t: now, x: s.x, y: s.y });
  SC.trail = SC.trail.filter(p => now - p.t < 3000);
  const trail = SC.trail.map(p => `<circle cx="${p.x}" cy="${p.y}" r="3" fill="var(--accent)" fill-opacity="${(1 - (now - p.t) / 3000).toFixed(2)}"/>`).join("");
  const kept = s.touches.map((t, i) => {
    const o = (0.25 + 0.75 * (i + 1) / s.touches.length).toFixed(2);
    return `<g opacity="${o}"><line x1="${t.x}" y1="${t.y}" x2="${t.x1}" y2="${t.y1}" stroke="${hue(3)}" stroke-width="1.5"/>` +
      `<circle cx="${t.x}" cy="${t.y}" r="4" fill="${hue(3)}"/><circle cx="${t.x1}" cy="${t.y1}" r="2" fill="none" stroke="${hue(3)}"/></g>`;
  }).join("");
  const ring = s.down ? `<circle cx="${s.x}" cy="${s.y}" r="14" fill="none" stroke="var(--accent)" stroke-width="2"/>` +
    `<line x1="${s.x - 20}" y1="${s.y}" x2="${s.x + 20}" y2="${s.y}" stroke="var(--accent)"/><line x1="${s.x}" y1="${s.y - 20}" x2="${s.x}" y2="${s.y + 20}" stroke="var(--accent)"/>` : "";
  $("scMap").setAttribute("viewBox", `0 0 ${w} ${h}`);
  $("scMap").innerHTML = `<line x1="${w / 2}" y1="0" x2="${w / 2}" y2="${h}" stroke="#333"/><line x1="0" y1="${h / 2}" x2="${w}" y2="${h / 2}" stroke="#333"/>` + kept + trail + ring;
  $("scFacts").innerHTML = facts([
    ["Finger", s.down ? `down at ${s.x}, ${s.y}` : "up"],
    ["Screen", `${w} by ${h}`],
  ]);
  $("scRows").innerHTML = s.touches.slice().reverse().map(t =>
    `<tr><td>${escapeHtml(t.gesture)}</td><td class="num">${t.x}, ${t.y}</td><td class="num">${t.x1}, ${t.y1}</td>` +
    `<td class="num">${Math.max(0, (s.uptimeMs - t.ms) / 1000).toFixed(0)} s</td></tr>`).join("") ||
    `<tr><td colspan="4" class="muted tiny">No touches on the face since it booted.</td></tr>`;
}

async function screenRound() {
  if (SC.none) return;
  try { paintScreen(await getJson("/bench/screen-touch")); }
  catch (e) {
    // firmware from before the screen feed: say so once and stop asking, the pads still work
    SC.none = /no \/api\/screen-touch/.test(e.message);
    $("scFacts").innerHTML = facts([["Screen", e.message]]);
  }
}

// -- the lights --
const LD = { shown: Array(12).fill("000000"), walking: false };
const KEPT = [0, 1, 10, 11];  // the robot's own, the front two on each side: microphone, reply and camera lights

function ledColor() {
  const v = $("ldColor").value, part = i => Math.min(168, parseInt(v.slice(i, i + 2), 16));
  return [part(1), part(3), part(5)];
}

function paintLeds() {
  const labels = TB.chart.leds || Array(12).fill("");
  $("ldCells").innerHTML = LD.shown.map((hex, i) =>
    `<div class="led${KEPT.includes(i) ? " kept" : ""}" data-led="${i}"><i style="background:#${hex}"></i>${i}` +
    `<input data-label="${i}" maxlength="60" placeholder="where it sits" value="${escapeHtml(labels[i] || "")}"></div>`).join("");
  $("ldCells").querySelectorAll(".led").forEach(c => c.onclick = e => {
    if (e.target.tagName !== "INPUT") lightOnly(Number(c.dataset.led));
  });
  $("ldCells").querySelectorAll("input[data-label]").forEach(inp => inp.onchange = () => {
    const leds = (TB.chart.leds || Array(12).fill("")).slice();
    leds[Number(inp.dataset.label)] = inp.value.trim();
    TB.chart.leds = leds; saveChart();
  });
}

async function sendFrame(frame) {
  try {
    await post("/bench/leds", { frame });
    LD.shown = frame.map(c => c.map(v => v.toString(16).padStart(2, "0")).join(""));
    // only the swatches: redrawing the cells would take a label being typed with it
    $("ldCells").querySelectorAll(".led i").forEach((sw, i) => { sw.style.background = "#" + LD.shown[i]; });
    return true;
  } catch (e) { say("ldMsg", e.message, true); return false; }
}
const frameOf = lit => Array.from({ length: 12 }, (_, i) => lit(i) ? ledColor() : [0, 0, 0]);

async function lightOnly(i) {
  if (await sendFrame(frameOf(n => n === i))) say("ldMsg", `Only LED ${i} is lit.`);
}

async function walkLeds() {
  if (LD.walking) { LD.walking = false; return; }
  LD.walking = true; $("ldWalk").textContent = "Stop";
  for (let i = 0; i < 12 && LD.walking && showing("lights"); i++) {
    if (!await sendFrame(frameOf(n => n === i))) break;
    say("ldMsg", `LED ${i}`);
    await sleep(1000);
  }
  if (LD.walking) { await sendFrame(frameOf(() => false)); say("ldMsg", "Walked all twelve."); }
  LD.walking = false; $("ldWalk").textContent = "Walk them";
}

async function readLeds() {
  try {
    const s = await getJson("/bench/status");
    LD.shown = Array.from({ length: 12 }, (_, i) => (s.leds || "").slice(i * 6, i * 6 + 6) || "000000");
    paintLeds(); say("ldMsg", "");
  } catch (e) { say("ldMsg", e.message, true); }
}

document.addEventListener("DOMContentLoaded", () => {
  paintScreen({ down: false, touches: [], uptimeMs: 0 });
  paintLeds();
  $("ldWalk").onclick = walkLeds; $("ldRead").onclick = readLeds;
  $("ldAll").onclick = async () => { if (await sendFrame(frameOf(() => true))) say("ldMsg", "All twelve lit."); };
  $("ldOff").onclick = async () => { if (await sendFrame(frameOf(() => false))) say("ldMsg", "All off."); };
});

// ── Voice: where the time in a turn goes ────────────────────────────
const LEGS = [["wait_for_quiet_ms", "waiting for quiet"], ["transcribe_ms", "transcribing"], ["mind_ms", "the mind"],
              ["render_first_ms", "rendering the first sentence"], ["open_channel_ms", "opening the channel"]];
const dur = ms => ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : ms + " ms";
const median = a => { const s = a.slice().sort((x, y) => x - y); return s.length ? s[Math.floor((s.length - 1) / 2)] : null; };

function paintVoice(v) {
  const bySeq = Object.fromEntries(v.turns.map(t => [t.seq, t]));
  // a reply and the words it answers are one bar: the heard turn's legs, then the said turn's
  const whole = t => ({ ...(bySeq[t.answers] || {}), ...t });
  const replies = v.turns.filter(t => t.kind === "said" && t.answers).map(whole);
  const mid = k => median(replies.map(t => t[k]).filter(x => x != null));
  $("vcFacts").innerHTML = replies.length ? facts([
    ["Replies timed", String(replies.length)],
    ["Your last word to my first sound, typically", dur(mid("last_word_to_first_sound_ms"))],
    ...LEGS.map(([k, name]) => [`of that, ${name}`, mid(k) == null ? "–" : dur(mid(k))]),
  ]) : `<span class="muted">No reply timed yet.</span><b>${v.turns.length ? "Things were heard and said, but nothing was said in answer to something heard." : "Say something to the robot and have it answer."}</b>`;
  $("vcLogSummary").textContent = `The turns, one by one (${v.turns.length})`;
  $("vcKey").innerHTML = LEGS.map(([, name], i) => `<span><i style="background:${hue(i)}"></i>${name}</span>`).join("");
  const bar = t => {
    const parts = LEGS.map(([k], i) => [t[k] || 0, i]).filter(([ms]) => ms > 0), total = parts.reduce((a, [ms]) => a + ms, 0) || 1;
    return `<div class="legs" title="${LEGS.filter(([k]) => t[k]).map(([k, name]) => `${name} ${dur(t[k])}`).join(", ")}">` +
      parts.map(([ms, i]) => `<i style="width:${(ms * 100 / total).toFixed(1)}%;background:${hue(i)}"></i>`).join("") + `</div>`;
  };
  $("vcRows").innerHTML = v.turns.slice().reverse().map(t => {
    const when = new Date(t.t * 1000).toLocaleTimeString();
    if (t.kind === "heard") {
      const what = t.words ? `heard ${t.words} word${t.words === 1 ? "" : "s"} (${t.audio_s} s)` : `a sound with no words (${t.audio_s} s)`;
      return `<tr><td class="tiny muted">${when}</td><td>${what}</td><td>${bar(t)}<span class="tiny muted">` +
        `quiet ${dur(t.wait_for_quiet_ms)}, transcribing ${dur(t.transcribe_ms)}</span></td><td></td></tr>`;
    }
    const w = t.answers ? whole(t) : t;
    const what = `${t.clip ? "played a clip" : `said ${t.pieces} sentence${t.pieces === 1 ? "" : "s"}`} (${t.audio_s} s)${t.woke ? ", woke the robot for it" : ""}`;
    const legs = `${t.answers ? `the mind ${dur(t.mind_ms)}, ` : ""}rendering ${dur(t.render_first_ms)}, opening ${dur(t.open_channel_ms)}`;
    return `<tr><td class="tiny muted">${when}</td><td>${what}</td><td>${bar(w)}<span class="tiny muted">${legs}</span></td>` +
      `<td class="num">${t.answers ? dur(t.last_word_to_first_sound_ms) : `<span class="muted tiny">not a reply</span>`}</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted tiny">Nothing heard or said since this server started.</td></tr>`;
}

async function loadQuiet() {
  let s;
  try { s = await (await call("/settings", {})).json(); } catch (e) { say("vcMsg", e.message, true); return; }
  const el = $("vcQuiet");
  el.value = s.channel.end_silence_ms; $("vcQuietOut").textContent = dur(Number(el.value));
  el.oninput = () => { $("vcQuietOut").textContent = dur(Number(el.value)); };
  el.onchange = async () => {
    try {
      const r = await (await post("/settings", { channel: { end_silence_ms: Number(el.value) } })).json();
      say("vcMsg", `Saved: ${dur(r.channel.end_silence_ms)} of quiet ends a turn.`);
    } catch (e) { say("vcMsg", e.message, true); }
  };
}
function paintTalk(t) {
  const left = t.on ? Math.max(0, Math.round((t.until * 1000 - Date.now()) / 60000)) : 0;
  $("tkFacts").innerHTML = facts([
    ["Back and forth", t.on ? `on, ${left} minute${left === 1 ? "" : "s"} left` : "off"],
    ["The voice", t.on ? (t.voice === "held" ? "loaded and held" : t.voice || "being loaded...") : "put away when idle, as usual"],
    ["The robot", t.channel_open ? (t.listening ? "listening" : "channel open, not listening right now") : "channel closed"],
  ]);
}
async function setTalk(body) {
  try { paintTalk(await (await post("/talk", body)).json()); say("tkMsg", ""); }
  catch (e) { say("tkMsg", e.message, true); }
}
function voiceOpen() { loadQuiet(); loadDeadAir(); }
// whether the list of turns is open is the reader's choice, and is remembered
function keepLogOpen() {
  const log = $("vcLog");
  try { log.open = localStorage.getItem("soma_turns_open") === "1"; } catch (e) { /* no storage: it starts closed */ }
  log.addEventListener("toggle", () => { try { localStorage.setItem("soma_turns_open", log.open ? "1" : "0"); } catch (e) { /* fine */ } });
}

document.addEventListener("DOMContentLoaded", () => {
  watch(() => showing("voice") && "/api/voice-timing", 2000, paintVoice, e => { $("vcFacts").textContent = why(e); });
  watch(() => showing("voice") && "/talk", 2000, paintTalk, e => { $("tkFacts").textContent = why(e); });
  keepLogOpen();
  $("tkOn").onclick = () => setTalk({ minutes: Number($("tkMinutes").value) });
  $("tkOff").onclick = () => setTalk({ on: false });
});

// -- power --
// What the power chip measures, from the robot's /api/status, and how it moves while you watch.
// Two strips and not one chart with two scales: the battery and the USB input sit a volt apart and
// what matters in each is a movement of tens of millivolts.
const PW = { on: false, rows: [], max: 900, hover: null };
const volts = mv => (mv / 1000).toFixed(3) + " V";
const clock = t => new Date(t).toLocaleTimeString([], { hour12: false });
const DIRECTION = { charging: "into the battery", discharging: "out of the battery", standby: "neither way" };
const nearest = (rows, t) => rows.reduce((best, c) => Math.abs(c.t - t) < Math.abs(best.t - t) ? c : best);

function powerTake(s) {
  const p = s.power, row = { t: Date.now(), pct: s.batteryLevel, flag: s.charging, fw: s.firmware };
  if (p) Object.assign(row, { mv: p.batteryMv, usb: p.usbMv, sys: p.systemMv, dir: p.direction, phase: p.phase,
    limited: p.inputLimited, good: p.usbGood, present: p.batteryPresent, temp: p.chipTempC, raw: p.raw });
  // the body's battery monitor, on a firmware and a body that have one: the only current reading
  if (p && p.monitorMa != null) { row.ma = p.monitorMa; row.bodyMv = p.monitorMv; }
  PW.rows.push(row);
  if (PW.rows.length > PW.max) PW.rows.shift();
}

function powerEvents() {
  const out = [], seen = PW.rows;
  for (let i = 1; i < seen.length; i++) {
    const a = seen[i - 1], b = seen[i];
    if (b.miss && !a.miss) out.push([b.t, "the robot did not answer"]);
    if (a.miss && !b.miss) out.push([b.t, "it answered again"]);
    if (a.miss || b.miss) continue;
    if (a.fw !== b.fw) out.push([b.t, `firmware ${a.fw} → ${b.fw}`]);
    if (a.dir !== b.dir) out.push([b.t, `current ${DIRECTION[a.dir] || a.dir} → ${DIRECTION[b.dir] || b.dir}`]);
    if (a.phase !== b.phase) out.push([b.t, `charge phase ${a.phase} → ${b.phase}`]);
    if (a.good !== b.good) out.push([b.t, b.good ? "USB input became usable" : "USB input stopped being usable"]);
    if (a.limited !== b.limited) out.push([b.t, b.limited ? "the chip started holding back" : "the chip stopped holding back"]);
    if (a.pct !== b.pct) out.push([b.t, `gauge ${a.pct}% → ${b.pct}%`]);
  }
  return out;
}

const TREND = { left: 56, right: 12, top: 10, bottom: 18 };
// floor: the least height the strip shows (noise is not news); label: how a value on the axis reads.
function drawTrend(id, key, floor = 60, label = v => (v / 1000).toFixed(3)) {
  const cv = $(id), g = cv.getContext("2d"), css = getComputedStyle(document.documentElement);
  const dim = css.getPropertyValue("--text-dim"), { left, right, top, bottom } = TREND;
  g.clearRect(0, 0, cv.width, cv.height);
  g.font = "10px " + css.getPropertyValue("--mono"); g.fillStyle = dim;
  const rows = PW.rows, pts = rows.filter(r => r[key] != null);
  if (pts.length < 2) { g.fillText(pts.length ? "one reading so far" : "no readings yet", left, cv.height / 2); return; }
  const t0 = rows[0].t, t1 = rows[rows.length - 1].t, span = Math.max(t1 - t0, 1000);
  let lo = Math.min(...pts.map(r => r[key])), hi = Math.max(...pts.map(r => r[key]));
  const pad = Math.max((floor - (hi - lo)) / 2, (hi - lo) * 0.1);
  lo -= pad; hi += pad;
  const x = t => left + (t - t0) / span * (cv.width - left - right);
  const y = v => top + (hi - v) / (hi - lo) * (cv.height - top - bottom);
  g.strokeStyle = css.getPropertyValue("--border"); g.lineWidth = 1;
  [hi, (hi + lo) / 2, lo].forEach(v => {
    g.beginPath(); g.moveTo(left, y(v)); g.lineTo(cv.width - right, y(v)); g.stroke();
    g.fillText(label(v), 4, y(v) + 3);
  });
  const end = clock(t1);
  g.fillText(clock(t0), left, cv.height - 4); g.fillText(end, cv.width - right - g.measureText(end).width, cv.height - 4);
  g.strokeStyle = css.getPropertyValue("--accent"); g.lineWidth = 2; g.lineJoin = "round"; g.beginPath();
  let pen = false;                                                // lifted where a reading is missing
  rows.forEach(r => {
    if (r[key] == null) { pen = false; return; }
    if (pen) g.lineTo(x(r.t), y(r[key])); else g.moveTo(x(r.t), y(r[key]));
    pen = true;
  });
  g.stroke();
  if (PW.hover != null) {
    const r = nearest(pts, PW.hover);
    g.strokeStyle = dim; g.lineWidth = 1; g.beginPath(); g.moveTo(x(r.t), top); g.lineTo(x(r.t), cv.height - bottom); g.stroke();
    g.fillStyle = css.getPropertyValue("--accent"); g.beginPath(); g.arc(x(r.t), y(r[key]), 4, 0, 2 * Math.PI); g.fill();
  }
}
const drawTrends = () => {
  drawTrend("pwBatt", "mv"); drawTrend("pwUsb", "usb");
  const has = PW.rows.some(r => r.ma != null);
  $("pwAmpsWrap").hidden = !has;
  if (has) drawTrend("pwAmps", "ma", 40, v => v.toFixed(0));
};

function paintPower() {
  const last = PW.rows.filter(r => !r.miss).pop(), first = PW.rows.find(r => r.mv != null);
  if (!last) { $("pwFacts").innerHTML = ""; }
  else if (last.mv == null) {
    $("pwFacts").innerHTML = facts([["Battery", `${last.pct}% by the chip's gauge`], ["Charging flag", last.flag ? "yes" : "no"],
      ["Firmware", last.fw]]);
    say("pwMsg", "This robot's firmware reports only the percentage and the flag. It needs a build with the power readings.", true);
  } else {
    const moved = last.mv - first.mv, secs = Math.round((last.t - first.t) / 1000);
    $("pwFacts").innerHTML = facts([
      ["Battery", `${volts(last.mv)}, ${last.pct}% by the chip's gauge${last.present ? "" : ", and the chip sees no battery"}`],
      ["USB in", `${volts(last.usb)}, ${last.good ? "usable" : "not usable"}`],
      ["System", volts(last.sys)],
      ...(last.ma == null ? [["Battery current", "this body or firmware has no monitor to read it from"]] : [
        // the sign was settled on the robot, 19 Nov 2026: negative is current into the battery
        ["Battery current", `${Math.abs(last.ma).toFixed(1)} mA ${last.ma < 0 ? "into the battery" : last.ma > 0 ? "out of the battery" : "either way"}, by the body's monitor`],
        ["Battery, at the body", volts(last.bodyMv)]]),
      ["Current", DIRECTION[last.dir] || last.dir],
      ["Charge phase", last.phase],
      ["Held back", last.limited ? "yes: the input sags or is at its limit" : "no"],
      ["Chip", `${last.temp.toFixed(1)} °C`],
      ["Since you started", secs < 4 ? "just now"
        : `battery ${moved >= 0 ? "+" : "−"}${Math.abs(moved)} mV in ${secs < 120 ? secs + " s" : ago(secs)}`],
      ["Raw", last.raw],
    ]);
  }
  drawTrends();
  const ev = powerEvents().slice(-12).reverse();
  $("pwEvents").innerHTML = ev.map(([t, what]) => `<div><span>${clock(t)}</span>${escapeHtml(what)}</div>`).join("")
    || `<span>Nothing yet.</span>`;
  const misses = PW.rows.filter(r => r.miss).length;
  $("pwRate").textContent = PW.rows.length ? `${PW.rows.length} readings${misses ? `, ${misses} missed` : ""}` : "";
}

async function powerLoop() {
  while (PW.on && showing("power")) {
    const began = Date.now();
    let failed = "";
    try { powerTake(await getJson("/bench/status")); }
    catch (e) { PW.rows.push({ t: Date.now(), miss: true }); failed = e.message; }
    if (failed) say("pwMsg", failed, true); else say("pwMsg", "");
    paintPower();
    await sleep(Math.max(0, 2000 - (Date.now() - began)));
  }
  PW.on = false; $("pwWatch").textContent = "Start watching";
}

function powerCsv() {
  if (!PW.rows.length) { say("pwMsg", "Watch for a while first.", true); return; }
  const cell = v => v == null ? "" : v, flag = v => v == null ? "" : (v ? 1 : 0);
  download("power.csv", "time,battery_mv,usb_mv,system_mv,body_battery_mv,body_battery_ma,gauge_percent,current,charge_phase,held_back,usb_usable,chip_c,firmware\n" +
    PW.rows.map(r => [new Date(r.t).toISOString(), cell(r.mv), cell(r.usb), cell(r.sys), cell(r.bodyMv), cell(r.ma), cell(r.pct), cell(r.dir), cell(r.phase),
      flag(r.limited), flag(r.good), cell(r.temp), cell(r.fw)].join(",")).join("\n") + "\n", "text/csv");
}

document.addEventListener("DOMContentLoaded", () => {
  $("pwWatch").onclick = () => {
    PW.on = !PW.on; $("pwWatch").textContent = PW.on ? "Stop watching" : "Start watching";
    if (PW.on) powerLoop();
  };
  $("pwCsv").onclick = powerCsv;
  ["pwBatt", "pwUsb", "pwAmps"].forEach(id => {
    const cv = $(id);
    cv.onmousemove = e => {
      const rows = PW.rows, pts = rows.filter(r => r.mv != null);
      if (pts.length < 2) return;
      const px = e.offsetX * cv.width / cv.clientWidth, t0 = rows[0].t, span = Math.max(rows[rows.length - 1].t - t0, 1000);
      PW.hover = t0 + Math.min(1, Math.max(0, (px - TREND.left) / (cv.width - TREND.left - TREND.right))) * span;
      const r = nearest(pts, PW.hover);
      $("pwHover").textContent = `${clock(r.t)}: battery ${volts(r.mv)}, USB in ${volts(r.usb)}, gauge ${r.pct}%, current ${DIRECTION[r.dir] || r.dir}` +
        (r.ma == null ? "" : `, ${r.ma.toFixed(1)} mA at the body`);
      drawTrends();
    };
    cv.onmouseleave = () => { PW.hover = null; $("pwHover").textContent = "Point at a line to read it."; drawTrends(); };
  });
});

// ── Diagnostics: the stock body's five benches under one tab ─────────
// Which one was open last is the reader's choice, and is remembered.
const SUBS = ["voice", "servos", "lights", "touch", "power"];
function showSub(id) {
  document.querySelectorAll(".subtabs .subtab").forEach(b => b.classList.toggle("active", b.dataset.sub === id));
  document.querySelectorAll(".subview").forEach(v => v.classList.toggle("active", v.id === id));
  try { localStorage.setItem("soma_diag_sub", id); } catch (e) { /* fine */ }
  if (id === "voice") voiceOpen(); else benchOpen();
}
function diagOpen() {
  let id = "voice";
  try { const kept = localStorage.getItem("soma_diag_sub"); if (SUBS.includes(kept)) id = kept; } catch (e) { /* fine */ }
  showSub(id);
}

// ── Appendages: the plan, checked, and what the robot says it carries ─
// firmware/docs/appendages.md is the design. The plan and its pin check are this box's; "Send to the
// robot" makes the ticked ones the robot's list. Against firmware from before the registry the robot
// reports nothing and the Diagnostics pane says so.
const AP = { data: null, sel: null, level: 20, secs: 5 };   // 20: 1.6% of torch, inside what was comfortable in a dark room
async function apOpen() {
  fanRead();
  try { AP.data = await getJson("/appendages"); say("apMsg", ""); }
  catch (e) { say("apMsg", why(e), true); return; }
  if (!AP.data.plan.some(a => a.name === AP.sel)) AP.sel = AP.data.plan.length ? AP.data.plan[0].name : null;
  apPaint();
}
async function apSave(plan) {
  try {
    AP.data = await (await call("/appendages", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ appendages: plan }) })).json();
    say("apMsg", "Kept.");
  } catch (e) { say("apMsg", e.message, true); }
  apPaint();
}
function apPaint() {
  const d = AP.data; if (!d) return;
  const chk = d.check;
  $("apList").innerHTML = d.plan.map(a => {
    const ok = chk.appendages[a.name].ok;
    return `<div class="apitem${a.name === AP.sel ? " sel" : ""}${ok ? "" : " bad"}" data-name="${escapeHtml(a.name)}">
      <input type="checkbox" ${a.on ? "checked" : ""} data-name="${escapeHtml(a.name)}" title="In the robot's list">
      <span>${escapeHtml(a.name)}<br><span class="muted tiny">${escapeHtml(a.kind)}${ok ? "" : " · pin problem"}</span></span></div>`;
  }).join("") || `<p class="muted tiny">No appendages in the plan.</p>`;
  $("apList").querySelectorAll(".apitem").forEach(el => el.onclick = e => {
    if (e.target.tagName === "INPUT") return;
    AP.sel = el.dataset.name; apPaint();
  });
  $("apList").querySelectorAll("input[type=checkbox]").forEach(cb => cb.onchange = () => {
    apSave(d.plan.map(a => a.name === cb.dataset.name ? { ...a, on: cb.checked } : a));
  });
  const b = chk.budget;
  $("apBudget").innerHTML = facts([["Free pins used", `${b.used} of ${b.free}`],
    ["Left", b.left.length ? b.left.map(p => `G${p}`).join(" ") : "none"]]);
  // what the robot carries beside what is ticked here, so a list that hasn't been sent shows
  const ticked = d.plan.filter(x => x.on).map(x => x.name).sort().join(", ") || "nothing";
  const carried = d.robot.reported ? ((d.robot.appendages || []).map(x => x.name).sort().join(", ") || "nothing") : null;
  $("apSync").textContent = carried === null ? d.robot.why
    : carried === ticked ? `The robot carries what's ticked: ${carried}.`
    : `The robot carries ${carried}; ticked here is ${ticked}. Send to the robot to make them match.`;
  $("apApply").onclick = apApply;
  $("apApply").disabled = carried === null;

  const a = d.plan.find(x => x.name === AP.sel);
  if (!a) { $("apTitle").textContent = "About it"; $("apFacts").innerHTML = ""; $("apPins").innerHTML = ""; return; }
  const kind = d.kinds[a.kind], mine = chk.appendages[a.name];
  $("apTitle").textContent = a.name;
  $("apFacts").innerHTML = facts([
    ["Kind", kind.title],
    ["Gets on by", kind.how === "described" ? "a line on the card (no rebuild)" : "a firmware build with it compiled in"],
    ["Power", kind.power],
    ["In the robot's list", a.on ? "yes" : "no"],
    ...Object.entries(a.opts).map(([k, v]) => [k, v]),
  ]);
  $("apPins").innerHTML = Object.entries(kind.pins).map(([role, what]) => {
    const pin = a.pins[role];
    const where = pin === undefined ? "" : d.free_pins[String(pin)] || "";
    return `<tr><td>${escapeHtml(role)}</td>
      <td class="num"><input type="number" min="0" max="48" value="${pin ?? ""}" data-role="${escapeHtml(role)}" class="appin"></td>
      <td>${escapeHtml(where)}</td><td class="muted">${escapeHtml(what)}</td></tr>`;
  }).join("");
  $("apPins").querySelectorAll("input.appin").forEach(inp => inp.onchange = () => {
    const pins = { ...a.pins };
    if (inp.value === "") delete pins[inp.dataset.role]; else pins[inp.dataset.role] = Number(inp.value);
    apSave(d.plan.map(x => x.name === a.name ? { ...x, pins } : x));
  });
  $("apProblems").textContent = mine.problems.join(" · ");
  $("apProblems").className = "msg tiny" + (mine.problems.length ? " bad" : " ok");
  if (!mine.problems.length) $("apProblems").textContent = a.on ? "Its pins are clear." : "Not in the robot's list, so it claims no pins.";
  $("apCard").textContent = mine.card;

  const r = d.robot, there = r.reported && (r.appendages || []).find(x => x.name === a.name);
  $("apRobot").innerHTML = facts([["The robot", r.reported ? (there ? (there.ok ? "carries it" : `refused it: ${there.error || "no reason given"}`) : "doesn't list it") : r.why]]);
  $("apTest").textContent = "Self-test: " + kind.test;
  if (there && there.ok && a.kind === "flashlight") {
    // level 1 to 100 and how long; every on ends by itself on the robot, so there is no "on until I say"
    $("apActions").innerHTML = `
      <label class="tiny">Level <input type="number" id="apLevel" class="appin" min="1" max="100" value="${AP.level}"></label>
      <label class="tiny">Seconds <input type="number" id="apSecs" class="appin" min="1" max="600" value="${AP.secs}"></label>
      <button class="btn" id="apOn">On</button><button class="btn" id="apOff">Off</button>`;
    $("apLevel").onchange = () => { AP.level = Math.max(1, Math.min(100, Number($("apLevel").value) || 1)); };
    $("apSecs").onchange = () => { AP.secs = Math.max(1, Math.min(600, Number($("apSecs").value) || 5)); };
    $("apOn").onclick = () => { $("apLevel").onchange(); $("apSecs").onchange(); apAct(a.name, { action: "on", level: AP.level, ms: AP.secs * 1000 }); };
    $("apOff").onclick = () => apAct(a.name, { action: "off" });
  } else {
    const whyNot = !kind.built ? "The firmware has no " + a.kind + " yet" : there ? "The robot refused it" : "The robot doesn't carry it: send it the list";
    $("apActions").innerHTML = kind.actions.map(x =>
      `<button class="btn" disabled title="${escapeHtml(whyNot)}">${escapeHtml(x)}</button>`).join("");
  }
  $("apState").hidden = !there;
  if (there) $("apState").textContent = JSON.stringify(there.state || there, null, 1);
}
// One of an appendage's own actions, passed through to the robot. Its answer is its new state.
async function apAct(name, body) {
  try {
    const ans = await (await call(`/appendages/${encodeURIComponent(name)}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    const there = ((AP.data.robot || {}).appendages || []).find(x => x.name === name);
    if (there && ans.state) there.state = ans.state;
    say("apActMsg", body.action === "off" ? "Off." : `On at level ${ans.state.level}${ans.state.asked ? ` (asked ${ans.state.asked}; the night ceiling held it)` : ""}, ${ans.state.lightPercent ?? ans.state.onPercent}% of torch's light.`);
  } catch (e) {
    let text = e.message;
    try { text = JSON.parse(text).error || text; } catch (_) { /* plain text already */ }
    say("apActMsg", text, true);
  }
  apPaint();
}
// The fan on the bench: /bench/fan names its registers itself, so this card can't reach anything else.
function fanShow(s) {
  $("fanFacts").innerHTML = facts([["Running", s.enabled && s.speed > 0 ? `yes, at ${s.speed}%` : "no"],
    ["Speed read back", `${s.rpm.toLocaleString()} RPM`], ["Drive", s.pwmHz ? `${s.pwmHz / 1000} kHz` : "unknown"]]);
}
async function fanRead() {
  try { fanShow(await getJson("/bench/fan")); say("fanMsg", ""); }
  catch (e) { say("fanMsg", why(e), true); }
}
async function fanSet(speed) {
  try {
    const s = await (await call("/bench/fan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speed }) })).json();
    fanShow(s);
    say("fanMsg", speed ? `Set to ${speed}%. The RPM settles in a second or two: Read again.` : "Stopped.");
  } catch (e) { say("fanMsg", why(e), true); }
}
$("fanSpeed").oninput = () => { $("fanSpeedText").textContent = $("fanSpeed").value + "%"; };
$("fanSet").onclick = () => fanSet(Number($("fanSpeed").value));
$("fanStop").onclick = () => fanSet(0);
$("fanRead").onclick = fanRead;

// The ticked appendages become the robot's whole list, and it restarts to read it.
async function apApply() {
  if (!confirm("Save the ticked appendages on the robot? It restarts to read the list, which takes about a minute.")) return;
  try {
    const ans = await (await call("/appendages/apply", { method: "POST" })).json();
    const names = Object.keys(ans.sent);
    say("apMsg", `Sent ${names.length ? names.join(", ") : "an empty list"}. The robot is restarting; open this tab again in a minute.`);
  } catch (e) { say("apMsg", e.message, true); }
}
