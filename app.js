// Gallery + model view (one page). Route: #m=<model id> opens the view.
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = x => x == null ? "–" : x === 0 ? "0" : x < 1e-3 || x >= 1e4 ? x.toExponential(2) : x.toPrecision(3);
const tm = e => e.time == null ? "–" : (e.status === "timeout" ? ">" : "") + (e.time < 1 ? e.time.toFixed(2) : e.time.toFixed(1)) + "s";
const ratio = m => m.cpp.status === "ok" && m.rs.status === "ok" ? m.rs.time / Math.max(m.cpp.time, 0.01) : null;
const KIND_LABEL = { match: "match", diff: "differ", timeout: "timeout", error: "error", pending: "pending", cpponly: "SMLib only" };
function kind(m) {
  if (m.cpp.status === "pending" || m.rs.status === "pending") return "pending";
  if (m.rs.status === "timeout" || m.cpp.status === "timeout") return "timeout";
  if (m.cpp.status !== "ok" || m.rs.status !== "ok") return "error";
  return m.cmp && m.cmp.agree ? "match" : "diff";
}
const KEY = { rs: m => m.rs.time ?? -1, cpp: m => m.cpp.time ?? -1, ratio: m => ratio(m) ?? -1, dist: m => m.cmp ? m.cmp.max_dist : -1, faces: m => Math.max(m.cpp.faces ?? 0, m.rs.faces ?? 0) };
const FOLDERS = ["Fillet", "Solids", "Boolean", "Defeature"];
let M = [], LIST = [], byId = new Map(), GZ = false;

// ---------------------------------------------------------------- gallery
function filtered() {
  const q = $("q").value.trim().toLowerCase(), ff = $("ff").value, fs = $("fs").value, so = $("so").value;
  let L = M.filter(m => (!q || m.path.toLowerCase().includes(q)) && (!ff || m.folder === ff) && (!fs || kind(m) === fs));
  if (so === "name") {
    const fi = f => { const i = FOLDERS.indexOf(f); return i < 0 ? 99 : i; };
    L.sort((a, b) => fi(a.folder) - fi(b.folder) || a.folder.localeCompare(b.folder) || a.name.localeCompare(b.name));
  } else L.sort((a, b) => KEY[so](b) - KEY[so](a));
  return L;
}
function thumb(m, cls) {
  return m.bytes ? `<img class="${cls}" src="thumb/${encodeURIComponent(m.id)}.webp" loading="lazy" decoding="async" alt="" onerror="this.outerHTML='<div class=&quot;${cls} nothumb&quot;>no preview</div>'">`
    : `<div class="${cls} nothumb">${esc(m.cpp.status)} / ${esc(m.rs.status)}</div>`;
}
function renderGrid() {
  LIST = filtered();
  $("grid").innerHTML = LIST.map(m => {
    const k = kind(m);
    const r = ratio(m);
    return `<a class="card" href="#m=${encodeURIComponent(m.id)}">${thumb(m, "th")}<span class="badge k-${k}">${KIND_LABEL[k]}</span><span class="folder">${esc(m.folder)}</span>
<div class="cap"><div class="cn">${esc(m.name)}</div><div class="cm">${m.rs.faces ?? m.cpp.faces ?? "–"} faces · ${tm(m.cpp)} | ${tm(m.rs)}${r != null ? ` ×${r.toFixed(0)}` : ""}</div></div></a>`;
  }).join("") || `<p style="color:var(--dim)">No models match.</p>`;
  if ($("viewer").classList.contains("on")) renderStrip();
}

