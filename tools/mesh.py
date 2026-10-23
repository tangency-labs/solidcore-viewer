#!/usr/bin/env python3
"""Trim-loop-clipped face meshes for both kernels, built the same way for each.

usage: mesh.py <workdir> [--step N] [--only PREFIX,...] [--jobs N]

Per model and side (needs run.py's raw grid):
 1. `--loops` export (each face's trim loops in UV, sampled per edge use, plus the
    surface at those samples) -> <work>/loops/<id>.<side>.json
 2. per face: the UV grid lines (every STEP-th of the 88 run lines, plus the last)
    cut the UV domain into cells. A cell no loop segment touches is kept whole (two
    triangles) when its corners are inside the loops (even-odd). A cell a loop touches
    is clipped against the loops (pyclipper, even-odd) and the pieces are triangulated
    with earcut. Every vertex that is not a run-grid point the kernel already
    evaluated (loop samples carry their own surface point) is evaluated on the surface
    by the same kernel (`--eval`).
 3. -> <work>/mesh/<id>.<side>.s<STEP>.npz: per face uv, xyz, tri, gridk (run-grid
    index or -1) and the method used ("clip", or "mask" when loops are missing or open).
A face whose loops are missing (no UV trim curve) or not closed in UV (e.g. a periodic
face bounded by seam-crossing loops) is meshed from the kernel's inside mask instead,
as before.
Libraries: numpy (BSD), pyclipper (MIT, Clipper: Boost), mapbox_earcut (ISC).
"""
import json, os, signal, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor

import mapbox_earcut as earcut
import numpy as np
import pyclipper

G = 88
CORPUS = os.path.expanduser("~/github/solidcore/upstream/TestFiles/pt_TestFiles")
HERE = os.path.dirname(os.path.abspath(__file__))
BINS = {
    "cpp": [os.path.join(HERE, "bin/smlib_wire")],
    "rs": [os.path.expanduser("~/github/solidcore-wt/render/bin/release/examples/corpus")],
}
S = float(1 << 30)  # clipper integer scale over the UV domain
TIMEOUT = 300


def mid(rel):
    return rel.replace("/", "__").replace(".", "_")


