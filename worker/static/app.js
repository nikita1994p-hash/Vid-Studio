// Vid Studio: a small hash-routed dashboard over the worker's /api.
const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
let health = null;
let timer = null;
let currentTab = "details";

const STAGES = [
  ["script", "Script & hooks", "✎"],
  ["check", "Fact check", "🔎"],
  ["voice", "Voice-over", "🎙"],
  ["visuals", "Images & video", "🖼"],
  ["render", "Editing", "🎞"],
  ["youtube", "YouTube", "▶"],
];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (res.status === 401) { location.href = "/login"; throw new Error("sign in first"); }
  if (res.status === 409 && data && data.detail === "create a channel first" && !location.hash.startsWith("#/channels")) {
    location.hash = "#/channels";
  }
  if (!res.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toast.h);
  toast.h = setTimeout(() => (t.hidden = true), isErr ? 7000 : 3500);
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const fmtSec = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "—");
const money = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

function stateChip(job) {
  const label = { queued: "Queued", running: "Working", done: "Ready", error: "Failed" }[job.state] || job.state;
  const stage = job.state === "running" && job.stage ? ` · ${STAGES.find((s) => s[0] === job.stage)?.[1] || ""}` : "";
  return `<span class="chip ${esc(job.state)}">${esc(label + stage)}</span>`;
}

function ytChip(yt) {
  if (!yt || yt.state === "none") return `<span class="muted">Not uploaded</span>`;
  if (yt.state === "uploaded") {
    const label = { schedule: "Scheduled", public: "Public", private: "Private", auto: "Uploaded" }[yt.mode] || "Uploaded";
    return `<a class="chip uploaded" href="${esc(yt.studio_url)}" target="_blank" rel="noopener">${label} ↗</a>`;
  }
  const stale = yt.at && Date.now() - new Date(yt.at).getTime() > 10 * 60 * 1000;
  return stale
    ? `<span class="chip error" title="No answer from n8n for 10 minutes: open n8n → Executions">Check n8n</span>`
    : `<span class="chip uploading">Uploading…</span>`;
}

function stepper(stages) {
  return `<div class="stepper">${STAGES.map(([key, label, icon]) => {
    const s = (stages && stages[key]) || { state: "pending" };
    const mark = { done: "✓", error: "!", skipped: "–" }[s.state] || icon;
    return `<div class="step ${esc(s.state)}"><div class="dot">${mark}</div>
      <div class="step-label">${esc(label)}</div><div class="step-detail">${esc(s.detail || "")}</div></div>`;
  }).join("")}</div>`;
}

function thumb(job) {
  return job.thumb
    ? `<img class="thumb" src="/files/${esc(job.job_id)}/thumb.jpg" alt="" loading="lazy">`
    : `<div class="thumb ph">${job.state === "error" ? "!" : "⏳"}</div>`;
}

function poll(fn, ms) {
  clearTimeout(timer);
  timer = setTimeout(fn, ms);
}

// ------------------------------------------------------------------ views

async function renderDashboard() {
  const jobs = await api("/api/jobs");
  const active = jobs.find((j) => j.state === "running" || j.state === "queued");
  const done = jobs.filter((j) => j.state === "done");
  const uploaded = jobs.filter((j) => j.youtube?.state === "uploaded");
  const month = new Date().toISOString().slice(0, 7);
  const spent = jobs.filter((j) => (j.created || "").startsWith(month)).reduce((a, j) => a + (j.cost || 0), 0);
  const earn = await api("/api/earnings").catch(() => null);

  view.innerHTML = `
    <h1>Channel dashboard</h1>
    <div class="grid">
      <section class="card wide">
        <h2>Pipeline now</h2>
        ${active ? `
          <div class="vid" style="margin-bottom:20px">${thumb(active)}
            <div><div class="vid-title"><a href="#/video/${esc(active.job_id)}">${esc(active.title)}</a></div>
            <div class="muted">${when(active.created)} ${active.mock ? '<span class="chip test">TEST</span>' : ""}</div></div></div>
          <div id="active-stepper"></div>`
        : `<div class="empty">Nothing is being made right now.<br><br>
            <button class="btn btn-primary" data-action="new">Create a video</button></div>`}
      </section>
      <section class="card">
        <h2>Channel analytics</h2>
        <div class="stats">
          <div><div class="stat">${done.length}</div><div class="stat-label">Videos ready</div></div>
          <div><div class="stat">${uploaded.length}</div><div class="stat-label">Sent to YouTube</div></div>
          <div><div class="stat">${earn ? usd(earn.month.profit) : money(spent)}</div><div class="stat-label"><a href="#/earnings">${earn ? "Profit this month →" : "AI cost this month"}</a></div></div>
          <div><div class="stat">${jobs.filter((j) => j.state === "error").length}</div><div class="stat-label">Failed runs</div></div>
        </div>
      </section>
      <section class="card">
        <h2>Setup</h2>
        <div class="kv">
          <div>Mode</div><div>${health?.mock ? '<span class="chip test">TEST (free)</span>' : "Live (Gemini)"}</div>
          <div>Voice</div><div>${esc(health?.tts_provider)} · ${esc(health?.voice)}</div>
          <div>Auto-publish</div><div>${health?.auto_publish ? `on, ${esc(health.publish)}` : "off — uploads stay private"}</div>
        </div>
        <p><a href="#/settings">All settings →</a></p>
      </section>
      <section class="card wide">
        <h2>Latest content</h2>
        ${contentTable(jobs.slice(0, 5))}
        <p><a href="#/content">Go to content →</a></p>
      </section>
    </div>`;
  if (active) {
    const full = await api(`/api/jobs/${active.job_id}`);
    $("#active-stepper").innerHTML = stepper(full.status.stages);
  }
  poll(route, active ? 2000 : 10000);
}

function contentTable(jobs) {
  if (!jobs.length) return `<div class="empty">No videos yet. Press <b>＋ CREATE</b>.</div>`;
  return `<table><thead><tr><th>Video</th><th>Status</th><th>YouTube</th><th>Date</th><th>Length</th><th>AI cost</th></tr></thead>
    <tbody>${jobs.map((j) => `
      <tr class="row" data-href="#/video/${esc(j.job_id)}">
        <td><div class="vid">${thumb(j)}<div><div class="vid-title">${esc(j.title)}${j.variant ? ` <span class="chip variant">${esc(j.variant)}</span>` : ""}</div>
          <div class="muted">${j.kind === "uploaded" ? "Uploaded by you" : "AI generated"}
          ${j.mock ? '<span class="chip test">TEST</span>' : ""}</div></div></div></td>
        <td>${stateChip(j)}</td>
        <td>${ytChip(j.youtube)}</td>
        <td>${when(j.created)}</td>
        <td>${fmtSec(j.seconds)}</td>
        <td>${money(j.cost)}</td>
      </tr>`).join("")}</tbody></table>`;
}

async function renderContent() {
  const jobs = await api("/api/jobs");
  view.innerHTML = `<h1>Channel content</h1><section class="card">${contentTable(jobs)}</section>`;
  poll(route, jobs.some((j) => j.state === "running" || j.state === "queued") ? 2000 : 10000);
}

