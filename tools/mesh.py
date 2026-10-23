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
    index or -1) and the method used: "clip:<closed|seam|noloops>", or "mask:<why>" when
    no loop polygon matching the kernel's inside mask could be built.
An edge use without UV trim curve (e.g. a degenerate pole edge) becomes a straight UV
segment between its neighbours. A loop that closes only across a periodic seam is
unwrapped and closed along the seam lines and the domain boundary, picking the closure
that matches the kernel's inside mask. A face whose loop polygon still differs from the
kernel's inside mask at more than MASK_TOL of the run grid is meshed from that mask.
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


MASK_TOL = 0.03  # max fraction of run-grid points where the loop polygon and the kernel's mask disagree


def perim(p, lo, hi, tol):
    """Position of UV point p along the domain boundary (0..4: bottom, right, top, left),
    or None when it is not on the boundary."""
    su, sv = hi[0] - lo[0], hi[1] - lo[1]
    x, y = (p[0] - lo[0]) / su, (p[1] - lo[1]) / sv
    if abs(y) < tol: return min(max(x, 0), 1)
    if abs(x - 1) < tol: return 1 + min(max(y, 0), 1)
    if abs(y - 1) < tol: return 2 + 1 - min(max(x, 0), 1)
    if abs(x) < tol: return 3 + 1 - min(max(y, 0), 1)
    return None


def perim_point(t, lo, hi):
    t = t % 4
    k, f = int(t), t - int(t)
    u0, v0, u1, v1 = lo[0], lo[1], hi[0], hi[1]
    return [(u0 + (u1 - u0) * f, v0), (u1, v0 + (v1 - v0) * f), (u1 - (u1 - u0) * f, v1), (u0, v1 - (v1 - v0) * f)][k]


def gap_path(a, b, mode, lo, hi, K):
    """UV points from a to b for edge uses without UV trim curve: a straight segment, or
    (mode "ccw"/"cw") along the domain boundary when both ends lie on it."""
    if mode != "straight":
        ta, tb = perim(a, lo, hi, 1e-6), perim(b, lo, hi, 1e-6)
        if ta is not None and tb is not None:
            if mode == "ccw":
                tb2 = tb if tb >= ta else tb + 4
                ts = [ta] + [float(c) for c in range(int(np.floor(ta)) + 1, int(np.ceil(tb2)))] + [tb2]
            else:
                tb2 = tb if tb <= ta else tb - 4
                ts = [ta] + [float(c) for c in range(int(np.ceil(ta)) - 1, int(np.floor(tb2)), -1)] + [tb2]
            return np.array([perim_point(t, lo, hi) for t in ts], float)
    t = np.linspace(0, 1, K)[:, None]
    return a + (b - a) * t


def loop_polyline(l, lo, hi, mode="straight"):
    """One loop's UV polyline and surface points from its edge-use segments. A run of
    edge uses with no UV trim curve (e.g. a degenerate pole edge) is bridged from the
    previous segment's end to the next one's start by gap_path(mode) (surface points
    NaN, to be evaluated). None when no segment has samples."""
    su, sv = hi[0] - lo[0], hi[1] - lo[1]
    segs = l["segs"]
    have = [i for i, sg in enumerate(segs) if "uv" in sg]
    if not have:
        return None
    K = max(len(segs[i]["uv"]) for i in have)
    uvs, xyzs = [], []
    n = len(segs)
    for i, sg in enumerate(segs):
        if "uv" in sg:
            uvs.append(np.array(sg["uv"], float))
            xyzs.append(np.array([p if p else [np.nan] * 3 for p in sg["xyz"]], float))
            continue
        if "uv" not in segs[(i - 1) % n]:
            continue  # bridged together with the first edge use of this run
        prev = segs[(i - 1) % n]
        nxt = next(segs[(i + k) % n] for k in range(1, n + 1) if "uv" in segs[(i + k) % n])
        path = gap_path(np.array(prev["uv"][-1], float), np.array(nxt["uv"][0], float), mode, lo, hi, K)
        uvs.append(path)
        xyzs.append(np.full((len(path), 3), np.nan))
    uv, xyz = np.concatenate(uvs), np.concatenate(xyzs)
    keep = np.ones(len(uv), bool)
    keep[1:] = np.any(np.abs(np.diff(uv, axis=0)) > 1e-12 * max(su, sv), axis=1)
    return uv[keep], xyz[keep]