def run(cmd, timeout=TIMEOUT):
    p = subprocess.Popen(["nice", "-n", "10"] + cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
        _, err = p.communicate(timeout=timeout)
        return p.returncode, err.decode(errors="replace")[-300:]
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return None, "timeout"


def axis(step):
    a = list(range(0, G, step))
    if a[-1] != G - 1:
        a.append(G - 1)
    return a


def pip_evenodd(pts, rings):
    """Even-odd point in polygon for pts (N,2) against closed rings."""
    inside = np.zeros(len(pts), bool)
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    for r in rings:
        a, b = r, np.roll(r, -1, axis=0)
        ax, ay, bx, by = a[:, 0][None], a[:, 1][None], b[:, 0][None], b[:, 1][None]
        for s in range(0, len(pts), 2048):
            xs, ys = x[s:s + 2048], y[s:s + 2048]
            cond = (ay > ys) != (by > ys)
            with np.errstate(divide="ignore", invalid="ignore"):
                xi = ax + (ys - ay) * (bx - ax) / (by - ay)
            inside[s:s + 2048] ^= (np.count_nonzero(cond & (xs < xi), axis=1) & 1).astype(bool)
    return inside


def mesh_face(grid, face, step):
    """-> dict(uv, xyz, tri, gridk, method); xyz rows NaN where an evaluation is needed."""
    ax = axis(step)
    lo, hi = face["dom"] if face else (None, None)
    rings, ring_xyz = [], []
    ok = face is not None and all(l is not None for l in face["loops"])
    if ok:
        su, sv = hi[0] - lo[0], hi[1] - lo[1]
        if not (su > 0 and sv > 0):
            ok = False
    if ok:
        for l in face["loops"]:
            uv = np.array(l["uv"], float)
            xyz = np.array([p if p else [np.nan] * 3 for p in l["xyz"]], float)
            # drop consecutive duplicates (edge-use joints)
            keep = np.ones(len(uv), bool)
            keep[1:] = np.any(np.abs(np.diff(uv, axis=0)) > 1e-12 * max(su, sv), axis=1)
            uv, xyz = uv[keep], xyz[keep]
            gap = np.hypot((uv[0, 0] - uv[-1, 0]) / su, (uv[0, 1] - uv[-1, 1]) / sv)
            if gap > 1e-6:
                ok = False
                break
            if len(uv) > 1 and np.allclose(uv[0], uv[-1], rtol=0, atol=1e-12 * max(su, sv)):
                uv, xyz = uv[:-1], xyz[:-1]
            if len(uv) >= 3:
                rings.append(uv)
                ring_xyz.append(xyz)
        if ok and face["loops"] and not rings:
            ok = False
    if not ok:
        return mask_mesh(grid, step)
    if not face["loops"]:  # no loops: the whole UV domain
        rings = [np.array([lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]], float)]
        ring_xyz = [np.full((4, 3), np.nan)]
    U = lo[0] + (hi[0] - lo[0]) * np.arange(G) / (G - 1)
    V = lo[1] + (hi[1] - lo[1]) * np.arange(G) / (G - 1)
    us, vs = U[ax], V[ax]
    n = len(ax)
    gk = np.array([[r * G + c for c in ax] for r in ax])  # run-grid index per node (row=v)
    node_uv = np.stack(np.meshgrid(us, vs), -1).reshape(-1, 2)
    node_in = pip_evenodd(node_uv, rings).reshape(n, n)
    # cells touched by a loop segment (segment bbox, conservative)
    touched = np.zeros((n - 1, n - 1), bool)
    for r in rings:
        a, b = r, np.roll(r, -1, axis=0)
        u0, u1 = np.minimum(a[:, 0], b[:, 0]), np.maximum(a[:, 0], b[:, 0])
        v0, v1 = np.minimum(a[:, 1], b[:, 1]), np.maximum(a[:, 1], b[:, 1])
        i0 = np.clip(np.searchsorted(us, u0, "right") - 1, 0, n - 2)
        i1 = np.clip(np.searchsorted(us, u1, "left") - 1, 0, n - 2)
        j0 = np.clip(np.searchsorted(vs, v0, "right") - 1, 0, n - 2)
        j1 = np.clip(np.searchsorted(vs, v1, "left") - 1, 0, n - 2)
        for a0, a1, b0, b1 in zip(i0, np.maximum(i1, i0), j0, np.maximum(j1, j0)):
            touched[b0:b1 + 1, a0:a1 + 1] = True
    verts, vxyz, vgk = [], [], []
    vid = {}

    def toi(u, v):
        return (int(round((u - lo[0]) / (hi[0] - lo[0]) * S)), int(round((v - lo[1]) / (hi[1] - lo[1]) * S)))

    # lookup tables: integer coords -> known 3D (grid points with kernel values, loop samples)
    known = {}
    for r, x in zip(rings, ring_xyz):
        for p, q in zip(r, x):
            known.setdefault(toi(p[0], p[1]), (p, q, -1))
    for jr in range(n):
        for ic in range(n):
            k = int(gk[jr, ic])
            g = grid[k] if grid is not None else 0
            known[toi(us[ic], vs[jr])] = ((us[ic], vs[jr]), np.array(g, float) if g else np.full(3, np.nan), k)

    def vert(key):
        i = vid.get(key)
        if i is None:
            i = len(verts)
            vid[key] = i
            kn = known.get(key)
            if kn is not None:
                verts.append(kn[0]); vxyz.append(kn[1]); vgk.append(kn[2])
            else:
                verts.append((lo[0] + key[0] / S * (hi[0] - lo[0]), lo[1] + key[1] / S * (hi[1] - lo[1])))
                vxyz.append(np.full(3, np.nan)); vgk.append(-1)
        return i

    tris = []
    clip_paths = [[toi(p[0], p[1]) for p in r] for r in rings]
    for jr in range(n - 1):
        for ic in range(n - 1):
            c = [(ic, jr), (ic + 1, jr), (ic + 1, jr + 1), (ic, jr + 1)]
            keys = [toi(us[a], vs[b]) for a, b in c]
            if not touched[jr, ic]:
                if node_in[jr, ic] and node_in[jr, ic + 1] and node_in[jr + 1, ic + 1] and node_in[jr + 1, ic]:
                    i = [vert(k) for k in keys]
                    tris += [(i[0], i[1], i[2]), (i[0], i[2], i[3])]
                continue
            pc = pyclipper.Pyclipper()
            try:
                pc.AddPath(keys, pyclipper.PT_SUBJECT, True)
                pc.AddPaths(clip_paths, pyclipper.PT_CLIP, True)
                tree = pc.Execute2(pyclipper.CT_INTERSECTION, pyclipper.PFT_EVENODD, pyclipper.PFT_EVENODD)
            except pyclipper.ClipperException:
                continue
            stack = list(tree.Childs)
            while stack:
                outer = stack.pop()
                if outer.IsHole:
                    stack.extend(outer.Childs)
                    continue
                holes = [h for h in outer.Childs if h.IsHole]
                for h in holes:
                    stack.extend(h.Childs)
                ringsi = [outer.Contour] + [h.Contour for h in holes]
                flat = np.array([p for r in ringsi for p in r], float)
                ends = np.cumsum([len(r) for r in ringsi]).astype(np.uint32)
                idx = earcut.triangulate_float64(flat, ends)
                keyl = [tuple(map(int, p)) for r in ringsi for p in r]
                for t in range(0, len(idx), 3):
                    tris.append(tuple(vert(keyl[int(idx[t + m])]) for m in range(3)))
    uv = np.array(verts, float).reshape(-1, 2)
    tri = np.array(tris, np.int64).reshape(-1, 3)
    # make every triangle counter-clockwise in UV (normal along Su x Sv)
    if len(tri):
        a, b, c = uv[tri[:, 0]], uv[tri[:, 1]], uv[tri[:, 2]]
        area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
        flip = area < 0
        tri[flip] = tri[flip][:, [0, 2, 1]]
        tri = tri[area != 0]
    return {"uv": uv, "xyz": np.array(vxyz, float).reshape(-1, 3), "tri": tri, "gridk": np.array(vgk, np.int64), "method": "clip"}