async function renderVideo(id) {
  const data = await api(`/api/jobs/${encodeURIComponent(id)}`);
  const { status, result, plan, scores, files, scenes: sceneData, factcheck, promptcheck, hookcheck, cover } = data;
  const busy = status.state === "running" || status.state === "queued";
  const ready = status.state === "done" && files.includes("final.mp4");
  const base = `/files/${encodeURIComponent(id)}`;
  const editing = document.activeElement && document.activeElement.closest && document.activeElement.closest("#meta-form, #cover-form");
  if (editing) return poll(route, 3000); // do not wipe what the user is typing

  const accounts = ready ? await api("/api/accounts").catch(() => []) : [];
  const oldPlayer = view.querySelector("video.player");
  view.innerHTML = `
    <p><a href="#/content">← Channel content</a></p>
    <div class="video-page">
      <div>
        ${ready
          ? `<video class="player" data-job="${esc(id)}" src="${base}/final.mp4?t=${encodeURIComponent(status.updated || "")}" controls playsinline preload="metadata"${files.includes("thumb.jpg") ? ` poster="${base}/thumb.jpg"` : ""}></video>`
          : `<div class="player-ph">${status.state === "error" ? "This run failed — see the error on the right." : "The video appears here when editing is done…"}</div>`}
        <div class="actions">
          ${ready ? `<a class="btn btn-outline" href="${base}/final.mp4?download=1">⬇ Download MP4</a>` : ""}
          <button class="btn btn-danger" id="delete-btn" ${busy ? "disabled" : ""}>Delete</button>
        </div>
      </div>
      <div>
        <h1 style="margin-bottom:6px">${esc(result?.title || status.topic || "New video")}</h1>
        <div class="muted" style="margin-bottom:20px">${when(status.created)} · ${status.kind === "uploaded" ? "Uploaded by you" : "AI generated"}
          ${status.mock ? '<span class="chip test">TEST</span>' : ""} · ${stateChip(status)} · ${ytChip(status.youtube)}
          ${result?.variant ? ` · <span class="chip variant">Version ${esc(result.variant)}</span>` : ""}
          ${result?.variant_of ? ` <a href="#/video/${esc(result.variant_of)}">of the original</a>` : ""}</div>
        <section class="card">${stepper(status.stages)}
          ${status.error ? `<div class="error-box">${esc(status.error)}</div>` : ""}
        </section>
        ${factCard(factcheck)}
        ${promptCard(promptcheck)}
        ${ready && status.kind !== "uploaded" ? coverCard(base, cover, sceneData, files) : ""}
        ${ready ? publishCard(status, accounts) : ""}
        ${ready && status.kind !== "uploaded" ? variantsCard(result) : ""}
        <div class="tabs">
          ${["details", "script", "files"].map((t) => `<button class="tab ${currentTab === t ? "active" : ""}" data-tab="${t}">${
            { details: "Details", script: "Script & hooks", files: "Files" }[t]}</button>`).join("")}
        </div>
        <div id="tab-body">${tabBody(currentTab, { id, result, plan, scores, files, base, sceneData, busy, version: status.updated, hookcheck })}</div>
      </div>
    </div>`;

  // keep the very same <video> element across redraws: a new element would restart playback
  const freshPlayer = view.querySelector("video.player");
  if (oldPlayer && freshPlayer && oldPlayer.dataset.job === id &&
      oldPlayer.getAttribute("src") === freshPlayer.getAttribute("src")) {
    freshPlayer.replaceWith(oldPlayer);
  }

  $("#delete-btn")?.addEventListener("click", async () => {
    if (!confirm("Delete this video and all its files from this computer? (YouTube is not touched.)")) return;
    try { await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }); toast("Deleted"); location.hash = "#/content"; }
    catch (e) { toast(e.message, true); }
  });
  $("#publish-btn")?.addEventListener("click", async () => {
    const mode = $("input[name=mode]:checked").value;
    const platforms = [...view.querySelectorAll("input[name=platform]:checked")].map((x) => x.value);
    if (!platforms.length) return toast("Tick at least one platform", true);
    const direct = $("#publish-direct").checked;
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/publish`, { method: "POST", body: JSON.stringify({ mode, platforms, direct }) });
      toast(direct ? "Posting from the Studio…" : "Sent to n8n — it posts in a minute or two");
      route();
    } catch (e) { toast(e.message, true); }
  });
  $("#variants-btn")?.addEventListener("click", async () => {
    const count = Number($("#variants-count").value);
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/variants`, { method: "POST", body: JSON.stringify({ count }) });
      toast(`Making ${count} more version(s) — they appear in Content`);
    } catch (e) { toast(e.message, true); }
  });
  $("#meta-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(Object.fromEntries(f)) });
      toast("Saved");
      document.activeElement.blur();
      route();
    } catch (e) { toast(e.message, true); }
  });
  view.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { currentTab = b.dataset.tab; route(); }));
  view.querySelectorAll("[data-copy-prompt]").forEach((b) => b.addEventListener("click", async () => {
    const text = `${b.dataset.copyPrompt}. Vertical 9:16 photo, realistic, no text, no letters, no logos.`;
    try { await navigator.clipboard.writeText(text); toast("Prompt copied — paste it into the Gemini app"); }
    catch { prompt("Copy this prompt:", text); }
  }));
  view.querySelectorAll("input[data-scene-file]").forEach((inp) => inp.addEventListener("change", async () => {
    if (!inp.files.length) return;
    const fd = new FormData();
    fd.append("file", inp.files[0]);
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/scenes/${inp.dataset.sceneFile}/image`, { method: "POST", body: fd });
      toast("Picture replaced — press “Re-render video” when you are done");
      route();
    } catch (e) { toast(e.message, true); }
  }));
  $("#cover-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const btn = f.querySelector("button");
    btn.disabled = true;
    btn.textContent = "Making…";
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/cover`, { method: "POST", body: JSON.stringify({
        lines: [...f.querySelectorAll("input[name=line]")].map((x) => x.value.trim()).filter(Boolean),
        scene: f.scene.value === "" ? null : Number(f.scene.value) }) });
      toast("Cover updated");
      route();
    } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Remake cover"; }
  });
  $("#rerender-btn")?.addEventListener("click", async () => {
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/rerender`, { method: "POST" });
      toast("Re-rendering — about a minute");
      route();
    } catch (e) { toast(e.message, true); }
  });
  // a finished video does not change by itself, so only keep refreshing while something is moving
  const moving = Object.values(status.social || {}).some((s) => ["queued", "uploading"].includes(s.state));
  if (busy || moving || ["queued", "uploading"].includes(status.youtube?.state)) poll(route, 2000);
}

const SOCIAL_STATE = { queued: "queued", uploading: "uploading…", posted: "posted ✓", error: "failed" };

function publishCard(status, accounts) {
  const social = status.social || {};
  const yt = status.youtube || {};
  const rows = accounts.map((a) => {
    const s = social[a.id] || (a.id === "youtube" && yt.state === "uploaded" ? { state: "posted", url: yt.url } : null);
    const busyHere = s && ["queued", "uploading"].includes(s.state);
    const chip = s ? `<span class="chip ${s.state === "posted" ? "done" : s.state === "error" ? "error" : "uploading"}" title="${esc(s.error || s.note || "")}">${esc(SOCIAL_STATE[s.state] || s.state)}</span>` : "";
    const link = s?.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener">open ↗</a>` : "";
    return `<label class="platform ${a.connected ? "" : "off"}">
      <input type="checkbox" name="platform" value="${esc(a.id)}" ${a.connected ? "" : "disabled"} ${a.connected && a.id === "youtube" && !s ? "checked" : ""} ${busyHere ? "disabled" : ""}>
      <b>${esc(a.name)}</b> <span class="muted">${a.connected ? "→ " + esc(a.account?.name || "") : "not connected"}</span> ${chip}${link}
      ${s?.state === "error" ? `<div class="error-box" style="margin:4px 0 0">${esc(s.error || "")}</div>` : ""}</label>`;
  }).join("");
  const none = !accounts.some((a) => a.connected);
  return `<section class="card" style="margin-top:16px"><h3>Publish</h3>
    ${none ? `<p class="muted">No account connected yet — <a href="#/accounts">Accounts</a>.</p>` : ""}
    <div class="platforms">${rows}</div>
    <div class="radio-row">
      <label><input type="radio" name="mode" value="private" checked> Private / only me (review first)</label>
      <label><input type="radio" name="mode" value="schedule"> YouTube: schedule · ${esc(health?.publish || "")}</label>
      <label><input type="radio" name="mode" value="public"> Public now</label>
    </div>
    <label class="check"><input type="checkbox" id="publish-direct"> Post directly from the Studio (without n8n)</label>
    <button class="btn btn-primary" id="publish-btn" ${none ? "disabled" : ""}>Publish</button>
    <span class="muted" style="margin-left:8px">Marked as AI-generated where the platform supports it.
      Instagram, Facebook and X have no private mode: there "Private" still posts publicly.</span></section>`;
}

const SEVERITY = { high: "error", medium: "uploading", low: "queued" };

function factCard(fc) {
  if (!fc) return "";
  const head = `<section class="card" style="margin-top:16px"><h3>Fact check <span class="muted">(${esc(fc.mode || "")}${fc.model ? " · " + esc(fc.model) : ""})</span></h3>`;
  if (fc.error) return `${head}<p class="muted">Not checked — no model answered: ${esc(fc.error)}</p></section>`;
  const applied = fc.applied || [];
  if (!(fc.issues || []).length) return `${head}<p><span class="ok">✓</span> ${esc(fc.summary || "All figures look realistic.")}</p></section>`;
  return `${head}<p>${esc(fc.summary || "")}</p>${fc.issues.map((i) => {
    const fixed = i.scene > 0 && applied.includes(i.scene);
    return `<div class="issue"><span class="chip ${SEVERITY[i.severity] || "queued"}">${esc(i.severity)}</span>
      <b>${i.scene === 0 ? `Hook ${esc(i.hook)}` : `Scene ${esc(i.scene)}`}</b>: “${esc(i.claim)}” — ${esc(i.problem)}
      ${fixed ? '<span class="chip done">fixed before the voice-over</span>' : ""}
      ${!fixed && i.fixed_narration ? `<div class="muted">Suggested line: ${esc(i.fixed_narration)}</div>` : ""}</div>`;
  }).join("")}
    ${fc.mode === "flag" ? `<p class="muted">Only flagged. To change a line, write your own script (＋ CREATE → Write my own script) or set Settings → Fact check → fix.</p>` : ""}</section>`;
}

function promptCard(pc) {
  const changed = pc?.changed || [];
  if (!changed.length) return "";
  return `<section class="card" style="margin-top:16px"><h3>Picture prompts <span class="muted">(${changed.length} cleaned before FLUX)</span></h3>
    ${changed.map((c) => `<div class="issue"><b>${c.scene === 0 ? "Hook" : `Scene ${esc(c.scene)}`}</b>: ${esc(c.problem)}
      <div class="muted">Was: ${esc(c.before)}</div><div>Now: ${esc(c.after)}</div></div>`).join("")}</section>`;
}

function coverCard(base, cover, sceneData, files) {
  if (!sceneData) return "";
  const has = files.includes("cover.jpg");
  const lines = cover?.lines || [];
  const pics = sceneData.scenes.map((s, i) => [i, s]).filter(([, s]) => s.visual && !s.card && !s.is_video && !s.loop);
  return `<section class="card" style="margin-top:16px"><h3>Cover</h3>
    <div class="cover-row">
      ${has ? `<a href="${base}/cover.jpg" target="_blank" rel="noopener"><img class="cover-img" src="${base}/cover.jpg?v=${encodeURIComponent(cover?.at || "")}" alt="cover"></a>`
        : `<div class="cover-img ph">No cover yet</div>`}
      <form id="cover-form">
        ${[0, 1, 2].map((k) => `<label>Line ${k + 1}${k === 0 ? ' <span class="muted">(the money figure, shown huge)</span>' : ""}
          <input name="line" maxlength="18" value="${esc(lines[k] || "")}"></label>`).join("")}
        <label>Picture<select name="scene"><option value="">as in Settings</option>
          ${pics.map(([i]) => `<option value="${i}" ${cover?.scene === i ? "selected" : ""}>${i === 0 ? "Hook" : "Scene " + i}</option>`).join("")}</select></label>
        <div class="actions"><button class="btn btn-outline">Remake cover</button>
          ${has ? `<a class="btn btn-text" href="${base}/cover.jpg?download=1">⬇ Download</a>` : ""}</div>
      </form>
    </div>
    <p class="muted">Publish sets it as the thumbnail on YouTube (custom thumbnails need a phone-verified channel) and Facebook,
      and on Instagram when PUBLIC_BASE_URL is set. TikTok and X take no cover through their API — download it and pick it in the app.</p></section>`;
}

function variantsCard(result) {
  if (!result) return "";
  return `<section class="card" style="margin-top:16px"><h3>Versions for A/B testing</h3>
    <p class="muted">Each new version keeps the topic and the facts but gets the next-best hook, another title, voice,
      music, caption colour and new pictures — a real alternative, not a disguised copy (YouTube treats near-duplicates
      as spam). Post different versions to different platforms, or a few days apart, and keep the winner.</p>
    <div class="actions"><select id="variants-count"><option value="1">1 more</option><option value="2">2 more</option>
      <option value="3">3 more</option><option value="4">4 more</option></select>
      <button class="btn btn-outline" id="variants-btn">Make versions</button></div></section>`;
}

function sceneList(sceneData, base, busy, version) {
  const v = encodeURIComponent(version || "");
  const rows = sceneData.scenes.map((s, i) => {
    let visual;
    if (s.card) visual = `<div class="card-thumb">${esc(s.on_screen || "")}</div>`;
    else if (s.is_video) visual = `<div class="thumb ph" title="video clip">🎥</div>`;
    else if (s.visual) visual = `<img src="${base}/${encodeURIComponent(s.visual)}?v=${v}" alt="" loading="lazy">`;
    else visual = `<div class="thumb ph">…</div>`;
    const kind = s.loop ? "the hook's picture, so the end flows back into the start"
      : s.user_image ? "your picture" : s.card ? "number card" : s.is_video ? "video clip" : "photo";
    const n = String(i).padStart(2, "0");
    return `<div class="scene">${visual}<div><b>${i === 0 ? "Hook" : s.loop ? "Loop ending" : "Scene " + i}</b> — ${esc(s.text)}
      <div class="muted">${kind} · on screen: “${esc(s.on_screen || "")}”</div>
      <audio controls preload="none" src="${base}/voice_${n}.wav"></audio>
      ${s.loop ? "" : `<div class="actions" style="margin-top:6px">
        <button class="btn btn-outline" data-copy-prompt="${esc(s.prompt || s.query || "")}">📋 Copy prompt for Gemini</button>
        <label class="btn btn-outline" style="margin:0;cursor:pointer;color:var(--text);font-size:14px">🖼 Replace picture
          <input type="file" accept="image/png,image/jpeg,image/webp" data-scene-file="${i}" hidden></label>
      </div>`}</div></div>`;
  }).join("");
  return `<div class="card" style="margin:12px 0"><b>Free AI pictures from your Gemini Pro:</b>
      <span class="muted">press “Copy prompt”, paste it in the Gemini app (gemini.google.com), download the picture,
      press “Replace picture” on that scene. Then:</span>
      <div class="actions"><button class="btn btn-primary" id="rerender-btn" ${busy ? "disabled" : ""}>🎬 Re-render video</button></div></div>
    ${rows}`;
}

