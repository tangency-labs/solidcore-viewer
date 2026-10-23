// Shared three.js scene code for the viewer (index.html) and the thumbnail renderer
// (thumbs.html). Data format: build.py (.bin v2).
"use strict";
const TINT = { cpp: [0.55, 0.68, 0.85], rs: [0.86, 0.62, 0.42] };

function diffColor(v) { // 1..250 distance (log), 253 only SMLib inside, 254 only solidcore inside
  if (v >= 253) return [0.95, 0.2, 0.85];
  const t = Math.min(1, v / 250);
  return [1, 0.85 * (1 - t) + 0.1, 0.1];
}

function parseBin(buf) {
  const hl = new DataView(buf).getUint32(0, true);
  const hdr = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hl)));
  return { buf, base: 4 + hl, hdr };
}

// One side's meshes: per face quantized vertices and triangles (trim-loop clipped by
// mesh.py, or the kernel's inside mask); vertex normals from each face's triangles.
function buildSide(d, side) {
  const { buf, base, hdr } = d;
  const lo = hdr.bbox.slice(0, 3), hi = hdr.bbox.slice(3), ext = [0, 1, 2].map(k => Math.max(hi[k] - lo[k], 1e-12));
  const sh = hdr.sides[side], tint = TINT[side];
  let NV = 0, NT = 0;
  for (const f of sh.faces) if (f) { NV += f.nv; NT += f.nt; }
  const pos = new Float32Array(NV * 3), dcol = new Float32Array(NV * 3), idx = new Uint32Array(NT * 3);
  let v0 = 0, t0 = 0;
  for (const f of sh.faces) {
    if (!f) continue;
    const q = new Uint16Array(buf, base + f.v, f.nv * 3);
    const t = f.ti === 2 ? new Uint16Array(buf, base + f.t, f.nt * 3) : new Uint32Array(buf, base + f.t, f.nt * 3);
    const k = f.k != null ? new Uint8Array(buf, base + f.k, f.nv) : null;
    for (let i = 0; i < f.nv; i++) {
      for (let c = 0; c < 3; c++) pos[(v0 + i) * 3 + c] = lo[c] + q[i * 3 + c] / 65535 * ext[c];
      const cc = k && k[i] ? diffColor(k[i]) : tint;
      dcol[(v0 + i) * 3] = cc[0]; dcol[(v0 + i) * 3 + 1] = cc[1]; dcol[(v0 + i) * 3 + 2] = cc[2];
    }
    for (let i = 0; i < f.nt * 3; i++) idx[t0 + i] = t[i] + v0;
    v0 += f.nv; t0 += f.nt * 3;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setIndex(new THREE.BufferAttribute(idx, 1));
  g.computeVertexNormals();
  const plain = new Float32Array(NV * 3);
  for (let i = 0; i < NV * 3; i += 3) { plain[i] = tint[0]; plain[i + 1] = tint[1]; plain[i + 2] = tint[2]; }
  g.setAttribute("color", new THREE.BufferAttribute(plain.slice(), 3));
  g.userData = { diffCol: dcol, plainCol: plain };
  const eq = new Uint16Array(buf, base + sh.edges.off, sh.edges.n * 48 * 3), ep = new Float32Array(sh.edges.n * 47 * 6);
  let n = 0;
  for (let e = 0; e < sh.edges.n; e++) for (let j = 0; j < 47; j++) for (const tt of [j, j + 1]) {
    const o = (e * 48 + tt) * 3;
    for (let c = 0; c < 3; c++) ep[n++] = lo[c] + eq[o + c] / 65535 * ext[c];
  }
  const eg = new THREE.BufferGeometry(); eg.setAttribute("position", new THREE.BufferAttribute(ep, 3));
  const dp = [], dc = [];
  if (sh.dpts) {
    const q = new Uint16Array(buf, base + sh.dpts.off, sh.dpts.n * 3), c = new Uint8Array(buf, base + sh.dpts.codes, sh.dpts.n);
    for (let i = 0; i < sh.dpts.n; i++) { for (let j = 0; j < 3; j++) dp.push(lo[j] + q[i * 3 + j] / 65535 * ext[j]); dc.push(...diffColor(c[i])); }
  }
  const dg = new THREE.BufferGeometry();
  dg.setAttribute("position", new THREE.Float32BufferAttribute(dp, 3));
  dg.setAttribute("color", new THREE.Float32BufferAttribute(dc, 3));
  return { mesh: g, edges: eg, diffPts: dg };
}

function makeScene() {
  const sc = new THREE.Scene();
  sc.add(new THREE.HemisphereLight(0xdde6ff, 0x2a2520, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 0.85), fill = new THREE.DirectionalLight(0xbfd0ff, 0.3);
  sc.add(key, fill, key.target, fill.target);
  sc.userData.lights = [key, fill];
  return sc;
}

function makeObjects(geo, style) { // style: "main" | "ghost"
  const grp = new THREE.Group();
  const shade = new THREE.MeshStandardMaterial({ vertexColors: true, metalness: 0.08, roughness: 0.55, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
  if (style === "ghost") { shade.transparent = true; shade.opacity = 0.45; shade.depthWrite = false; }
  const wire = new THREE.MeshBasicMaterial({ color: style === "ghost" ? 0xffa860 : 0x8fa6c8, wireframe: true, transparent: true, opacity: style === "ghost" ? 0.55 : 0.35 });
  const m = new THREE.Mesh(geo.mesh, shade), w = new THREE.Mesh(geo.mesh, wire);
  const e = new THREE.LineSegments(geo.edges, new THREE.LineBasicMaterial({ color: style === "ghost" ? 0xff8c3a : 0x14171d }));
  const d = new THREE.Points(geo.diffPts, new THREE.PointsMaterial({ size: 5, sizeAttenuation: false, vertexColors: true, depthTest: false }));
  d.renderOrder = 5;
  grp.add(m, w, e, d);
  grp.userData = { m, w, e, d, style };
  return grp;
}

function disposeGroup(grp) {
  grp.traverse(o => { if (o.material) o.material.dispose(); });
}

// Lights follow the camera: key light up-left of the view direction, fill to the right.
function placeLights(sc, camera, center, radius) {
  const [k, f] = sc.userData.lights, dir = camera.position.clone().sub(center);
  const up = camera.up.clone(), right = new THREE.Vector3().crossVectors(dir, up).normalize();
  k.position.copy(center).add(dir).addScaledVector(up, radius * 1.2).addScaledVector(right, -radius * 0.8); k.target.position.copy(center);
  f.position.copy(center).add(dir).addScaledVector(right, radius * 1.5); f.target.position.copy(center);
}

function bboxOf(hdr) {
  const lo = new THREE.Vector3(...hdr.bbox.slice(0, 3)), hi = new THREE.Vector3(...hdr.bbox.slice(3));
  return { center: lo.clone().add(hi).multiplyScalar(0.5), radius: Math.max(hi.distanceTo(lo) / 2, 1e-9) };
}

// Place the camera along `dir` (unit, from target to camera) so a sphere of `radius`
// about `center` fits both field-of-view axes.
function fitCamera(camera, center, radius, dir, margin = 1.08) {
  const vf = camera.fov * Math.PI / 360, hf = Math.atan(Math.tan(vf) * camera.aspect);
  const d = radius * margin / Math.sin(Math.min(vf, hf));
  camera.position.copy(center).addScaledVector(dir, d);
  camera.near = radius / 200; camera.far = radius * 200; camera.updateProjectionMatrix();
}
const DEFAULT_DIR = () => new THREE.Vector3(0.62, -0.55, 0.55).normalize();

// Fetch a model's .bin (or .bin.gz, inflated here: static hosts serve it without
// Content-Encoding) and parse it.
async function fetchBin(id, gz, opts) {
  const r = await fetch("data/" + encodeURIComponent(id) + ".bin" + (gz ? ".gz" : ""), opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  const buf = gz ? await new Response(r.body.pipeThrough(new DecompressionStream("gzip"))).arrayBuffer() : await r.arrayBuffer();
  return parseBin(buf);
}
