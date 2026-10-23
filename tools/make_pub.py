#!/usr/bin/env python3
"""Make the public copy of the viewer: usage make_pub.py <sitedir> <pubdir>.

<sitedir> is a built site (web/* + build.py --step 2 --gzip + thumbnails). This copies
it into <pubdir> with the public title, methodology and attribution text, plus the
README, LICENSE, Pages workflow and the scripts that made the data (tools/).
"""
import os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
site, pub = sys.argv[1:3]

TITLE = "solidcore vs SMLib: upstream test models"
REPO = "https://github.com/tangency-labs/solidcore"
ABOUT = f"""<details id="about"><summary>About this page</summary>
<p>Each model is read by two kernels: SMLib, NVIDIA's C++ B-rep kernel (reference
build), and <a href="{REPO}">solidcore</a>, a Rust port of it. Both run the same
sampling:</p>
<ul>
<li>Each edge curve is evaluated at 48 parameters over its interval.</li>
<li>Each face gets an 88&times;88 grid over its UV domain. A grid point is kept where the
face's point classification (UV classification, face tolerance) puts it inside the
face, and the kept points are evaluated on the surface.</li>
<li>Each face's trim loops are sampled in UV along their edge uses' UV trim curves (24
samples per edge use) and evaluated on the surface.</li>
</ul>
<p>A model <b>matches</b> when both kernels have the same face and edge counts, the
same inside/outside mask at every grid point, and every grid, edge and trim-loop
sample within 1e-5 (outputs are written with 6 decimals).</p>
<p>Meshes: the grid cells are clipped to the face's trim loops (even-odd, Clipper) and
triangulated with earcut. Each new boundary vertex is evaluated on the surface by the
same kernel. Loops that close only across a periodic seam are unwrapped. Edge uses
with no UV trim curve, such as degenerate pole edges, are bridged in UV. A face whose
loop polygon still disagrees with the kernel's inside mask, or that has no UV trim
curves, uses the inside-mask cells instead. The meshes shown are built on every 2nd grid line (45&times;45 per
face). The agreement numbers come from the full 88&times;88 run.</p>
<p>Each model runs per kernel in its own process with a 60&nbsp;s limit, and solidcore
runs that hit the limit were retried with 300&nbsp;s. Times are wall-clock on one
shared workstation with other jobs running, measured for this sampling workload
only.</p>
<p>Models: NVIDIA SMLib test files (TestFiles/pt_TestFiles), Apache-2.0. SMLib is
NVIDIA's; solidcore is an independent Rust port. <a href="{REPO}">Source:
github.com/tangency-labs/solidcore</a>.</p>
</details>"""

if os.path.isdir(pub):
    for name in ("data", "thumb"):
        shutil.rmtree(os.path.join(pub, name), ignore_errors=True)
os.makedirs(pub, exist_ok=True)
for name in ("data", "thumb"):
    shutil.copytree(os.path.join(site, name), os.path.join(pub, name))
for f in ("app.js", "core.js", "view.html", "manifest.json"):
    shutil.copy(os.path.join(site, f), os.path.join(pub, f))

idx = open(os.path.join(site, "index.html")).read()
idx = idx.replace("<title>SMLib vs solidcore</title>", f"<title>{TITLE}</title>")
idx = re.sub(r'<h1 id="title">.*?</h1>', f'<h1 id="title">{TITLE}</h1>', idx)
idx, n = re.subn(r'<details id="about">.*?</details>', lambda m: ABOUT, idx, flags=re.S)
assert n == 1
idx = idx.replace("#about summary{cursor:pointer;color:var(--fg)}", "#about summary{cursor:pointer;color:var(--fg)}\n#about ul{margin:4px 0;padding-left:20px}")
open(os.path.join(pub, "index.html"), "w").write(idx)
app = open(os.path.join(pub, "app.js")).read().replace('" · SMLib vs solidcore"', '" · solidcore vs SMLib"')
open(os.path.join(pub, "app.js"), "w").write(app)

for f in ("README.md", "LICENSE", ".nojekyll"):
    shutil.copy(os.path.join(HERE, "pub", f), os.path.join(pub, f))
os.makedirs(os.path.join(pub, ".github/workflows"), exist_ok=True)
shutil.copy(os.path.join(HERE, "pub", "pages.yml"), os.path.join(pub, ".github/workflows/pages.yml"))
tools = os.path.join(pub, "tools")
shutil.rmtree(tools, ignore_errors=True)
os.makedirs(tools)
for f in ("smlib_wire.cpp", "run.py", "mesh.py", "build.py", "make_pub.py", "thumb_server.py", "web/thumbs.html"):
    shutil.copy(os.path.join(HERE, f), os.path.join(tools, os.path.basename(f)))
shutil.copy(os.path.join(HERE, "..", "crates/solidcore/examples/corpus/wire.rs"), os.path.join(tools, "wire.rs"))
print("published copy in", pub)