function hookRounds(hc) {
  if (!hc?.rounds || hc.rounds.length < 2) return "";
  const first = hc.rounds[0].best;
  const rejected = hc.rounds.flatMap((r) => r.rejected || []);
  return `<div class="muted" style="margin-bottom:12px">Rewritten to aim for ${esc(hc.target)}+: best hook ${esc(first)} → <b>${esc(hc.best)}</b>
    in ${hc.rounds.length - 1} round(s)${hc.best < hc.target ? " (target not reached — the best that passed the review is used)" : ""}.
    ${rejected.length ? `<details><summary>${rejected.length} rewrite(s) rejected by the review</summary>
      ${rejected.map((x) => `<div class="issue">“${esc(x.hook)}” — ${esc(x.reason)}</div>`).join("")}</details>` : ""}</div>`;
}

function tabBody(tab, { id, result, plan, scores, files, base, sceneData, busy, version, hookcheck }) {
  if (tab === "details") {
    if (!result) return `<div class="empty">Details appear when the script is ready.</div>`;
    return `<form id="meta-form">
      <label>Title (${(result.title || "").length}/100)<input name="title" value="${esc(result.title)}" maxlength="100"></label>
      <label>Description<textarea name="description" rows="5">${esc(result.description)}</textarea></label>
      <label>Tags<input name="tags" value="${esc(result.tags)}"></label>
      ${result.hook ? `<label>Hook used<input value="${esc(result.hook)}" readonly></label>` : ""}
      <button class="btn btn-primary">Save</button>
      <span class="muted" style="margin-left:8px">Edit before sending to YouTube.</span></form>`;
  }
  if (tab === "script") {
    if (!plan) return `<div class="empty">${result ? "Uploaded video — no script." : "The script is being written…"}</div>`;
    const hookScores = Object.fromEntries((scores?.hooks || []).map((h) => [h.hook, h]));
    const titleScores = scores?.titles || [];
    const hooks = plan.hooks.map((h) => {
      const s = hookScores[h.text.replace(/\n/g, " ")];
      const best = result && result.hook === h.text;
      return `<div class="hook ${best ? "best" : ""}"><div class="score" title="hookscore.py verdict">${s ? s.verdict : "—"}</div>
        <div><div>${esc(h.text)}</div><div class="muted">${esc(h.formula_id)}${s ? " · " + esc(s.band) : ""}${best ? " · ✓ used" : ""}</div></div></div>`;
    }).join("");
    const titles = (titleScores.length ? titleScores : plan.titles.map((t) => ({ title: t })))
      .map((t) => `<div class="hook ${result && result.title === t.title ? "best" : ""}"><div class="score" title="title.py score">${t.score ?? "—"}</div><div>${esc(t.title)}</div></div>`).join("");
    const scenes = [{ narration: result?.hook || plan.hooks[0]?.text, image_prompt: plan.hook_visual }, ...plan.scenes]
      .map((s, i) => {
        const n = String(i).padStart(2, "0");
        const img = files.includes(`img_${n}.png`) ? `<img src="${base}/img_${n}.png" alt="" loading="lazy">` : `<div class="thumb ph">${i === 0 && files.includes("hook.mp4") ? "🎥" : "…"}</div>`;
        const audio = files.includes(`voice_${n}.wav`) ? `<audio controls preload="none" src="${base}/voice_${n}.wav"></audio>` : "";
        return `<div class="scene">${img}<div><b>${i === 0 ? "Hook" : "Scene " + i}</b> — ${esc(s.narration)}
          <div class="muted">🖼 ${esc(s.image_prompt)}</div>${audio}</div></div>`;
      }).join("");
    return `<h3>Topic</h3><p>${esc(plan.topic)}</p>
      <h3>Hooks <span class="muted">(scored for Shorts: figure, stakes, curiosity, first five words, length)</span></h3>${hookRounds(hookcheck)}${hooks}
      <h3 style="margin-top:20px">Titles <span class="muted">(scored by your yt-package skill)</span></h3>${titles}
      <h3 style="margin-top:20px">Scenes</h3>${sceneData ? sceneList(sceneData, base, busy, version) : scenes}`;
  }
  const show = files.filter((f) => !f.endsWith(".tmp"));
  return `<div class="files">${show.map((f) => `<a href="${base}/${encodeURIComponent(f)}?download=1">${esc(f)}</a>`).join("")}</div>
    <p class="muted">On your computer: <code>data\\jobs\\${esc(id)}</code> inside the project folder.</p>`;
}

// ------------------------------------------------------------------ settings form

let voiceList = null;
async function loadVoices() {
  if (!voiceList) { try { voiceList = await api("/api/voices"); } catch { voiceList = []; } }
  return voiceList;
}

const signedNum = (v) => parseInt(String(v ?? "").replace(/[^-\d]/g, ""), 10) || 0;
const fmtSigned = (n, unit) => `${n >= 0 ? "+" : ""}${n}${unit}`;

// one input for a setting; blank = an extra "as in Settings" choice (used by the New video dialog)
function fieldInput(f, value, blank = null) {
  const v = value ?? "";
  const nm = `name="${esc(f.key)}"`;
  if (f.type === "textarea") return `<textarea ${nm} rows="3">${esc(v)}</textarea>`;
  if (f.type === "bool") {
    if (blank !== null) return `<select ${nm}><option value="">${esc(blank)}</option>
      <option value="true" ${v === "true" ? "selected" : ""}>on</option><option value="false" ${v === "false" ? "selected" : ""}>off</option></select>`;
    return `<label class="check" style="margin:0"><input type="checkbox" ${nm} ${v === "true" ? "checked" : ""}> on</label>`;
  }
  if (f.type === "select") {
    const opts = [...(blank !== null ? [""] : []), ...(f.options || [])];
    if (v && !opts.includes(v)) opts.push(v);
    return `<select ${nm} data-current="${esc(v)}">${opts.map((o) =>
      `<option value="${esc(o)}" ${o === v ? "selected" : ""}>${esc(o === "" ? blank : o)}</option>`).join("")}</select>`;
  }
  if (f.type === "color") return `<input type="color" ${nm} value="${esc(v || "#ffffff")}">`;
  if (f.type === "number") {
    return `<input type="number" ${nm} value="${esc(v)}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""}
      step="${f.step || 1}" placeholder="${blank !== null ? esc(blank) : ""}">`;
  }
  if (f.type === "signed") {
    if (blank !== null) return `<input ${nm} value="${esc(v)}" placeholder="${esc(blank)}, e.g. ${esc(f.default || "")}">`;
    const n = signedNum(v);
    return `<span class="range"><input type="range" ${nm} data-unit="${esc(f.unit)}" min="${f.min}" max="${f.max}" value="${n}">
      <output>${esc(fmtSigned(n, f.unit))}</output></span>`;
  }
  return `<input ${nm} value="${esc(v)}" placeholder="${blank !== null ? esc(blank) : ""}">`;
}

// the form's values as setting strings ("true"/"false", "+8%", "#RRGGBB"...)
function readFields(form, fields) {
  const out = {};
  for (const f of fields) {
    const el = form.elements[f.key];
    if (!el) continue;
    if (f.type === "bool" && el.type === "checkbox") out[f.key] = el.checked ? "true" : "false";
    else if (f.type === "signed" && el.type === "range") out[f.key] = fmtSigned(Number(el.value), el.dataset.unit);
    else if (f.type === "signed") out[f.key] = el.value.trim() ? fmtSigned(signedNum(el.value), f.unit) : "";
    else out[f.key] = el.value;
  }
  return out;
}

// voice language -> voice gender -> voice: the voice list follows the first two choices
async function wireVoicePickers(root, blank = null) {
  const loc = root.querySelector('[name="VOICE_LOCALE"]');
  const gender = root.querySelector('[name="VOICE_GENDER"]');
  const voice = root.querySelector('[name="EDGE_VOICE"]');
  if (!voice) return;
  const voices = await loadVoices();
  if (!voices.length) return;
  if (loc) {
    const cur = loc.dataset.current || loc.value;
    const locales = [...new Set(voices.map((v) => v.locale))].sort();
    loc.innerHTML = (blank !== null ? `<option value="">${esc(blank)}</option>` : "") +
      locales.map((l) => `<option ${l === cur ? "selected" : ""}>${esc(l)}</option>`).join("");
  }
  const fill = () => {
    const cur = voice.value || voice.dataset.current;
    const l = loc?.value || (health?.voice || "en-US").split("-").slice(0, 2).join("-");
    const g = gender?.value || "Any";
    const list = voices.filter((v) => v.locale === l && (g === "Any" || !g || v.gender === g));
    voice.innerHTML = (blank !== null ? `<option value="">${esc(blank)}</option>` : "") +
      list.map((v) => `<option value="${esc(v.name)}" ${v.name === cur ? "selected" : ""}>${esc(v.name.replace(/^[a-z]{2}-[A-Z]{2}-/, ""))} · ${esc(v.gender)}</option>`).join("");
    if (blank === null && !list.some((v) => v.name === cur) && list[0]) voice.value = list[0].name;
  };
  loc?.addEventListener("change", fill);
  gender?.addEventListener("change", fill);
  fill();
}

