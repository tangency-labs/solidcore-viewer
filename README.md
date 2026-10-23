# solidcore vs SMLib: upstream test models

A static viewer at <https://tangency-labs.github.io/solidcore-viewer/>. It shows the
262 B-rep part files of the SMLib test corpus rendered through two kernels: SMLib,
NVIDIA's C++ B-rep kernel (reference build), and
[solidcore](https://github.com/tangency-labs/solidcore), a Rust port of it. The page
has a thumbnail gallery and a side-by-side, overlay and diff 3D view per model.

## Method

Both kernels read each model and run the same sampling:

- Each edge curve is evaluated at 48 parameters over its interval.
- Each face gets an 88×88 grid over its UV domain. A grid point is kept where the face's
  point classification (UV classification, face tolerance) puts it inside the face, and
  the kept points are evaluated on the surface.
- Each face's trim loops are sampled in UV along their edge uses' UV trim curves (24
  samples per edge use) and evaluated on the surface.

A model **matches** when both kernels have the same face and edge counts, the same
inside/outside mask at every grid point, and every grid, edge and trim-loop sample within
1e-5 (outputs are written with 6 decimals).

**Meshes.** Grid cells are clipped to each face's trim loops (even-odd, with Clipper
via pyclipper) and triangulated with earcut. Each new boundary vertex is evaluated on
the surface by the same kernel. Faces whose loops are missing or not closed in UV use
the inside-mask cells instead. The published meshes use every 2nd grid line (45×45 per
face), quantized to 16 bits and gzipped. All agreement numbers come from the full 88×88
run.

**Timeouts and times.** Each model runs per kernel in its own process with a 60 s limit,
and solidcore runs that hit the limit were retried with 300 s. Times are wall-clock on
one shared workstation with other jobs running, measured for this sampling workload
only.

## Layout

- `index.html`, `app.js`, `core.js`: the gallery and the model view. three.js r128 is
  loaded from a CDN, pinned with SRI.
- `manifest.json`: per-model status, timings and agreement metrics.
- `data/<model>.bin.gz`: quantized meshes and edges for both kernels. `tools/build.py`
  describes the format.
- `thumb/<model>.webp`: gallery thumbnails, rendered from `tools/thumbs.html`.
- `tools/`: the scripts that made the data, with paths set for the machine they ran on:
  - `smlib_wire.cpp`: C++, links the SMLib build.
  - `wire.rs`: the solidcore example mode.
  - `run.py`: one process per file and kernel, with timeouts.
  - `mesh.py`: trim-loop clipping.
  - `build.py`: data files and metrics.
  - `thumb_server.py` and `thumbs.html`: thumbnails.
  - `make_pub.py`: builds this copy.

## Attribution and licence

Models: NVIDIA SMLib test files (TestFiles/pt_TestFiles), Apache-2.0. SMLib is NVIDIA's;
solidcore is an independent Rust port. This repository holds only meshes derived from
those files, the viewer and its scripts. It is licensed under Apache-2.0 (see `LICENSE`).

The viewer loads three.js (MIT) from a CDN. The data scripts use numpy (BSD), pyclipper
(MIT; Clipper, Boost Software License) and mapbox_earcut (ISC).