def mask_mesh(grid, step):
    """The earlier mesh: cells of the kernel's inside mask (4 corners -> 2, 3 -> 1)."""
    ax = axis(step)
    n = len(ax)
    if grid is None:
        return {"uv": np.zeros((0, 2)), "xyz": np.zeros((0, 3)), "tri": np.zeros((0, 3), np.int64), "gridk": np.zeros(0, np.int64), "method": "none"}
    vid, xyz, gks = {}, [], []
    for jr in range(n):
        for ic in range(n):
            k = ax[jr] * G + ax[ic]
            if grid[k]:
                vid[(jr, ic)] = len(xyz)
                xyz.append(grid[k]); gks.append(k)
    tris = []
    for jr in range(n - 1):
        for ic in range(n - 1):
            c = [(jr, ic), (jr, ic + 1), (jr + 1, ic + 1), (jr + 1, ic)]
            ins = [x in vid for x in c]
            if all(ins):
                tris += [(vid[c[0]], vid[c[1]], vid[c[2]]), (vid[c[0]], vid[c[2]], vid[c[3]])]
            elif sum(ins) == 3:
                s = [vid[x] for x in c if x in vid]
                tris.append(tuple(s))
    return {"uv": np.zeros((len(xyz), 2)), "xyz": np.array(xyz, float).reshape(-1, 3), "tri": np.array(tris, np.int64).reshape(-1, 3), "gridk": np.array(gks, np.int64), "method": "mask"}