async function renderSettings() {
  health = await api("/api/health");
  const { fields } = await api("/api/settings");
  const tg = await api("/api/notify").catch(() => ({}));
  const groups = [...new Set(fields.map((f) => f.group))];
  const source = (f) => f.source === "studio"
    ? `<button type="button" class="chip" data-reset="${esc(f.key)}" title="Back to the .env value: ${esc(f.env_value)}">changed here ↺</button>`
    : `<span class="muted" style="font-size:12px">${f.source === ".env" ? ".env" : "default"}</span>`;
  const extra = {
    Voice: `<div class="actions voice-row"><button type="button" class="btn btn-outline" id="voice-preview">▶ Listen</button>
      <input id="voice-sample" value="Here's the math. A used van costs about eight thousand dollars.">
      <audio id="voice-audio" controls hidden></audio></div>`,
    Captions: `<div class="actions"><button type="button" class="btn btn-outline" data-preview-captions>🖼 Preview the look</button></div>`,
    Picture: `<div class="actions"><button type="button" class="btn btn-outline" data-preview-captions>🖼 Preview the look</button></div>`,
    Notifications: `<div class="tg-box">
      <h3 style="margin:0 0 6px">Telegram bot ${tg.configured ? `<span class="chip done">connected${tg.chat ? " · " + esc(tg.chat) : ""}</span>` : ""}</h3>
      <p class="muted">1) In Telegram open <b>@BotFather</b>, send <code>/newbot</code>, copy the token it gives you.
        2) Paste it here and press Save. 3) Open your new bot${tg.bot ? ` (<b>${esc(tg.bot)}</b>)` : ""}, press <b>Start</b>,
        then press “Find my chat”. The token stays on this computer (<code>data\\accounts\\telegram.json</code>).</p>
      <div class="actions"><input id="tg-token" type="password" autocomplete="off" style="flex:1;min-width:220px"
          placeholder="${tg.has_token ? "token saved — paste a new one to replace it" : "123456789:AA…"}">
        <button type="button" class="btn btn-outline" id="tg-save">Save token</button>
        <button type="button" class="btn btn-outline" id="tg-find" ${tg.has_token ? "" : "disabled"}>Find my chat</button>
        <button type="button" class="btn btn-outline" id="tg-test" ${tg.configured ? "" : "disabled"}>Send test</button>
        ${tg.has_token ? `<button type="button" class="btn btn-text" id="tg-forget">Disconnect</button>` : ""}</div></div>`,
  };
  view.innerHTML = `<h1>Settings</h1>
    <p class="muted">Saved here, these win over the <code>.env</code> file and apply to the next video — no restart.
      API keys stay in <code>.env</code> on purpose. Every new video can also override them in ＋ CREATE → Options.</p>
    <form id="settings-form">
      ${groups.map((g) => `<section class="card settings-group"><h2>${esc(g)}</h2>
        <div class="settings-grid">${fields.filter((f) => f.group === g).map((f) => `
          <label class="setting"><span>${esc(f.label)} ${source(f)}</span>${fieldInput(f, f.value)}</label>`).join("")}</div>
        ${extra[g] || ""}</section>`).join("")}
      <div class="save-bar"><button class="btn btn-primary">Save settings</button>
        <span class="muted">Unsaved changes are only used by the Listen / Preview buttons.</span></div>
    </form>
    <dialog id="preview-dialog"><img id="preview-img" alt="caption preview" style="max-height:80vh;display:block">
      <form method="dialog" class="dialog-actions"><button class="btn btn-primary">Close</button></form></dialog>
    <section class="card" style="margin-top:16px">
      <h2>Script rules</h2>
      <p class="muted">How every script is built: hook → setup → middle → payoff → loop back into the hook.
        The AI reads this before each new video. Lines starting with <code>#</code> are notes for you only.
        ${health.next_part ? `Next video: <b>${esc(health.next_part)}</b>.` : ""}</p>
      <form id="rules-form">
        <textarea name="rules" rows="26" spellcheck="false" style="font-family:ui-monospace,Consolas,monospace;font-size:13px">Loading…</textarea>
        <div class="actions">
          <button class="btn btn-primary">Save rules</button>
          <button class="btn btn-outline" type="button" id="rules-reset">Restore default</button>
        </div>
      </form></section>
    <section class="card" style="margin-top:16px"><h2>System</h2><div class="kv">
      <div>Renderer (FFmpeg)</div><div>${health.ok ? '<span class="ok">✓</span> ' : '<span class="bad">✗</span> '}${esc(health.ffmpeg || "missing")}</div>
      <div>Gemini API key</div><div>${health.gemini_key ? '<span class="ok">✓</span> set' : '<span class="bad">✗</span> not set — GEMINI_API_KEY in .env'}</div>
      <div>Mode</div><div>${health.mock ? "TEST — free, no Gemini" : "Live"}</div>
      <div>YT skills folder</div><div>${health.skills_found ? '<span class="ok">✓</span> found' : '<span class="bad">✗</span> not found — SKILLS_DIR in .env'}</div>
      <div>Voice profile (voice.md)</div><div>${health.voice_profile_filled ? '<span class="ok">✓</span> filled' : "empty template"}</div>
      <div>Videos made</div><div>${esc(health.videos_made)}</div></div></section>`;

  const form = $("#settings-form");
  form.querySelectorAll('input[type="range"]').forEach((r) => r.addEventListener("input", () => {
    r.nextElementSibling.textContent = fmtSigned(Number(r.value), r.dataset.unit);
  }));
  wireVoicePickers(form);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const now = readFields(form, fields);
    const changes = Object.fromEntries(Object.entries(now).filter(([k, v]) => v !== (fields.find((f) => f.key === k)?.value ?? "")));
    if (!Object.keys(changes).length) return toast("Nothing changed");
    try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ changes }) }); toast(`Saved ${Object.keys(changes).length} setting(s)`); renderSettings(); loadHealth(); }
    catch (e) { toast(e.message, true); }
  });
  form.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", async () => {
    try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ changes: { [b.dataset.reset]: null } }) }); toast("Back to the .env value"); renderSettings(); }
    catch (e) { toast(e.message, true); }
  }));
  $("#voice-preview").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      const res = await fetch("/api/preview/voice", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: $("#voice-sample").value, options: readFields(form, fields) }) });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const audio = $("#voice-audio");
      audio.src = URL.createObjectURL(await res.blob());
      audio.hidden = false;
      audio.play();
    } catch (e) { toast(e.message, true); }
    ev.target.disabled = false;
  });
  form.querySelectorAll("[data-preview-captions]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const res = await fetch("/api/preview/captions", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ options: readFields(form, fields) }) });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      $("#preview-img").src = URL.createObjectURL(await res.blob());
      $("#preview-dialog").showModal();
    } catch (e) { toast(e.message, true); }
    b.disabled = false;
  }));

  const tgAction = async (btn, fn, ok) => {
    btn.disabled = true;
    try { await fn(); toast(ok); renderSettings(); } catch (e) { toast(e.message, true); btn.disabled = false; }
  };
  $("#tg-save")?.addEventListener("click", (ev) => {
    const token = $("#tg-token").value.trim();
    if (!token) return toast("Paste the token from @BotFather", true);
    tgAction(ev.target, () => api("/api/notify", { method: "PUT", body: JSON.stringify({ bot_token: token }) }),
      "Token saved — now open the bot, press Start, then “Find my chat”");
  });
  $("#tg-find")?.addEventListener("click", (ev) => tgAction(ev.target, () => api("/api/notify/find-chat", { method: "POST" }), "Chat found — press “Send test”"));
  $("#tg-test")?.addEventListener("click", (ev) => tgAction(ev.target, () => api("/api/notify/test", { method: "POST" }), "Test message sent — check Telegram"));
  $("#tg-forget")?.addEventListener("click", (ev) => {
    if (confirm("Disconnect the Telegram bot? No more alerts until you connect it again.")) tgAction(ev.target, () => api("/api/notify", { method: "DELETE" }), "Disconnected");
  });

  const rules = $("#rules-form");
  const { rules: text, default: fallback } = await api("/api/script-rules");
  rules.rules.value = text;
  rules.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { rules.rules.value = (await api("/api/script-rules", { method: "PUT", body: JSON.stringify({ rules: rules.rules.value }) })).rules; toast("Rules saved — the next video uses them"); }
    catch (e) { toast(e.message, true); }
  });
  $("#rules-reset").addEventListener("click", () => {
    if (confirm("Replace your rules with the default ones? (Press Save afterwards to keep them.)")) rules.rules.value = fallback;
  });
}

// ------------------------------------------------------------------ accounts

async function renderAccounts(query) {
  const list = await api("/api/accounts");
  const q = new URLSearchParams(query || "");
  const done = q.get("connected"), err = q.get("error");
  view.innerHTML = `<h1>Accounts</h1>
    <p class="muted">Log in to each platform once. The Studio shows who is logged in, so you always see where a video goes.
      Logins stay on this computer (<code>data\\accounts</code>). n8n and the Publish button post to these accounts.</p>
    ${err ? `<div class="error-box">${esc(err)}</div>` : ""}
    ${done ? `<div class="card ok-box">✓ ${esc(list.find((a) => a.id === done)?.name || done)} connected.</div>` : ""}
    <div class="acct-grid">${list.map(accountCard).join("")}</div>`;
  if (done || err) history.replaceState(null, "", "#/accounts");

  view.querySelectorAll("[data-logout]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Log out of this account? Videos will not be posted there until you connect again.")) return;
    try { await api(`/api/accounts/${b.dataset.logout}/logout`, { method: "POST" }); toast("Logged out"); renderAccounts(); }
    catch (e) { toast(e.message, true); }
  }));
  view.querySelectorAll("form[data-app]").forEach((f) => f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api(`/api/accounts/${f.dataset.app}/app`, { method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(f))) });
      toast("App keys saved — now press Connect"); renderAccounts();
    } catch (e) { toast(e.message, true); }
  }));
  view.querySelectorAll("select[data-page]").forEach((s) => s.addEventListener("change", async () => {
    try { await api(`/api/accounts/${s.dataset.page}/page`, { method: "POST", body: JSON.stringify({ page_id: s.value }) }); toast("Page chosen"); renderAccounts(); }
    catch (e) { toast(e.message, true); }
  }));
  view.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied"); } catch { prompt("Copy:", b.dataset.copy); }
  }));
}

