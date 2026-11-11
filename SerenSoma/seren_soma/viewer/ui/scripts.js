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
  $("fwRows").innerHTML = (f.staged || []).map(s =>
    `<tr><td>${escapeHtml(s.version)}</td><td>${escapeHtml(s.source)}</td><td>${Math.round(s.size / 1024)} KB</td>` +
    `<td><button class="btn" data-fw="${escapeHtml(s.version)}">Install</button></td></tr>`).join("") ||
    `<tr><td colspan="4" class="muted tiny">Nothing staged yet.</td></tr>`;
  $("fwRows").querySelectorAll("button[data-fw]").forEach(b => b.onclick = () => installFirmware(b.dataset.fw));
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
