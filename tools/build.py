#!/usr/bin/env python3
"""Convert run.py output into the viewer's data files and manifest.

usage: build.py <workdir> <sitedir>
Per model with any result: <sitedir>/data/<id>.bin (layout below); always:
<sitedir>/manifest.json (status, counts and agreement stats per model).

.bin layout (v2, little endian): u32 header length, header JSON, zero pad to 4, sections.
Positions are u16 quantized over the header bbox (shared by both sides). Per side:
  edges: u16[n_edges*48*3]
  faces[i]: v u16[nv*3] vertices, t u16|u32[nt*3] triangles ("ti" bytes) (mesh.py: trim-loop clipped, or
            the kernel's inside mask, "m"), optional k u8[nv] diff code per vertex;
            null for a face with no triangles.
  dpts (optional): u16[n*3] positions and u8[n] codes of grid points where the kernels differ.
Diff codes: 1..250 log-scaled distance above DIST_TOL, 253 only SMLib inside, 254 only
solidcore inside. Agreement metrics use the full 88x88 run grid and the loop samples.
Run with the venv that has numpy, pyclipper and mapbox_earcut (mesh.py).
"""
import gzip, json, math, os, struct, sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesh as meshmod  # noqa: E402

G = 88
# Published grid: every STEP-th row/column of the 88x88 run grid plus the last one
# (--step N); agreement metrics always use the full grid.
STEP = 1
GZIP = False  # --gzip: write data/<id>.bin.gz (the viewer inflates it with DecompressionStream)


def axis():
    a = list(range(0, G, STEP))
    if a[-1] != G - 1:
        a.append(G - 1)
    return a
DIST_TOL = 1e-5  # coordinates are printed with 6 decimals on both sides
CORPUS = os.path.expanduser("~/github/solidcore/upstream/TestFiles/pt_TestFiles")
SIDES = ("cpp", "rs")