function accountCard(a) {
  const who = a.connected && a.account
    ? `<div class="vid">${a.account.avatar ? `<img class="acct-avatar" src="${esc(a.account.avatar)}" alt="">` : `<div class="acct-avatar ph">👤</div>`}
        <div><b>${a.account.url ? `<a href="${esc(a.account.url)}" target="_blank" rel="noopener">${esc(a.account.name)}</a>` : esc(a.account.name)}</b>
        <div class="muted">connected ${when(a.connected_at)}</div></div></div>`
    : `<div class="muted">Not connected</div>`;
  const pages = a.pages.length > 1 ? `<label>Post to Page<select data-page="${esc(a.id)}">${a.pages.map((p) =>
    `<option value="${esc(p.id)}" ${p.id === a.page_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>` : "";
  const buttons = a.connected
    ? `<a class="btn btn-outline" href="/oauth/${esc(a.id)}/start" title="Log in with another account">⇄ Switch account</a>
       <button class="btn btn-danger" data-logout="${esc(a.id)}">Log out</button>`
    : a.app_configured ? `<a class="btn btn-primary" href="/oauth/${esc(a.id)}/start">Connect</a>`
      : `<span class="muted">Add the app keys below first</span>`;
  return `<section class="card acct">
    <h2>${esc(a.name)} ${a.connected ? '<span class="chip done">connected</span>' : ""}</h2>
    ${who}${pages}
    ${a.needs_public_url && !a.public_url ? `<div class="muted" style="margin-top:8px">⚠ needs PUBLIC_BASE_URL (a public https link to this Studio) to post.</div>` : ""}
    <div class="actions">${buttons}</div>
    <details ${a.app_configured ? "" : "open"}><summary>App keys ${a.app_configured ? "✓" : ""}</summary>
      <p class="muted">${esc(a.how)} <a href="${esc(a.dev_url)}" target="_blank" rel="noopener">Open developer site ↗</a></p>
      <label>Redirect URI <span class="muted">(paste it into the app settings)</span>
        <span class="actions" style="margin:0"><input value="${esc(a.redirect_uri)}" readonly style="flex:1">
        <button type="button" class="btn btn-outline" data-copy="${esc(a.redirect_uri)}">Copy</button></span></label>
      <form data-app="${esc(a.id)}">
        <label>${a.id === "tiktok" ? "Client key" : "Client ID / App ID"}<input name="client_id" value="${esc(a.client_id)}" required autocomplete="off"></label>
        <label>${a.id === "tiktok" ? "Client secret" : "Client secret / App secret"}
          <input name="client_secret" type="password" autocomplete="off" placeholder="${a.app_configured ? "saved — leave empty to keep" : ""}" ${a.app_configured ? "" : "required"}></label>
        <button class="btn btn-outline">Save app keys</button>
      </form>
    </details></section>`;
}

// ------------------------------------------------------------------ earnings

const usd = (v) => (v == null ? "—" : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const num = (v) => (v == null ? "—" : Number(v).toLocaleString());
const CHART = { views: "#2f8fe0", money: "#2ba640" }; // validated against the #282828 panel (dataviz validator)

// one-series bar chart: thin rounded bars, 2px gaps, recessive axis, hover tooltip per bar
function barChart(rows, { value, label, color, fmt, height = 180 }) {
  if (!rows.length) return `<div class="empty">No data yet.</div>`;
  const max = Math.max(...rows.map(value), 0) || 1;
  const w = 100 / rows.length;
  const bars = rows.map((r, i) => {
    const v = value(r);
    const h = Math.max(v > 0 ? 2 : 0, (v / max) * (height - 24));
    return `<g class="bar" data-tip="${esc(label(r))}: ${esc(fmt(v))}">
      <rect x="${i * w}%" y="0" width="${w}%" height="${height - 20}" fill="transparent"></rect>
      <svg x="${i * w}%" y="0" width="${w}%" height="${height - 20}" overflow="visible">
        <rect x="1" y="${height - 20 - h}" width="calc(100% - 2px)" height="${h}" rx="4" fill="${color}"></rect></svg></g>`;
  }).join("");
  const first = label(rows[0]), last = label(rows[rows.length - 1]);
  return `<div class="chart"><div class="chart-max muted">${esc(fmt(max))}</div>
    <svg width="100%" height="${height}" role="img" aria-label="bar chart">${bars}
      <line x1="0" x2="100%" y1="${height - 20}" y2="${height - 20}" stroke="var(--line)"></line>
      <text x="0" y="${height - 4}" class="axis">${esc(first)}</text>
      <text x="100%" y="${height - 4}" text-anchor="end" class="axis">${esc(last)}</text></svg>
    <div class="chart-tip" hidden></div></div>`;
}

function wireCharts(root) {
  root.querySelectorAll(".chart").forEach((c) => {
    const tip = c.querySelector(".chart-tip");
    c.querySelectorAll(".bar").forEach((b) => {
      b.addEventListener("mousemove", (e) => {
        const box = c.getBoundingClientRect();
        tip.textContent = b.dataset.tip;
        tip.hidden = false;
        tip.style.left = `${Math.min(e.clientX - box.left + 12, box.width - 160)}px`;
        tip.style.top = `${e.clientY - box.top - 34}px`;
      });
      b.addEventListener("mouseleave", () => { tip.hidden = true; });
    });
  });
}

function progress(label, value, goal, fmt = num) {
  const pct = value == null ? 0 : Math.min(100, (value / goal) * 100);
  return `<div class="progress"><div class="progress-head"><span>${esc(label)}</span>
    <span class="muted">${value == null ? "—" : esc(fmt(value))} / ${esc(fmt(goal))}</span></div>
    <div class="progress-track"><div class="progress-fill" style="width:${pct.toFixed(1)}%"></div></div></div>`;
}

let earningsRefreshing = false;

async function renderEarnings() {
  const e = await api("/api/earnings");
  const yt = e.youtube;
  const anyConnected = Object.values(e.connected).some(Boolean);
  const old = !e.refreshed_at || Date.now() - new Date(e.refreshed_at).getTime() > 6 * 3600 * 1000;
  const m = e.month;
  const fixed = e.monthly_costs || 0;
  const tile = (label, value, hint = "") => `<div class="tile"><div class="stat">${value}</div><div class="stat-label">${esc(label)}</div>${hint ? `<div class="muted" style="font-size:12px">${hint}</div>` : ""}</div>`;
  const jobs = await api("/api/jobs").catch(() => []);

  view.innerHTML = `<h1>Earnings</h1><div class="earn">
    <div class="actions earn-bar">
      <button class="btn btn-outline" id="earn-refresh" ${anyConnected ? "" : "disabled"}>↻ Refresh numbers</button>
      <span class="muted">${e.refreshed_at ? `updated ${when(e.refreshed_at)}` : "never updated"} · numbers come from the connected accounts</span></div>
    ${!e.connected.youtube ? `<div class="card ok-box warn-box">Connect YouTube in <a href="#/accounts">Accounts</a> to see views, subscribers and revenue. Income you add by hand works without it.</div>` : ""}
    ${Object.entries(e.errors || {}).map(([p, msg]) => `<div class="error-box"><b>${esc(p)}:</b> ${esc(msg)}</div>`).join("")}

    <section class="card"><h2>This month · ${esc(m.month)}</h2><div class="stats stats-wide">
      ${tile("Income", usd(m.income), m.manual ? `${usd(m.youtube)} YouTube + ${usd(m.manual)} added by you` : "")}
      ${tile("Costs", usd(m.costs), `AI ${usd(Math.max(0, m.costs - fixed))} + fixed ${usd(fixed)}`)}
      ${tile("Profit", usd(m.profit))}
      ${tile("Videos made", num(m.videos))}
      ${tile("Profit, 6 months", usd(e.six_months.profit))}
    </div></section>

    <div class="grid">
      <section class="card"><h2>YouTube</h2><div class="stats">
        ${tile("Subscribers", num(yt.subscribers), yt.subs_30d ? `${yt.subs_30d > 0 ? "+" : ""}${num(yt.subs_30d)} in 30 days` : "")}
        ${tile("Views, 30 days", num(yt.views_30d))}
        ${tile("Revenue, 30 days", yt.revenue_available ? usd(yt.revenue_30d) : "—", yt.revenue_available === false ? "not monetized yet" : "")}
        ${tile("RPM", yt.rpm != null ? usd(yt.rpm) : "—", "revenue per 1,000 views")}
      </div></section>
      <section class="card"><h2>Monetization progress</h2>
        ${e.ypp.map((t) => `<h3 style="margin:12px 0 6px">${esc(t.name)} ${t.met ? '<span class="chip done">reached</span>' : ""}</h3>
          ${progress("Subscribers", yt.subscribers, t.subs)}
          ${progress("Shorts views, 90 days", yt.shorts_views_90d, t.shorts_views_90d)}
          <div class="muted" style="font-size:12px;margin:-4px 0 6px">or ${num(t.watch_hours)} watch hours in 12 months (now ${num(yt.watch_hours_365d)})</div>`).join("")}
        <p class="muted" style="font-size:12px">YouTube Partner Program thresholds as published in 2025 — YouTube can change them.</p></section>
    </div>

    <div class="grid">
      <section class="card"><h2>YouTube views per day · last 30 days</h2>
        ${barChart(yt.daily || [], { value: (r) => r.views, label: (r) => r.date, color: CHART.views, fmt: num })}</section>
      <section class="card"><h2>Income per month · last 6 months</h2>
        ${barChart(e.months, { value: (r) => r.income, label: (r) => r.month, color: CHART.money, fmt: usd })}
        <table class="compact"><thead><tr><th>Month</th><th>Income</th><th>Costs</th><th>Profit</th><th>Videos</th></tr></thead>
          <tbody>${e.months.slice().reverse().map((x) => `<tr><td>${esc(x.month)}</td><td>${usd(x.income)}</td><td>${usd(x.costs)}</td>
            <td class="${x.profit < 0 ? "neg" : ""}">${usd(x.profit)}</td><td>${num(x.videos)}</td></tr>`).join("")}</tbody></table></section>
    </div>

    <section class="card wide"><h2>Videos by views</h2>
      ${e.top.length ? `<table><thead><tr><th>Video</th><th>Views</th><th>Likes</th><th>Revenue</th><th>Where</th></tr></thead><tbody>
        ${e.top.map((t) => `<tr class="row" data-href="#/video/${esc(t.job_id)}"><td>${esc(t.title)}</td><td>${num(t.views)}</td><td>${num(t.likes)}</td>
          <td>${t.revenue ? usd(t.revenue) : "—"}</td><td>${Object.entries(t.platforms).map(([p, s]) =>
            s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(p)}</a> ${s.views != null ? num(s.views) : ""}` : `${esc(p)} ${s.views != null ? num(s.views) : ""}`).join(" · ")}</td></tr>`).join("")}
      </tbody></table>` : `<div class="empty">Nothing posted yet — publish a video and press Refresh.</div>`}</section>

    <section class="card wide"><h2>Income you add yourself</h2>
      <p class="muted">Sponsors, affiliate commissions, TikTok Creator Rewards, payouts — anything the APIs don't report. Link it to a video to see what each video earns.</p>
      <form id="income-form" class="income-form">
        <label>Date<input type="date" name="date" value="${new Date().toISOString().slice(0, 10)}" required></label>
        <label>Source<select name="source">${e.sources.map((s) => `<option>${esc(s)}</option>`).join("")}</select></label>
        <label>Amount, $<input type="number" name="amount" step="0.01" required></label>
        <label>Video <span class="muted">(optional)</span><select name="job_id"><option value="">—</option>
          ${jobs.filter((j) => j.state === "done").map((j) => `<option value="${esc(j.job_id)}">${esc(j.title)}</option>`).join("")}</select></label>
        <label>Note<input name="note" maxlength="300"></label>
        <button class="btn btn-primary">Add</button>
      </form>
      ${e.income.length ? `<table class="compact"><thead><tr><th>Date</th><th>Source</th><th>Amount</th><th>Note</th><th></th></tr></thead><tbody>
        ${e.income.map((x) => `<tr><td>${esc(x.date)}</td><td>${esc(x.source)}</td><td>${usd(x.amount)}</td><td>${esc(x.note)}</td>
          <td><button class="btn btn-text" data-del-income="${esc(x.id)}" title="Delete">✕</button></td></tr>`).join("")}</tbody></table>` : ""}
    </section>

    <section class="card wide"><h2>Ways this channel can earn</h2>
      <ul class="tips">
        <li><b>Your own link in every description</b> — newsletter, affiliate or a product: Settings → Monetization → “Line added to every description”. Works from day one.</li>
        <li><b>YouTube Partner Program</b> — Shorts ad revenue after the thresholds above; RPM appears here automatically.</li>
        <li><b>Affiliate links per business</b> — equipment from the video (vending machines, pressure washers): log commissions above to see which topics pay.</li>
        <li><b>Sponsors</b> — small-business tools (invoicing, LLC filing, insurance) fit this niche; log each deal above.</li>
        <li><b>Digital product</b> — a “365 businesses” spreadsheet or startup checklists, linked from the description.</li>
      </ul></section></div>`;

  wireCharts(view);
  view.querySelectorAll("tr.row").forEach((r) => r.addEventListener("click", (ev) => { if (!ev.target.closest("a")) location.hash = r.dataset.href; }));
  const doRefresh = async () => {
    earningsRefreshing = true;
    $("#earn-refresh").disabled = true;
    $("#earn-refresh").textContent = "↻ Updating…";
    try { await api("/api/earnings/refresh", { method: "POST" }); toast("Numbers updated"); }
    catch (err) { toast(err.message, true); }
    earningsRefreshing = false;
    if (location.hash.startsWith("#/earnings")) renderEarnings();
  };
  $("#earn-refresh").addEventListener("click", doRefresh);
  if (old && anyConnected && !earningsRefreshing) doRefresh();
  $("#income-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    try {
      await api("/api/earnings/income", { method: "POST", body: JSON.stringify({ ...f, amount: Number(f.amount), job_id: f.job_id || null }) });
      toast("Added"); renderEarnings();
    } catch (err) { toast(err.message, true); }
  });
  view.querySelectorAll("[data-del-income]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this income entry?")) return;
    try { await api(`/api/earnings/income/${b.dataset.delIncome}`, { method: "DELETE" }); renderEarnings(); }
    catch (err) { toast(err.message, true); }
  }));
}