def unwrap_open(uv, xyz, lo, period):
    """A loop that closes only across the periodic seam of coordinate 0 (end = start +-
    period): wrap it into [lo, lo+period), start it after the seam crossing, add the
    crossing points on both seam lines, and orient it by increasing coordinate 0."""
    w = uv.copy()
    w[:, 0] = lo + np.mod(w[:, 0] - lo, period)
    if np.allclose(w[0], w[-1], rtol=0, atol=1e-9 * period):
        w, xyz = w[:-1], xyz[:-1]
    d = np.diff(np.concatenate([w[:, 0], w[:1, 0]]))
    jumps = np.nonzero(np.abs(d) > period / 2)[0]
    if len(jumps) != 1:
        return None
    j = int(jumps[0])
    w, xyz = np.roll(w, -(j + 1), axis=0), np.roll(xyz, -(j + 1), axis=0)
    first, last = w[0], w[-1]
    if last[0] > first[0]:  # increasing: crosses the seam at lo + period
        t = (lo + period - last[0]) / (first[0] + period - last[0])
        vc = last[1] + t * (first[1] - last[1])
        w = np.vstack([[lo, vc], w, [lo + period, vc]])
    else:
        t = (last[0] - lo) / (last[0] - (first[0] - period))
        vc = last[1] + t * (first[1] - last[1])
        w = np.vstack([[lo + period, vc], w, [lo, vc]])
        w = w[::-1]
        xyz = xyz[::-1]
    xyz = np.vstack([np.full((1, 3), np.nan), xyz, np.full((1, 3), np.nan)])
    return w, xyz


def mask_misfit(rings, grid, lo, hi):
    """Fraction of run-grid points where even-odd inside-the-rings differs from the
    kernel's inside mask."""
    if grid is None:
        return 0.0
    U = lo[0] + (hi[0] - lo[0]) * np.arange(G) / (G - 1)
    V = lo[1] + (hi[1] - lo[1]) * np.arange(G) / (G - 1)
    # interior grid points only: those on the domain boundary often lie exactly on a
    # loop that runs along it, where inside/outside is a tie
    pts = np.stack(np.meshgrid(U[1:-1], V[1:-1]), -1).reshape(-1, 2)
    ins = pip_evenodd(pts, rings)
    mask = np.array([bool(grid[r * G + c]) for r in range(1, G - 1) for c in range(1, G - 1)])
    return float(np.count_nonzero(ins != mask)) / len(mask)


def build_rings(face, grid, lo, hi):
    """build_rings_mode for each way of bridging edge uses without UV trim curves; the
    result that best matches the kernel's inside mask."""
    missing = any("miss" in sg for l in face["loops"] if l for sg in l["segs"])
    best = None
    for mode in (("straight", "ccw", "cw") if missing else ("straight",)):
        r = build_rings_mode(face, grid, lo, hi, mode)
        if r[0] is not None and (best is None or r[3] < best[3]):
            best = r
        elif best is None and r[0] is None:
            why = r[2]
    if best is None:
        return None, None, why
    if best[3] > MASK_TOL:
        return None, None, "maskcheck"
    return best[:3]