def load(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if d.get("status") == "ok" else None
    except (OSError, ValueError):
        return None


def load_loops(work, i, s):
    try:
        return json.load(open(os.path.join(work, "loops", f"{i}.{s}.json")))["faces"]
    except (OSError, ValueError, KeyError):
        return None


def dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def build(work, site, rel):
    i = rel.replace("/", "__").replace(".", "_")
    st = {}
    for s in SIDES:
        try:
            st[s] = json.load(open(os.path.join(work, "status", f"{i}.{s}.json")))
        except OSError:
            st[s] = {"status": "pending"}
    data = {s: load(os.path.join(work, "raw", f"{i}.{s}.json")) if st[s].get("status") == "ok" else None for s in SIDES}
    folder = rel.split("/")[0] if "/" in rel else "(top level)"
    entry = {"id": i, "path": rel, "folder": folder, "name": os.path.basename(rel)}
    for s in SIDES:
        e = {"status": st[s].get("status"), "time": st[s].get("time"), "limit": st[s].get("limit", 60)}
        if st[s].get("retried_after"):
            e["retried_after"] = st[s]["retried_after"]
        if st[s].get("error"):
            e["error"] = st[s]["error"]
        d = data[s]
        if d:
            e["faces"] = len(d["grids"])
            e["edges"] = len(d["edges"])
            e["breps"] = d.get("nbreps")
            e["inside"] = sum(1 for g in d["grids"] if g for p in g if p)
        entry[s] = e
    if not any(data.values()):
        return entry
    # meshes: mesh.py output for this grid step, else the mask mesh
    meshes = {}
    for s in SIDES:
        d = data[s]
        if not d:
            continue
        mp = os.path.join(work, "mesh", f"{i}.{s}.s{STEP}.npz")
        fm = []
        if os.path.exists(mp):
            z = np.load(mp)
            methods = list(z["methods"])
            for fi in range(len(d["grids"])):
                fm.append((z[f"xyz{fi}"], z[f"tri{fi}"], z[f"gk{fi}"], str(methods[fi])))
        else:
            for g in d["grids"]:
                m = meshmod.mask_mesh(g, STEP)
                fm.append((m["xyz"], m["tri"], m["gridk"], m["method"]))
        meshes[s] = fm
        entry[s]["mesh"] = {m: sum(1 for f in fm if f[3] == m) for m in ("clip", "mask")}
    allp = [np.asarray(p, float).reshape(-1, 3) for s in meshes for f in meshes[s] for p in [f[0][f[1].reshape(-1)] if len(f[1]) else np.zeros((0, 3))]]
    allp += [np.array(e, float).reshape(-1, 3) for d in data.values() if d for e in d["edges"]]
    allp += [np.array([p for p in g if p], float).reshape(-1, 3) for d in data.values() if d for g in d["grids"] if g]
    cat = np.concatenate(allp) if allp else np.zeros((0, 3))
    cat = cat[np.isfinite(cat).all(axis=1)]
    if not len(cat):
        cat = np.zeros((1, 3))
    lo, hi = cat.min(0).tolist(), cat.max(0).tolist()
    ext = [max(hi[k] - lo[k], 1e-12) for k in range(3)]
    diag = math.sqrt(sum((hi[k] - lo[k]) ** 2 for k in range(3))) or 1.0

    def qa(arr):
        arr = np.asarray(arr, float).reshape(-1, 3)
        return np.clip(np.round((arr - lo) / ext * 65535), 0, 65535).astype("<u2")

    body = bytearray()

    def section(buf):
        off = len(body)
        body.extend(buf)
        while len(body) % 4:
            body.append(0)
        return off

    header = {"v": 2, "bbox": lo + hi, "sides": {}}
    # agreement on the full run grid
    dfull = {}
    if data["cpp"] and data["rs"]:
        a, b = data["cpp"], data["rs"]
        only_c = only_r = 0
        maxd = 0.0
        worst = None
        faces_bad = 0
        for fi in range(min(len(a["grids"]), len(b["grids"]))):
            ga, gb = a["grids"][fi], b["grids"][fi]
            if ga is None or gb is None:
                if (ga is None) != (gb is None):
                    faces_bad += 1
                continue
            dd = bytearray(G * G)
            bad = False
            for k in range(G * G):
                pa_, pb_ = ga[k], gb[k]
                if pa_ and pb_:
                    x = dist(pa_, pb_)
                    if x > maxd:
                        maxd, worst = x, (fi, k)
                    if x > DIST_TOL:
                        bad = True
                        dd[k] = max(1, min(250, int(1 + 249 * math.log10(x / DIST_TOL) / max(1e-9, math.log10(diag / DIST_TOL)))))
                elif pa_ and not pb_:
                    only_c += 1; dd[k] = 253; bad = True
                elif pb_ and not pa_:
                    only_r += 1; dd[k] = 254; bad = True
            faces_bad += bad
            if bad:
                dfull[fi] = dd
        emax = 0.0
        for ea_, eb_ in zip(a["edges"], b["edges"]):
            for p, r in zip(ea_, eb_):
                emax = max(emax, dist(p, r))
        mism = only_c + only_r
        c = {
            "mask_mismatch": mism, "only_cpp": only_c, "only_rs": only_r,
            "max_dist": maxd, "max_dist_at": worst, "edge_max_dist": emax,
            "faces_differ": faces_bad, "diag": diag,
            "same_counts": len(a["grids"]) == len(b["grids"]) and len(a["edges"]) == len(b["edges"]),
        }
        # boundary agreement: the two kernels' trim-loop samples (surface at the UV samples)
        la, lb = (load_loops(work, i, s) for s in SIDES)
        if la is not None and lb is not None:
            same, bmax, bmiss, n = True, 0.0, 0, 0
            if len(la) != len(lb):
                same = False
            for fa, fb in zip(la, lb):
                if (fa is None) != (fb is None) or (fa and len(fa["loops"]) != len(fb["loops"])):
                    same = False
                    continue
                if not fa:
                    continue
                for xa, xb in zip(fa["loops"], fb["loops"]):
                    if (xa is None) != (xb is None) or (xa and xa["n"] != xb["n"]):
                        same = False
                        continue
                    if not xa:
                        continue
                    for p, r in zip(xa["xyz"], xb["xyz"]):
                        n += 1
                        if p and r:
                            bmax = max(bmax, dist(p, r))
                        elif p or r:
                            bmiss += 1
            c.update(bnd_same_structure=same, bnd_max_dist=bmax, bnd_missing=bmiss, bnd_samples=n)
        c["agree"] = (c["same_counts"] and mism == 0 and maxd <= DIST_TOL and emax <= DIST_TOL
                      and c.get("bnd_same_structure", True) and c.get("bnd_max_dist", 0) <= DIST_TOL and c.get("bnd_missing", 0) == 0)
        entry["cmp"] = c
    for s in SIDES:
        if s not in meshes:
            continue
        d = data[s]
        ea = qa(np.array(d["edges"], float).reshape(-1, 3)) if d["edges"] else np.zeros((0, 3), "<u2")
        sh = {"edges": {"off": section(ea.tobytes()), "n": len(d["edges"])}, "faces": []}
        dpos, dcode = [], []
        other = data["rs" if s == "cpp" else "cpp"]
        for fi, (xyz, tri, gk, method) in enumerate(meshes[s]):
            if not len(tri):
                sh["faces"].append(None)
                continue
            used = np.unique(tri.reshape(-1))
            remap = np.full(len(xyz), -1, np.int64)
            remap[used] = np.arange(len(used))
            v = qa(xyz[used])
            t = remap[tri].astype("<u2" if len(used) < 65536 else "<u4")
            f = {"v": section(v.tobytes()), "nv": len(used), "t": section(t.tobytes()), "nt": len(t), "ti": t.itemsize, "m": method}
            dd = dfull.get(fi)
            if dd is not None:
                g = gk[used]
                codes = np.array([dd[k] if k >= 0 else 0 for k in g], np.uint8)
                if codes.any():
                    f["k"] = section(codes.tobytes())
            sh["faces"].append(f)
            if dd is not None:
                ax = set(axis())
                for k in range(G * G):
                    if dd[k] and (k // G) in ax and (k % G) in ax:
                        p = d["grids"][fi][k] or (other["grids"][fi][k] if other else 0)
                        if p and (dd[k] < 253 or (dd[k] == 253) == (s == "cpp")):
                            dpos.append(p); dcode.append(dd[k])
        if dpos:
            sh["dpts"] = {"off": section(qa(np.array(dpos, float)).tobytes()), "codes": section(bytes(dcode)), "n": len(dpos)}
        header["sides"][s] = sh
    hb = json.dumps(header, separators=(",", ":")).encode()
    hb += b" " * ((-len(hb)) % 4)
    blob = struct.pack("<I", len(hb)) + hb + bytes(body)
    dst = os.path.join(site, "data", f"{i}.bin" + (".gz" if GZIP else ""))
    with open(dst + ".tmp", "wb") as fh:
        fh.write(gzip.compress(blob, 9, mtime=0) if GZIP else blob)
    os.replace(dst + ".tmp", dst)
    entry["bytes"] = os.path.getsize(dst)
    return entry


def main():
    global STEP, GZIP
    GZIP = "--gzip" in sys.argv
    work, site = sys.argv[1], sys.argv[2]
    if "--step" in sys.argv:
        STEP = int(sys.argv[sys.argv.index("--step") + 1])
    os.makedirs(os.path.join(site, "data"), exist_ok=True)
    rels = []
    for root, _, files in os.walk(CORPUS):
        for n in files:
            if n.endswith((".smb", ".smp")):
                rels.append(os.path.relpath(os.path.join(root, n), CORPUS))
    rels.sort()
    cache_path = os.path.join(work, f"build_cache_v2_s{STEP}{'_gz' if GZIP else ''}.json")
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        cache = {}
    out = []
    for rel in rels:
        i = rel.replace("/", "__").replace(".", "_")
        stamp = []
        for s in SIDES:
            for p in (os.path.join(work, "status", f"{i}.{s}.json"), os.path.join(work, "mesh", f"{i}.{s}.s{STEP}.npz"),
                      os.path.join(work, "loops", f"{i}.{s}.json")):
                stamp.append(os.path.getmtime(p) if os.path.exists(p) else 0)
        c = cache.get(rel)
        if c and c["stamp"] == stamp and (not c["entry"].get("bytes") or os.path.exists(os.path.join(site, "data", f"{i}.bin" + (".gz" if GZIP else "")))):
            out.append(c["entry"])
            continue
        e = build(work, site, rel)
        cache[rel] = {"stamp": stamp, "entry": e}
        out.append(e)
        print("built", rel, file=sys.stderr)
    json.dump(cache, open(cache_path + ".tmp", "w"))
    os.replace(cache_path + ".tmp", cache_path)
    tmp = os.path.join(site, "manifest.json.tmp")
    json.dump({"models": out, "G": len(axis()), "G_run": G, "gz": GZIP, "dist_tol": DIST_TOL}, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, os.path.join(site, "manifest.json"))


if __name__ == "__main__":
    main()