// ------------------------------------------------------------------ topics queue

let topicFilter = "todo";

function topicCheck(c) {
  if (!c || c.verdict === "ok") return "";
  return c.verdict === "doubt"
    ? ` <span class="chip error" title="${esc(c.note)}">doubtful</span> <span class="muted topic-note">${esc(c.note)}</span>`
    : ` <span class="chip done" title="${esc(c.note)}">corrected</span> <span class="muted topic-note">${esc(c.note)}</span>`;
}

async function renderTopics() {
  const t = await api("/api/topics");
  const shown = t.items.filter((x) => topicFilter === "all" || x.state === topicFilter);
  const canDrag = topicFilter === "todo";
  const first = t.items.find((x) => x.state === "todo");
  const STATE = { todo: "to film", used: "filmed", skipped: "skipped" };
  view.innerHTML = `<h1>Topics</h1>
    <p class="muted">The businesses still to film, in order. A video with an empty topic (＋ CREATE, or the daily n8n run)
      takes the first one; it is marked <b>filmed</b> when the video is finished. Drag rows to change the order.
      ${t.enabled ? "" : `<b>The queue is off</b> — Settings → Content → “Empty topic = take the next one from the Topics queue”.`}</p>
    <div class="grid">
      <section class="card"><h2>Queue</h2><div class="stats">
        <div><div class="stat">${t.todo}</div><div class="stat-label">To film</div></div>
        <div><div class="stat">${t.used}</div><div class="stat-label">Filmed</div></div>
        <div><div class="stat">${t.items.length - t.todo - t.used}</div><div class="stat-label">Skipped</div></div></div>
        ${first ? `<p>Next: <b>${esc(first.topic)}</b></p>
          <button class="btn btn-primary" id="topic-make">🎬 Make the next video now</button>` : ""}</section>
      <section class="card"><h2>Add topics</h2>
        <form id="topic-form"><textarea name="lines" rows="4" placeholder="One business per line, e.g.&#10;Bounce house rentals&#10;Mobile dog grooming"></textarea>
          <div class="actions"><button class="btn btn-primary">Add</button>
            <label class="check" style="margin:0"><input type="checkbox" name="top"> put them first</label></div></form>
        <div class="actions">
          <button class="btn btn-outline" id="topic-starter">📋 Load ~400 starter ideas</button>
          <button class="btn btn-outline" id="topic-suggest">✨ 30 fresh ideas from Gemini</button></div>
        <div class="actions"><button class="btn btn-outline" id="topic-check" ${t.unchecked ? "" : "disabled"}>✔ Check topics${t.unchecked ? ` (${t.unchecked} unchecked)` : " — all checked"}</button>
          ${t.doubt ? `<span class="chip error" title="Marked by the check — skip, rename or delete them">${t.doubt} doubtful</span>` : ""}</div>
        <p class="muted" style="font-size:12px;margin-bottom:0">Gemini fixes typos and wording and marks anything that isn't a real, legal, startable business. Fresh ideas are checked automatically.</p></section>
    </div>
    <section class="card wide" style="margin-top:16px">
      <div class="tabs" style="margin-top:0">${["todo", "used", "skipped", "all"].map((s) =>
        `<button class="tab ${topicFilter === s ? "active" : ""}" data-filter="${s}">${{ todo: "To film", used: "Filmed", skipped: "Skipped", all: "All" }[s]}</button>`).join("")}
        ${topicFilter === "used" && t.used ? `<button class="btn btn-text" id="topic-clear" style="margin-left:auto">Remove filmed from the list</button>` : ""}</div>
      ${shown.length ? `<ol class="topic-list">${shown.map((x, i) => `
        <li class="topic ${esc(x.state)}" data-id="${esc(x.id)}" ${canDrag ? 'draggable="true"' : ""}>
          ${canDrag ? '<span class="grip" title="Drag to reorder">⋮⋮</span>' : ""}
          <span class="topic-n">${i + 1}</span>
          <span class="topic-name">${esc(x.topic)}${topicCheck(x.check)}</span>
          ${topicFilter === "all" ? `<span class="chip ${x.state === "used" ? "done" : "queued"}">${STATE[x.state]}</span>` : ""}
          ${x.job_id ? `<a href="#/video/${esc(x.job_id)}">video →</a>` : ""}
          <span class="topic-actions">
            ${x.state === "todo" && i > 0 && canDrag ? `<button class="btn btn-text" data-top="${esc(x.id)}" title="Move to the top">⤒</button>` : ""}
            <button class="btn btn-text" data-edit="${esc(x.id)}" title="Rename">✎</button>
            ${x.state !== "used" ? `<button class="btn btn-text" data-state="used" data-for="${esc(x.id)}" title="Mark as filmed">✓</button>` : ""}
            ${x.state === "todo" ? `<button class="btn btn-text" data-state="skipped" data-for="${esc(x.id)}" title="Skip">⤼</button>`
              : `<button class="btn btn-text" data-state="todo" data-for="${esc(x.id)}" title="Back to the queue">↺</button>`}
            <button class="btn btn-text" data-del="${esc(x.id)}" title="Delete">✕</button></span></li>`).join("")}</ol>`
        : `<div class="empty">${topicFilter === "todo" ? "The queue is empty — add topics or load the starter list." : "Nothing here."}</div>`}
    </section>`;

  const busyBtn = async (btn, fn) => {
    btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message, true); }
    renderTopics();
  };
  view.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => { topicFilter = b.dataset.filter; renderTopics(); }));
  $("#topic-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const lines = f.lines.value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!lines.length) return;
    busyBtn(f.querySelector("button"), async () => {
      const r = await api("/api/topics", { method: "POST", body: JSON.stringify({ lines, top: f.top.checked }) });
      toast(`Added ${r.added}${r.skipped ? `, ${r.skipped} already in the list or filmed` : ""}`);
    });
  });
  $("#topic-starter").addEventListener("click", (ev) => busyBtn(ev.target, async () => {
    const r = await api("/api/topics/starter", { method: "POST" });
    toast(`Added ${r.added} ideas${r.skipped ? ` (${r.skipped} were already there)` : ""}`);
  }));
  $("#topic-suggest").addEventListener("click", (ev) => {
    ev.target.textContent = "✨ Asking Gemini…";
    busyBtn(ev.target, async () => {
      const r = await api("/api/topics/suggest", { method: "POST", body: JSON.stringify({ count: 30 }) });
      toast(`Added ${r.added} new ideas${r.dropped?.length ? ` · dropped ${r.dropped.length} doubtful: ${r.dropped.join("; ")}` : ""}`);
    });
  });
  $("#topic-check")?.addEventListener("click", async (ev) => {
    const btn = ev.target;
    btn.disabled = true;
    let fixed = 0, doubt = 0, done = 0;
    try {
      for (;;) {   // the server checks 80 at a time; keep going until none are left
        btn.textContent = `✔ Checking… ${done} done, ${t.unchecked - done > 0 ? t.unchecked - done : 0} left`;
        const r = await api("/api/topics/check", { method: "POST" });
        fixed += r.fixed; doubt += r.doubt; done += r.checked;
        if (!r.left || !r.checked) break;
      }
      toast(`Checked ${done}: ${fixed} corrected, ${doubt} doubtful`);
    } catch (e) { toast(e.message, true); }
    renderTopics();
  });
  $("#topic-make")?.addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      const r = await api("/api/jobs", { method: "POST", body: JSON.stringify({}) });
      toast("Started — follow the pipeline live");
      currentTab = "script";
      location.hash = `#/video/${r.job_id}`;
    } catch (e) { toast(e.message, true); ev.target.disabled = false; }
  });
  $("#topic-clear")?.addEventListener("click", (ev) => {
    if (confirm("Remove all filmed topics from the list? (The videos are not touched.)"))
      busyBtn(ev.target, () => api("/api/topics?state=used", { method: "DELETE" }));
  });
  view.querySelectorAll("[data-state]").forEach((b) => b.addEventListener("click", () =>
    busyBtn(b, () => api(`/api/topics/${b.dataset.for}`, { method: "PATCH", body: JSON.stringify({ state: b.dataset.state }) }))));
  view.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => {
    const cur = t.items.find((x) => x.id === b.dataset.edit);
    const name = prompt("Topic:", cur?.topic || "");
    if (name && name.trim()) busyBtn(b, () => api(`/api/topics/${b.dataset.edit}`, { method: "PATCH", body: JSON.stringify({ topic: name }) }));
  }));
  view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => {
    if (confirm("Delete this topic?")) busyBtn(b, () => api(`/api/topics/${b.dataset.del}`, { method: "DELETE" }));
  }));

  // drag and drop: the order of the visible 'to film' rows, then everything else as it was
  const list = view.querySelector(".topic-list");
  if (!canDrag || !list) return;
  const saveOrder = () => {
    const ids = [...list.querySelectorAll("li")].map((li) => li.dataset.id);
    const rest = t.items.filter((x) => !ids.includes(x.id)).map((x) => x.id);
    api("/api/topics/order", { method: "PUT", body: JSON.stringify({ ids: [...ids, ...rest] }) })
      .then(renderTopics).catch((e) => toast(e.message, true));
  };
  view.querySelectorAll("[data-top]").forEach((b) => b.addEventListener("click", () => {
    list.prepend(b.closest("li"));
    saveOrder();
  }));
  let dragged = null;
  list.querySelectorAll("li").forEach((li) => {
    li.addEventListener("dragstart", (e) => { dragged = li; li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
    li.addEventListener("dragend", () => { li.classList.remove("dragging"); if (dragged) saveOrder(); dragged = null; });
    li.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (!dragged || dragged === li) return;
      const box = li.getBoundingClientRect();
      li.parentNode.insertBefore(dragged, e.clientY > box.top + box.height / 2 ? li.nextSibling : li);
    });
  });
}