// ---------------------------------------------------------------- viewer
const S = { shade: true, edges: true, wire: false, diff: false, overlay: false };
let V = null; // three.js state, created on first open
let cur = null, loadSeq = 0;
const cache = new Map(); // id -> Promise<parsed .bin>, most recent last
function fetchModel(id) {
  if (cache.has(id)) { const p = cache.get(id); cache.delete(id); cache.set(id, p); return p; }
  const p = loadThree().then(() => fetchBin(id, GZ));
  p.catch(() => cache.delete(id));
  cache.set(id, p);
  while (cache.size > 10) cache.delete(cache.keys().next().value);
  return p;
}
function loadScript(src, integrity) {
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; if (integrity) { s.integrity = integrity; s.crossOrigin = "anonymous"; }
    s.onload = res; s.onerror = () => rej(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}
let threeReady = null;
function loadThree() {
  return threeReady ||= (async () => {
    await loadScript("https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js", "sha384-CI3ELBVUz9XQO+97x6nwMDPosPR5XvsxW2ua7N1Xeygeh1IxtgqtCkGfQY9WWdHu");
    await loadScript("https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js", "sha384-wagZhIFgY4hD+7awjQjR4e2E294y6J2HSnd8eTNc15ZubTeQeVRZwhQJ+W6hnBsf");
    await loadScript("core.js");
  })();
}
function initThree() {
  const rA = new THREE.WebGLRenderer({ canvas: $("ca"), antialias: true, alpha: true });
  const rB = new THREE.WebGLRenderer({ canvas: $("cb"), antialias: true, alpha: true });
  for (const r of [rA, rB]) r.setPixelRatio(Math.min(devicePixelRatio, 2));
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 100);
  camera.up.set(0, 0, 1);
  const ctlA = new THREE.OrbitControls(camera, $("ca")), ctlB = new THREE.OrbitControls(camera, $("cb"));
  V = { rA, rB, camera, ctlA, ctlB, scA: makeScene(), scB: makeScene(), scO: makeScene(), objs: [], geos: [], center: new THREE.Vector3(), radius: 1, dir: DEFAULT_DIR() };
  for (const c of [ctlA, ctlB]) c.addEventListener("change", () => { (c === ctlA ? ctlB : ctlA).target.copy(c.target); draw(); });
}
function applyState() {
  for (const g of V.objs) {
    const u = g.userData, ghost = u.style === "ghost";
    u.m.visible = S.shade; u.w.visible = S.wire; u.e.visible = S.edges; u.d.visible = S.diff;
    u.e.material.color.set(ghost ? 0xff8c3a : (S.shade ? 0x14171d : 0x9fb7d8));
    const geo = u.m.geometry;
    geo.attributes.color.array.set(S.diff ? geo.userData.diffCol : geo.userData.plainCol);
    geo.attributes.color.needsUpdate = true;
  }
  for (const k of Object.keys(S)) $("t-" + k).classList.toggle("on", S[k]);
  $("pb").style.display = S.overlay ? "none" : "";
  $("la").innerHTML = S.overlay ? '<span style="color:#9fc0ff">SMLib (blue)</span> + <span style="color:#ff9a50">solidcore (translucent orange)</span>' : "SMLib (C++)";
  const lg = S.diff ? '<i style="background:#ffd91a"></i>small<i style="background:#ff1a1a"></i>large dist<i style="background:#f233d9"></i>mask mismatch' : "";
  $("lga").innerHTML = lg; $("lgb").innerHTML = lg;
  resize();
}
function resize() {
  const c = $("strip").querySelector("a.cur");
  if (c) c.scrollIntoView({ block: "nearest", inline: "nearest" });
  if (!V) return;
  for (const [r, c] of [[V.rA, "ca"], [V.rB, "cb"]]) { const el = $(c); if (el.clientWidth && el.clientHeight) r.setSize(el.clientWidth, el.clientHeight, false); }
  const el = $("ca");
  if (el.clientHeight) { V.camera.aspect = el.clientWidth / el.clientHeight; V.camera.updateProjectionMatrix(); }
  draw();
}
let pending = false;
function draw() { if (pending || !V) return; pending = true; requestAnimationFrame(() => { pending = false; drawNow(); }); }
function drawNow() {
  for (const sc of [V.scA, V.scB, V.scO]) placeLights(sc, V.camera, V.center, V.radius);
  if (S.overlay) { V.rA.render(V.scO, V.camera); return; }
  V.rA.render(V.scA, V.camera);
  if ($("pb").style.display !== "none") V.rB.render(V.scB, V.camera);
}
function fit() {
  // keep the current view direction, refit distance and target to this model
  const d = V.camera.position.clone().sub(V.ctlA.target);
  if (d.lengthSq() > 0 && isFinite(d.x)) V.dir = d.normalize();
  fitCamera(V.camera, V.center, V.radius, V.dir);
  for (const c of [V.ctlA, V.ctlB]) { c.target.copy(V.center); c.update(); }
  draw();
}
function clearModel() {
  for (const g of V.objs) { g.parent && g.parent.remove(g); disposeGroup(g); }
  for (const geo of V.geos) { geo.mesh.dispose(); geo.edges.dispose(); geo.diffPts.dispose(); }
  V.objs = []; V.geos = [];
}
function statsHtml(m) {
  const side = (s, l) => { const e = m[s]; return `${l} <span class="${e.status}">${e.status}</span>${e.faces != null ? ` ${e.faces} faces, ${e.edges} edges` : ""}${e.time != null ? `, ${tm(e)}` : ""}${e.retried_after ? ` (retried, ${e.limit}s limit)` : ""}${e.error ? ` <small>${esc(e.error)}</small>` : ""}`; };
  let st = side("cpp", "SMLib") + " · " + side("rs", "solidcore");
  const c = m.cmp;
  if (c) st += `<br><span class="${c.agree ? "ok" : "crash"}">${c.agree ? "match" : "differ"}</span>: grid max dist ${fmt(c.max_dist)} (bbox diag ${fmt(c.diag)}), mask mismatch ${c.mask_mismatch}, edge max dist ${fmt(c.edge_max_dist)}${c.bnd_samples != null ? `, trim-loop max dist ${fmt(c.bnd_max_dist)}${c.bnd_same_structure ? "" : " (loop structure differs)"}` : ""}${c.same_counts ? "" : ", counts differ"}`;
  return st;
}
async function show(id) {
  const m = byId.get(id);
  if (!m) { closeViewer(); return; }
  cur = id;
  const seq = ++loadSeq;
  $("viewer").classList.add("on");
  document.body.style.overflow = "hidden";
  $("vname").textContent = m.path;
  document.title = m.name + " · solidcore vs SMLib";
  $("stats").innerHTML = statsHtml(m);
  renderStrip();
  for (const [s, el] of [["cpp", "ma"], ["rs", "mb"]]) $(el).innerHTML = m[s].status === "ok" ? "loading…" : `${esc(m[s].status)}${m[s].error ? "<br><small>" + esc(m[s].error) + "</small>" : ""}`;
  await loadThree();
  if (!V) initThree();
  if (seq !== loadSeq) return;
  clearModel(); draw();
  if (!m.bytes) { $("ma").textContent ||= "no data"; return; }
  let d;
  try { d = await fetchModel(id); } catch (e) { $("ma").textContent = "failed to load: " + e.message; return; }
  if (seq !== loadSeq) return;
  const add = (sc, geo, style) => { const o = makeObjects(geo, style); sc.add(o); V.objs.push(o); };
  for (const s of ["cpp", "rs"]) {
    if (!d.hdr.sides[s]) continue;
    const geo = buildSide(d, s); V.geos.push(geo);
    add(s === "cpp" ? V.scA : V.scB, geo, "main");
    add(V.scO, geo, s === "cpp" ? "main" : "ghost");
    $(s === "cpp" ? "ma" : "mb").textContent = "";
  }
  const bb = bboxOf(d.hdr); V.center = bb.center; V.radius = bb.radius;
  applyState(); fit();
  // preload neighbours
  const i = LIST.findIndex(x => x.id === id);
  for (const j of [i + 1, i - 1, i + 2]) { const n = LIST[j]; if (n && n.bytes) fetchModel(n.id).catch(() => {}); }
}
function renderStrip() {
  const el = $("strip");
  el.innerHTML = LIST.map(m => `<a href="#m=${encodeURIComponent(m.id)}" data-id="${esc(m.id)}" class="${m.id === cur ? "cur" : ""}" title="${esc(m.path)}">${thumb(m, "")}<span>${esc(m.name)}</span><i class="k-${kind(m)}"></i></a>`).join("");
  const c = el.querySelector("a.cur");
  if (c) c.scrollIntoView({ block: "nearest", inline: "nearest" });
}
function step(k) {
  if (!LIST.length) return;
  let i = LIST.findIndex(x => x.id === cur);
  i = i < 0 ? 0 : (i + k + LIST.length) % LIST.length;
  history.replaceState(null, "", "#m=" + encodeURIComponent(LIST[i].id));
  route();
}
function closeViewer() {
  $("viewer").classList.remove("on");
  document.body.style.overflow = "";
  document.title = $("title").textContent;
  cur = null; loadSeq++;
  if (V) { clearModel(); }
}
function route() {
  const m = location.hash.match(/^#m=(.+)$/);
  if (m) show(decodeURIComponent(m[1])); else closeViewer();
}

// ---------------------------------------------------------------- wiring
for (const k of Object.keys(S)) $("t-" + k).onclick = () => { S[k] = !S[k]; if (V) applyState(); else $("t-" + k).classList.toggle("on", S[k]); };
$("t-reset").onclick = () => { if (V) { V.dir = DEFAULT_DIR(); fitCamera(V.camera, V.center, V.radius, V.dir); for (const c of [V.ctlA, V.ctlB]) { c.target.copy(V.center); c.update(); } draw(); } };
$("back").onclick = () => { if (history.length > 1 && sessionStorage.getItem("fromGallery") === "1") history.back(); else { history.pushState(null, "", location.pathname); route(); } };
$("prev").onclick = () => step(-1);
$("next").onclick = () => step(1);
$("info").onclick = () => $("stats").classList.toggle("open");
$("strip").addEventListener("click", e => {
  const a = e.target.closest("a"); if (!a) return;
  e.preventDefault();
  history.replaceState(null, "", "#m=" + encodeURIComponent(a.dataset.id));
  route();
});
$("grid").addEventListener("click", e => { if (e.target.closest("a.card")) { try { sessionStorage.setItem("fromGallery", "1"); } catch (_) {} } });
addEventListener("keydown", e => {
  if (!$("viewer").classList.contains("on") || e.target.tagName === "INPUT") return;
  if (e.key === "ArrowRight" || e.key === "ArrowDown") { step(1); e.preventDefault(); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { step(-1); e.preventDefault(); }
  else if (e.key === "Escape") $("back").click();
});
// swipe left/right on the header bar to step models (the canvases keep orbiting)
let sw = null;
$("v-head").addEventListener("touchstart", e => { const t = e.touches[0]; sw = { x: t.clientX, y: t.clientY, t: Date.now() }; }, { passive: true });
$("v-head").addEventListener("touchend", e => {
  if (!sw) return; const t = e.changedTouches[0], dx = t.clientX - sw.x, dy = t.clientY - sw.y;
  if (Math.abs(dx) > 50 && Math.abs(dy) < 40 && Date.now() - sw.t < 600) step(dx < 0 ? 1 : -1);
  sw = null;
}, { passive: true });
addEventListener("resize", resize);
addEventListener("hashchange", route);
for (const id of ["q", "ff", "fs", "so"]) $(id).addEventListener(id === "q" ? "input" : "change", renderGrid);

fetch("manifest.json", { cache: "no-cache" }).then(r => r.json()).then(j => {
  M = j.models; GZ = !!j.gz; byId = new Map(M.map(m => [m.id, m]));
  const fs = [...new Set(M.map(m => m.folder))].sort((a, b) => { const ia = FOLDERS.indexOf(a), ib = FOLDERS.indexOf(b); return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b); });
  $("ff").innerHTML = `<option value="">All folders</option>` + fs.map(f => `<option>${esc(f)}</option>`).join("");
  const n = k => M.filter(m => kind(m) === k).length, ok = s => M.filter(m => m[s].status === "ok").length;
  $("sum").innerHTML = `<span><b>${M.length}</b> models</span><span>SMLib ok <b>${ok("cpp")}</b></span><span>solidcore ok <b>${ok("rs")}</b></span><span>match <b>${n("match")}</b></span><span>differ <b>${n("diff")}</b></span><span>timeout <b>${n("timeout")}</b></span><span>error <b>${n("error")}</b></span>${n("pending") ? `<span>pending <b>${n("pending")}</b></span>` : ""}`;
  renderGrid();
  route();
});
