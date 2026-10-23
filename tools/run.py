#!/usr/bin/env python3
"""Run SMLib (C++) and solidcore (Rust) wire dumps on corpus models, one child process
per file and side, 60 s timeout, at most 4 at once under nice -n 10.

usage: run.py <workdir> [--only PREFIX,...] [--force] [--timeout S] [--jobs N]
              [--side cpp|rs] [--retry-timeouts]
--retry-timeouts reruns only models whose recorded status is timeout (keeping a note of
the earlier limit in `retried_after`).
Writes <workdir>/raw/<id>.<side>.json and <workdir>/status/<id>.<side>.json.
"""
import json, os, subprocess, sys, time, signal
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.expanduser("~/github/solidcore/upstream/TestFiles/pt_TestFiles")
BINS = {
    "cpp": [os.path.join(HERE, "bin/smlib_wire"), "{f}", "{o}"],
    "rs": [os.path.expanduser("~/github/solidcore-wt/render/bin/release/examples/corpus"), "--wire1", "{f}", "{o}"],
}
TIMEOUT, JOBS, MAXLOAD = 60, 4, 24.0


def models():
    out = []
    for root, _, files in os.walk(CORPUS):
        for n in files:
            if n.endswith((".smb", ".smp")):
                out.append(os.path.relpath(os.path.join(root, n), CORPUS))
    return sorted(out)


def mid(rel):
    return rel.replace("/", "__").replace(".", "_")


def load():
    return float(open("/proc/loadavg").read().split()[0])


def run_one(work, rel, side, force, retry=False):
    i = mid(rel)
    st_path = os.path.join(work, "status", f"{i}.{side}.json")
    prev = None
    if retry:
        try:
            prev = json.load(open(st_path))
        except (OSError, ValueError):
            return
        if prev.get("status") != "timeout":
            return
    elif not force and os.path.exists(st_path):
        return
    while load() > MAXLOAD:
        time.sleep(15)
    raw = os.path.join(work, "raw", f"{i}.{side}.json")
    if os.path.exists(raw):
        os.remove(raw)
    cmd = ["nice", "-n", "10"] + [a.format(f=os.path.join(CORPUS, rel), o=raw) for a in BINS[side]]
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
        _, err = p.communicate(timeout=TIMEOUT)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)  # our own child's process group
        p.communicate()
        rc = None
        err = b""
    dt = time.monotonic() - t0
    st = {"time": round(dt, 2), "limit": TIMEOUT}
    if prev is not None:
        st["retried_after"] = prev.get("limit", 60)
    if rc is None:
        st.update(status="timeout", error=f"no result after {TIMEOUT}s")
    else:
        body = None
        try:
            with open(raw) as fh:
                head = fh.read(400)
            if head.startswith('{"status":"error"'):
                body = json.loads(open(raw).read())
        except OSError:
            pass
        if rc == 0 and os.path.exists(raw):
            st.update(status="ok")
        elif body is not None:
            st.update(status="error", error=body.get("error", ""))
        else:
            tail = err.decode(errors="replace").strip().splitlines()[-3:]
            sig = f"signal {-rc}" if rc < 0 else f"exit {rc}"
            st.update(status="crash", error=(sig + (": " + " | ".join(tail) if tail else ""))[:400])
    json.dump(st, open(st_path, "w"))
    print(f"{side:3} {st['status']:7} {dt:6.1f}s {rel}", flush=True)


def main():
    global TIMEOUT, JOBS
    work = sys.argv[1]
    a = sys.argv
    if "--timeout" in a:
        TIMEOUT = int(a[a.index("--timeout") + 1])
    if "--jobs" in a:
        JOBS = int(a[a.index("--jobs") + 1])
    sides = [a[a.index("--side") + 1]] if "--side" in a else ["cpp", "rs"]
    retry = "--retry-timeouts" in a
    only = None
    force = "--force" in sys.argv
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].split(",")
    os.makedirs(os.path.join(work, "raw"), exist_ok=True)
    os.makedirs(os.path.join(work, "status"), exist_ok=True)
    ms = models()
    if only:
        ms = [m for m in ms if any(m.startswith(p) for p in only)]
    jobs = [(m, s) for m in ms for s in sides]
    with ThreadPoolExecutor(JOBS) as ex:
        for f in [ex.submit(run_one, work, m, s, force, retry) for m, s in jobs]:
            f.result()


if __name__ == "__main__":
    main()