// ------------------------------------------------------------------ A/B results

const AB_STATE = { "not posted": "queued", collecting: "uploading", "too few views": "queued", decided: "done" };

async function renderAB() {
  const ab = await api("/api/ab");
  const board = (rows, what) => rows.length ? `<table class="compact"><thead><tr><th>${what}</th><th>Tests</th><th>Wins</th><th>Win rate</th><th>Avg % viewed</th><th>Avg views</th></tr></thead>
    <tbody>${rows.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.tests}</td><td>${r.wins}</td><td>${r.win_rate}%</td>
      <td>${r.retention ?? "—"}${r.retention != null ? "%" : ""}</td><td>${num(r.views)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">Appears after the first finished test.</div>`;
  const bar = (v) => v == null ? "—" : `<span class="ret"><span class="ret-fill" style="width:${Math.min(100, v)}%"></span></span> ${v}%`;
  view.innerHTML = `<h1>A/B tests</h1>
    <p class="muted">Make 2-5 versions of a video (＋ CREATE → Versions, or “Make versions” on a video), post them to YouTube,
      and after <b>${ab.days} days</b> with at least <b>${num(ab.min_views)} views</b> each, the version people watched the longest
      (average % viewed) wins. Numbers come from Earnings → Refresh (every 6 hours by itself).
      ${ab.learn ? "Winning hook types are fed into new scripts automatically." : "Learning from winners is off (Settings → Variants)."}</p>
    ${ab.advice ? `<div class="card ok-box">💡 ${esc(ab.advice)}</div>` : ""}
    <div class="grid">
      <section class="card"><h2>Hook types</h2>${board(ab.leaderboard.formula, "Hook formula")}</section>
      <section class="card"><h2>Voices</h2>${board(ab.leaderboard.voice, "Voice")}</section>
    </div>
    ${ab.groups.length ? ab.groups.map((g) => `<section class="card wide" style="margin-top:16px">
      <h2><a href="#/video/${esc(g.base)}">${esc(g.topic || g.base)}</a>
        <span class="chip ${AB_STATE[g.state] || "queued"}">${esc(g.state)}</span></h2>
      ${g.note ? `<p class="muted">${esc(g.note)}</p>` : ""}
      <table><thead><tr><th>Version</th><th>Hook</th><th>Voice</th><th>YouTube views</th><th>% viewed</th><th>Avg watch</th><th>Likes</th><th>Other platforms</th></tr></thead>
        <tbody>${g.members.map((m) => `<tr class="row ${m.job_id === g.winner ? "winner" : ""}" data-href="#/video/${esc(m.job_id)}">
          <td><span class="chip variant">${esc(m.letter)}</span>${m.job_id === g.winner ? " 🏆" : ""}</td>
          <td><div>${esc(m.hook)}</div><div class="muted">${esc(m.formula)}</div></td>
          <td>${esc((m.voice || "").replace(/^[a-z]{2}-[A-Z]{2}-/, "").replace("Neural", ""))}</td>
          <td>${m.youtube_id ? num(m.views) : '<span class="muted">not posted</span>'}</td>
          <td>${bar(m.retention)}</td>
          <td>${m.avg_seconds != null ? `${m.avg_seconds} s of ${fmtSec(m.seconds)}` : "—"}</td>
          <td>${num(m.likes)}</td>
          <td>${Object.entries(m.other_views || {}).map(([p, v]) => `${esc(p)} ${num(v)}`).join(" · ") || "—"}</td></tr>`).join("")}</tbody></table>
    </section>`).join("")
    : `<section class="card wide" style="margin-top:16px"><div class="empty">No tests yet. Open a finished video and press “Make versions”.</div></section>`}`;
}

// ------------------------------------------------------------------ router

async function route() {
  clearTimeout(timer);
  const [hash, query] = (location.hash || "#/").split("?");
  const [, page, id] = hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === (page || "dashboard") || (page === "video" && a.dataset.nav === "content")));
  try {
    if (page === "content") await renderContent();
    else if (page === "video" && id) await renderVideo(decodeURIComponent(id));
    else if (page === "settings") await renderSettings();
    else if (page === "accounts") await renderAccounts(query);
    else if (page === "earnings") await renderEarnings();
    else if (page === "topics") await renderTopics();
    else if (page === "ab") await renderAB();
    else if (page === "channels") await renderChannels();
    else if (page === "account") await renderAccount();
    else if (page === "users") await renderUsers();
    else if (me && !me.channel) { location.hash = "#/channels"; return; }
    else await renderDashboard();
  } catch (e) {
    view.innerHTML = `<div class="error-box">Cannot load: ${esc(e.message)}<br>Is the worker container running? (docker compose ps)</div>`;
    poll(route, 5000);
  }
}

// ------------------------------------------------------------------ accounts and channels

let me = null;

async function loadMe() {
  me = await api("/api/me");
  const pick = $("#channel-pick");
  pick.innerHTML = me.channels.length
    ? me.channels.map((c) => `<option value="${esc(c.id)}" ${c.id === me.channel ? "selected" : ""}>${esc(c.name)}</option>`).join("")
    : `<option value="">No channel yet</option>`;
  $("#user-email").textContent = me.user.email;
  $("#user-btn").textContent = (me.user.email[0] || "●").toUpperCase();
  $("#users-link").hidden = !isAdmin(me.user);
  $("#nav-users").hidden = !isAdmin(me.user);
}

$("#channel-pick").addEventListener("change", async (e) => {
  if (!e.target.value) return;
  try {
    await api(`/api/channels/${encodeURIComponent(e.target.value)}/open`, { method: "POST" });
    await loadMe();
    loadHealth();
    if (location.hash.startsWith("#/video")) location.hash = "#/content";
    else route();
  } catch (err) { toast(err.message, true); }
});

async function renderChannels() {
  await loadMe();
  const tpl = Object.entries(me.templates).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  view.innerHTML = `
    <h1>Channels</h1>
    <p class="muted">Each channel has its own YouTube and Telegram logins, settings, topics, videos and earnings.
      The AI keys, the music and the daily FLUX picture limit are shared.</p>
    ${me.channels.length ? "" : `<div class="ok-box">Create your first channel to start.</div>`}
    <div class="card"><h2>New channel</h2>
      <form id="channel-form" class="actions channel-form">
        <input name="name" placeholder="Channel name, e.g. Daily Fit Habits" maxlength="60" required>
        <select name="template">${tpl}</select>
        <button class="btn btn-primary">Create</button>
      </form>
      <p class="muted small">The template fills the niche, series, voice, script rules and a starter list of topics.
        Everything can be changed later in Settings and Topics.</p>
    </div>
    ${me.channels.map((c) => `
      <div class="card channel-card" data-id="${esc(c.id)}">
        <div class="actions">
          <input class="ch-name" value="${esc(c.name)}" maxlength="60">
          <button class="btn btn-outline" data-ch="rename">Rename</button>
          ${c.id === me.channel ? `<span class="chip done">open</span>` : `<button class="btn btn-outline" data-ch="open">Open</button>`}
        </div>
        <label class="check"><input type="checkbox" data-ch="daily" ${c.daily !== false ? "checked" : ""}>
          Make a video every day at 9:00 (n8n daily run)</label>
        <p class="muted small">Template: ${esc(me.templates[c.template] || c.template || "—")} · created ${esc(when(c.created))}</p>
        <details><summary class="muted">Delete this channel…</summary>
          <p class="muted small">Deletes all its videos, topics, settings, logins and earnings from this computer.
            Type the channel name to confirm.</p>
          <div class="actions"><input class="ch-confirm" placeholder="${esc(c.name)}">
            <button class="btn btn-danger" data-ch="delete">Delete channel</button></div>
        </details>
      </div>`).join("")}`;

  $("#channel-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const btn = f.querySelector("button");
    btn.disabled = true;
    try {
      const c = await api("/api/channels", { method: "POST", body: JSON.stringify({ name: f.name.value, template: f.template.value }) });
      toast(`Channel “${c.name}” created`);
      await loadMe();
      loadHealth();
      location.hash = "#/topics";
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });

  view.querySelectorAll(".channel-card").forEach((card) => {
    const id = card.dataset.id;
    const url = `/api/channels/${encodeURIComponent(id)}`;
    card.addEventListener("click", async (e) => {
      const act = e.target.dataset.ch;
      try {
        if (act === "rename") {
          await api(url, { method: "PATCH", body: JSON.stringify({ name: card.querySelector(".ch-name").value }) });
          toast("Renamed");
        } else if (act === "open") {
          await api(`${url}/open`, { method: "POST" });
          location.hash = "#/";
        } else if (act === "delete") {
          const confirm = card.querySelector(".ch-confirm").value;
          await api(`${url}?confirm=${encodeURIComponent(confirm)}`, { method: "DELETE" });
          toast("Channel deleted");
        } else return;
        await loadMe();
        loadHealth();
        if (act !== "open") renderChannels();
      } catch (err) { toast(err.message, true); }
    });
    card.querySelector("[data-ch=daily]").addEventListener("change", async (e) => {
      try {
        await api(url, { method: "PATCH", body: JSON.stringify({ daily: e.target.checked }) });
        toast(e.target.checked ? "Daily video on" : "Daily video off");
      } catch (err) { toast(err.message, true); }
    });
  });
}