def process(work, rel, side, step):
    i = mid(rel)
    out = os.path.join(work, "mesh", f"{i}.{side}.s{step}.npz")
    raw = os.path.join(work, "raw", f"{i}.{side}.json")
    try:
        st = json.load(open(os.path.join(work, "status", f"{i}.{side}.json")))
    except OSError:
        return
    if st.get("status") != "ok" or not os.path.exists(raw):
        return
    if os.path.exists(out) and os.path.getmtime(out) > os.path.getmtime(raw):
        return
    while float(open("/proc/loadavg").read().split()[0]) > 24:
        time.sleep(15)
    model = os.path.join(CORPUS, rel)
    lp = os.path.join(work, "loops", f"{i}.{side}.json")
    if not os.path.exists(lp) or os.path.getmtime(lp) < os.path.getmtime(raw):
        rc, err = run(BINS[side] + ["--loops", model, lp])
        if rc != 0:
            print(f"{side} loops failed {rel}: {err.strip()[-200:]}", flush=True)
    try:
        loops = json.load(open(lp))["faces"]
    except (OSError, ValueError, KeyError):
        loops = None
    grids = json.load(open(raw))["grids"]
    meshes = []
    for fi, g in enumerate(grids):
        face = loops[fi] if loops is not None and fi < len(loops) else None
        try:
            meshes.append(mesh_face(g, face, step))
        except Exception as e:  # keep the batch going; fall back to the mask mesh
            print(f"{side} mesh_face {rel} face {fi}: {e!r}", flush=True)
            meshes.append(mask_mesh(g, step))
    # evaluate the new vertices with the same kernel
    req = os.path.join(work, "mesh", f"{i}.{side}.s{step}.req")
    res = req[:-4] + ".res"
    need = [(fi, np.nonzero(np.isnan(m["xyz"][:, 0]))[0]) for fi, m in enumerate(meshes) if m["method"] == "clip"]
    need = [(fi, ix) for fi, ix in need if len(ix)]
    if need:
        with open(req, "w") as fh:
            for fi, ix in need:
                fh.write(f"{fi} {len(ix)}\n")
                for u, v in meshes[fi]["uv"][ix]:
                    fh.write(f"{float(u)!r} {float(v)!r}\n")
        rc, err = run(BINS[side] + ["--eval", model, req, res])
        vals = []
        if rc == 0:
            for line in open(res):
                p = line.split()
                vals.append([float(x) for x in p] if len(p) == 3 else [np.nan] * 3)
        else:
            print(f"{side} eval failed {rel}: {err.strip()[-200:]}", flush=True)
        k = 0
        for fi, ix in need:
            if len(vals) >= k + len(ix):
                meshes[fi]["xyz"][ix] = np.array(vals[k:k + len(ix)], float)
            k += len(ix)
        for p in (req, res):
            if os.path.exists(p):
                os.remove(p)
    arrs, methods = {}, []
    for fi, m in enumerate(meshes):
        tri = m["tri"]
        if len(tri):
            bad = np.isnan(m["xyz"]).any(axis=1)
            tri = tri[~bad[tri].any(axis=1)]
        arrs[f"xyz{fi}"] = m["xyz"]
        arrs[f"tri{fi}"] = tri.astype(np.int32)
        arrs[f"gk{fi}"] = m["gridk"].astype(np.int32)
        methods.append(m["method"])
    arrs["methods"] = np.array(methods)
    tmp = out + ".tmp.npz"
    np.savez_compressed(tmp, **arrs)
    os.replace(tmp, out)
    print(f"{side} meshed {rel}: {len(meshes)} faces, {methods.count('clip')} clipped, {methods.count('mask')} mask", flush=True)


def main():
    work = sys.argv[1]
    a = sys.argv
    step = int(a[a.index("--step") + 1]) if "--step" in a else 1
    jobs = int(a[a.index("--jobs") + 1]) if "--jobs" in a else 4
    only = a[a.index("--only") + 1].split(",") if "--only" in a else None
    os.makedirs(os.path.join(work, "loops"), exist_ok=True)
    os.makedirs(os.path.join(work, "mesh"), exist_ok=True)
    rels = sorted(os.path.relpath(os.path.join(r, f), CORPUS) for r, _, fs in os.walk(CORPUS) for f in fs if f.endswith((".smb", ".smp")))
    if only:
        rels = [r for r in rels if any(r.startswith(p) for p in only)]
    with ProcessPoolExecutor(jobs) as ex:
        for f in [ex.submit(process, work, r, s, step) for r in rels for s in ("cpp", "rs")]:
            f.result()


if __name__ == "__main__":
    main()