def build_rings_mode(face, grid, lo, hi, mode):
    """-> (rings, ring_xyz, how, misfit) or (None, None, why, 1). Closed loops are used as they are;
    loops open across a periodic seam are unwrapped and closed along the seam lines and
    the domain's opposite boundary, choosing the closure that matches the kernel's inside
    mask; misfit is mask_misfit of the result."""
    su, sv = hi[0] - lo[0], hi[1] - lo[1]
    if not face["loops"]:
        rings = [np.array([lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]], float)]
        return rings, [np.full((4, 3), np.nan)], "noloops", 0.0
    closed, open_ = [], {0: [], 1: []}
    for l in face["loops"]:
        pl = loop_polyline(l, lo, hi, mode) if l is not None else None
        if pl is None:
            return None, None, "nouv", 1.0
        uv, xyz = pl
        if len(uv) < 2:
            continue
        g = uv[-1] - uv[0]
        if abs(g[0]) / su < 1e-6 and abs(g[1]) / sv < 1e-6:
            if np.allclose(uv[0], uv[-1], rtol=0, atol=1e-12 * max(su, sv)):
                uv, xyz = uv[:-1], xyz[:-1]
            if len(uv) >= 3:
                closed.append((uv, xyz))
            continue
        for a, (span, other) in enumerate(((su, sv), (sv, su))):
            if abs(abs(g[a]) - span) / span < 1e-3 and abs(g[1 - a]) / other < 1e-6:
                open_[a].append((uv, xyz))
                break
        else:
            return None, None, "open", 1.0
    if open_[0] and open_[1]:
        return None, None, "open2axes", 1.0
    how = "closed"
    cands = [([c[0] for c in closed], [c[1] for c in closed])]
    for a in (0, 1):
        ops = open_[a]
        if not ops:
            continue
        how = "seam"
        sw = (lambda x: x) if a == 0 else (lambda x: x[:, ::-1])
        lo_a, per, lo_b, hi_b = (lo[0], su, lo[1], hi[1]) if a == 0 else (lo[1], sv, lo[0], hi[0])
        # candidate closures: each open loop closed straight from its end to its start
        # (right when the missing piece runs along a pole or domain line), and, once
        # unwrapped across the seam, closed along the domain boundary or as a band
        opts = [[(uv, xyz) for uv, xyz in ops]]
        un = [unwrap_open(sw(uv), xyz, lo_a, per) for uv, xyz in ops]
        if all(r is not None for r in un):
            nan2 = np.full((2, 3), np.nan)

            def cap(p, b):  # close an increasing polyline along the boundary line b
                w = np.vstack([p[0], [[lo_a + per, b], [lo_a, b]]])
                return sw(w), np.vstack([p[1], nan2])
            if len(un) == 1:
                opts += [[cap(un[0], hi_b)], [cap(un[0], lo_b)]]
            elif len(un) == 2:
                un.sort(key=lambda p: float(np.mean(p[0][:, 1])))
                band = (sw(np.vstack([un[0][0], un[1][0][::-1]])), np.vstack([un[0][1], un[1][1][::-1]]))
                opts += [[band], [cap(un[0], lo_b), cap(un[1], hi_b)]]
        cands = [(cands[0][0] + [o[0] for o in opt], cands[0][1] + [o[1] for o in opt]) for opt in opts]
    scored = [(mask_misfit(c[0], grid, lo, hi), c) for c in cands]
    mf, best = min(scored, key=lambda x: x[0])
    return best[0], best[1], how + ("" if mode == "straight" else "+" + mode), mf


def mesh_face(grid, face, step):
    """-> dict(uv, xyz, tri, gridk, method); xyz rows NaN where an evaluation is needed."""
    ax = axis(step)
    lo, hi = face["dom"] if face else (None, None)
    if face is None:
        return mask_mesh(grid, step, "noface")
    su, sv = hi[0] - lo[0], hi[1] - lo[1]
    if not (su > 0 and sv > 0):
        return mask_mesh(grid, step, "domain")
    rings, ring_xyz, why = build_rings(face, grid, lo, hi)
    if rings is None:
        return mask_mesh(grid, step, why)
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
    return {"uv": uv, "xyz": np.array(vxyz, float).reshape(-1, 3), "tri": tri, "gridk": np.array(vgk, np.int64), "method": "clip:" + why}


def mask_mesh(grid, step, why=""):
    """The earlier mesh: cells of the kernel's inside mask (4 corners -> 2, 3 -> 1)."""
    ax = axis(step)
    n = len(ax)
    if grid is None:
        return {"uv": np.zeros((0, 2)), "xyz": np.zeros((0, 3)), "tri": np.zeros((0, 3), np.int64), "gridk": np.zeros(0, np.int64), "method": "none:" + why}
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
    return {"uv": np.zeros((len(xyz), 2)), "xyz": np.array(xyz, float).reshape(-1, 3), "tri": np.array(tris, np.int64).reshape(-1, 3), "gridk": np.array(gks, np.int64), "method": "mask:" + why}


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
            meshes.append(mask_mesh(g, step, "error"))
    # evaluate the new vertices with the same kernel
    req = os.path.join(work, "mesh", f"{i}.{side}.s{step}.req")
    res = req[:-4] + ".res"
    need = [(fi, np.nonzero(np.isnan(m["xyz"][:, 0]))[0]) for fi, m in enumerate(meshes) if m["method"].startswith("clip")]
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
    nclip = sum(m.startswith("clip") for m in methods)
    print(f"{side} meshed {rel}: {len(meshes)} faces, {nclip} clipped, {len(methods) - nclip} other {sorted(set(m for m in methods if not m.startswith('clip')))}", flush=True)


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