async function renderAccount() {
  await loadMe();
  const u = me.user;
  view.innerHTML = `
    <h1>My account</h1>
    <div class="card"><h2>${esc(u.email)}</h2>
      <p class="muted">${esc(ROLE_NAME[u.role] || "User")} · since ${esc(when(u.created))}</p>
      <label class="check"><input type="checkbox" id="acc-marketing" ${u.marketing ? "checked" : ""}>
        Send me news, tips and offers about Vid Studio by e-mail</label>
    </div>
    <div class="card"><h2>Change password</h2>
      <form id="pw-form" class="settings-form">
        <label>Current password<input name="old" type="password" autocomplete="current-password" required></label>
        <label>New password <span class="muted">(at least 8 characters)</span>
          <input name="new" type="password" autocomplete="new-password" minlength="8" required></label>
        <div class="actions"><button class="btn btn-primary">Change password</button></div>
      </form>
    </div>
    <div class="actions"><button class="btn btn-outline" data-action="logout">↪ Sign out</button></div>`;
  $("#acc-marketing").addEventListener("change", async (e) => {
    try {
      await api("/api/me/marketing", { method: "PUT", body: JSON.stringify({ marketing: e.target.checked }) });
      toast(e.target.checked ? "Subscribed" : "Unsubscribed");
    } catch (err) { toast(err.message, true); }
  });
  $("#pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/me/password", { method: "PUT", body: JSON.stringify({ old: e.target.old.value, new: e.target.new.value }) });
      e.target.reset();
      toast("Password changed - other devices were signed out");
    } catch (err) { toast(err.message, true); }
  });
}

const ROLE_NAME = { superadmin: "Super admin", owner: "Studio owner", user: "User" };
const isAdmin = (u) => u && (u.role === "superadmin" || u.role === "owner");

async function renderUsers(query = renderUsers.q || "") {
  await loadMe();
  if (!isAdmin(me.user)) { view.innerHTML = `<div class="error-box">Only the Studio owner or a super admin sees this page.</div>`; return; }
  renderUsers.q = query;
  const users = await api(`/api/users?q=${encodeURIComponent(query)}`);
  const subs = users.filter((u) => u.marketing && !u.disabled).length;
  view.innerHTML = `
    <h1>Users</h1>
    <div class="actions users-bar">
      <input id="user-search" type="search" placeholder="Search by e-mail or ID" value="${esc(query)}">
      <a class="btn btn-outline" href="/api/users/marketing.csv">⬇ Subscribers CSV</a>
    </div>
    <p class="muted small">${users.length} account(s)${query ? ` matching “${esc(query)}”` : ""} ·
      ${subs} agreed to news and offers (the CSV has only them, with the date they agreed).</p>
    <div class="card"><table class="table users-table">
      <thead><tr><th>E-mail / ID</th><th>Role</th><th>Channels</th><th>News</th><th>Last sign-in</th><th>Signed in</th><th></th></tr></thead>
      <tbody>${users.map((u) => `
        <tr data-id="${esc(u.id)}">
          <td><div>${esc(u.email)}${u.disabled ? ` <span class="chip error">blocked</span>` : ""}${u.id === me.user.id ? ` <span class="chip done">you</span>` : ""}</div>
            <code class="user-id" title="Click to copy">${esc(u.id)}</code></td>
          <td>${esc(ROLE_NAME[u.role] || u.role)}</td>
          <td>${u.channels.length ? u.channels.map(esc).join("<br>") : "—"}</td>
          <td>${u.marketing ? "✓ " + esc(when(u.marketing_at)) : "—"}</td>
          <td>${u.last_login ? esc(when(u.last_login)) : "—"}</td>
          <td>${u.sessions ? `${u.sessions} device(s)` : "—"}</td>
          <td class="user-actions">${u.manageable ? `
            <button class="btn btn-text" data-u="password">🔑 Password</button>
            <button class="btn btn-text" data-u="logout" ${u.sessions ? "" : "disabled"}>↪ Log out</button>
            <button class="btn btn-text" data-u="toggle">${u.disabled ? "✅ Unblock" : "⛔ Block"}</button>
            <button class="btn btn-text btn-danger" data-u="delete">🗑 Delete</button>` : ""}</td>
        </tr>`).join("") || `<tr><td colspan="7" class="muted">Nobody found.</td></tr>`}</tbody></table></div>
    <p class="muted small">People sign up themselves at <code>/login</code>. Block: they cannot sign in until unblocked.
      Log out: ends their Studio sessions on every device. Deleting keeps their channels: they move to you.</p>`;

  let typing;
  const box = $("#user-search");
  box.addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => renderUsers(box.value).then(() => {
      const again = $("#user-search");
      again.focus();
      again.setSelectionRange(again.value.length, again.value.length);
    }), 300);
  });
  view.querySelectorAll(".user-id").forEach((c) => c.addEventListener("click", () => {
    navigator.clipboard?.writeText(c.textContent).then(() => toast("ID copied"));
  }));
  view.querySelectorAll("tr[data-id]").forEach((row) => row.addEventListener("click", async (e) => {
    const act = e.target.dataset.u;
    if (!act) return;
    const id = row.dataset.id;
    const u = users.find((x) => x.id === id);
    const url = `/api/users/${encodeURIComponent(id)}`;
    try {
      if (act === "toggle") {
        if (!u.disabled && !confirm(`Block ${u.email}? They are signed out and cannot sign in until unblocked.`)) return;
        await api(url, { method: "PATCH", body: JSON.stringify({ disabled: !u.disabled }) });
        toast(u.disabled ? "Unblocked" : "Blocked");
      }
      if (act === "password") {
        const pw = prompt(`New password for ${u.email} (at least 8 characters). They are signed out everywhere:`);
        if (!pw) return;
        await api(url, { method: "PATCH", body: JSON.stringify({ password: pw }) });
        toast("Password set - tell the user");
      }
      if (act === "logout") {
        await api(`${url}/logout`, { method: "POST" });
        toast(`${u.email} signed out of the Studio`);
      }
      if (act === "delete") {
        if (!confirm(`Delete ${u.email} for good? Their channels move to you.`)) return;
        await api(url, { method: "DELETE" });
        toast("Deleted");
      }
      renderUsers();
    } catch (err) { toast(err.message, true); }
  }));
}

async function loadHealth() {
  try {
    health = await api("/api/health");
    const pill = $("#mode-pill");
    pill.textContent = health.mock ? "TEST MODE · free" : "LIVE · Gemini";
    pill.className = "pill" + (health.mock ? " warn" : "");
    $("#channel-niche").textContent = health.niche || "";
  } catch { /* the view shows the error */ }
}

// ------------------------------------------------------------------ dialogs

// settings a single video may change, offered in ＋ CREATE → Options
const NEW_OPTION_KEYS = ["BODY_SCENES", "VOICE_LOCALE", "VOICE_GENDER", "EDGE_VOICE", "EDGE_RATE", "EDGE_PITCH",
  "VISUAL_PROVIDER", "IMAGE_STYLE", "MUSIC_TRACK", "LOOP_ENDING", "MAX_PARTS", "CAPTIONS", "CAPTION_POSITION",
  "CAPTION_WORDS", "OVERLAY_TEXT", "CHANNEL_LANGUAGE"];
let newFields = [];

async function openNew() {
  const f = $("#new-form");
  f.reset();
  try { health = await api("/api/health"); } catch { /* keep the last known settings */ }
  const hint = $("#next-part-hint");
  const next = health?.next_part || (health?.next_topic && `“${health.next_topic}” (next in Topics)`);
  hint.hidden = !next;
  hint.textContent = next ? `With an empty topic this makes ${next}.` : "";
  f.mock.checked = !!health?.mock;
  f.video.checked = !!health?.use_video_hook;
  try {
    const { fields } = await api("/api/settings");
    newFields = fields.filter((x) => NEW_OPTION_KEYS.includes(x.key));
    const box = $("#new-options");
    box.innerHTML = newFields.map((x) => `<label class="setting"><span>${esc(x.label)}</span>${fieldInput(x, "", `as in Settings (${x.value || "—"})`)}</label>`).join("");
    wireVoicePickers(box, "as in Settings");
  } catch { /* the dialog still works without options */ }
  $("#new-dialog").showModal();
}

$("#new-dialog").addEventListener("close", async () => {
  if ($("#new-dialog").returnValue !== "ok") return;
  const f = $("#new-form");
  const options = Object.fromEntries(Object.entries(readFields(f, newFields)).filter(([, v]) => v !== ""));
  const script = f.script.value.trim();
  try {
    const r = await api("/api/jobs", { method: "POST", body: JSON.stringify({
      topic: f.topic.value.trim() || null, mock: f.mock.checked, use_video_hook: f.video.checked,
      options, script: script || null, variants: Number(f.variants.value) }) });
    toast(Number(f.variants.value) > 1 ? `Started — ${f.variants.value} versions, one after another` : "Started — follow the pipeline live");
    currentTab = "script";
    location.hash = `#/video/${r.job_id}`;
  } catch (e) { toast(e.message, true); }
});

function openUpload() {
  $("#upload-form").reset();
  $("#drop-text").textContent = "Drag a video here or click to choose (.mp4, .mov, .webm)";
  $("#upload-progress").hidden = true;
  $("#upload-dialog").showModal();
}

$("#upload-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target;
  if (!f.file.files.length) return toast("Choose a video file", true);
  const fd = new FormData(f);
  const bar = $("#upload-progress");
  bar.hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = (e.loaded / e.total) * 100; };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* keep {} */ }
    if (xhr.status >= 300) return toast(data.detail || "Upload failed", true);
    $("#upload-dialog").close();
    toast("Uploaded");
    currentTab = "details";
    location.hash = `#/video/${data.job_id}`;
  };
  xhr.onerror = () => toast("Upload failed", true);
  xhr.send(fd);
});

const drop = $("#drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  if (e.dataTransfer.files.length) {
    $("#upload-form").file.files = e.dataTransfer.files;
    $("#drop-text").textContent = e.dataTransfer.files[0].name;
  }
});
$("#upload-form").file.addEventListener("change", (e) => {
  if (e.target.files.length) $("#drop-text").textContent = e.target.files[0].name;
});
$("#upload-form").querySelector("[value=cancel]").addEventListener("click", () => $("#upload-dialog").close());

// ------------------------------------------------------------------ wiring

$("#create-btn").addEventListener("click", (e) => { e.stopPropagation(); $("#create-menu").hidden = !$("#create-menu").hidden; });
$("#user-btn").addEventListener("click", (e) => { e.stopPropagation(); $("#user-menu").hidden = !$("#user-menu").hidden; });
document.addEventListener("click", async (e) => {
  const act = e.target.closest("[data-action]");
  if (act) {
    e.preventDefault();
    $("#create-menu").hidden = true;
    $("#user-menu").hidden = true;
    if (act.dataset.action === "new") openNew();
    if (act.dataset.action === "upload") openUpload();
    if (act.dataset.action === "logout") {
      await api("/api/auth/logout", { method: "POST" }).catch(() => {});
      location.href = "/login";
    }
    return;
  }
  if (!e.target.closest(".create-wrap")) { $("#create-menu").hidden = true; $("#user-menu").hidden = true; }
  if (e.target.closest("#user-menu a")) $("#user-menu").hidden = true;
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a")) location.hash = row.dataset.href;
});
$("#menu-btn").addEventListener("click", () => document.body.classList.toggle("nav-open"));
window.addEventListener("hashchange", () => { document.body.classList.remove("nav-open"); route(); });

loadMe().catch(() => {}).then(loadHealth).then(route);
setInterval(loadHealth, 30000);
