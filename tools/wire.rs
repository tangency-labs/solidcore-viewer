//! Scratch: `--wire <out.json> <file>...` dumps edges and iso lines of each model.
use super::read_breps;
use solidcore::intersect::point_class::{PointClassType, PointClassification};
use solidcore::kernel::Kernel;
use solidcore::math::{Point2, Point3};
use solidcore::query::face::p2::{point_classify, PointClassifyOptions};
use solidcore::topology::brep::p1 as bl;
use solidcore::topology::edge as el;
use solidcore::topology::face::p2 as fl;
use solidcore::topology::{Brep, FaceId};
use std::fmt::Write as _;

fn inside(k: &Kernel, b: &mut Brep, f: FaceId, uv: Point2, tol: f64) -> bool {
    let mut pc = PointClassification::new(tol);
    let opts = PointClassifyOptions { src_zone_tol_3d: tol, use_uv_classification: true, ..PointClassifyOptions::default() };
    point_classify(k, b, f, uv, &mut pc, opts).is_ok() && pc.point_class() != PointClassType::Unknown
}

fn lines(v: &[Vec<Point3>]) -> String {
    v.iter().map(|l| format!("[{}]", l.iter().map(|p| format!("[{:.6},{:.6},{:.6}]", p.x, p.y, p.z)).collect::<Vec<_>>().join(","))).collect::<Vec<_>>().join(",")
}

pub fn dump(args: &[String]) -> Result<(), String> {
    let (out, files) = args.split_first().ok_or("--wire <out> <file>...")?;
    let mut items = Vec::new();
    for file in files {
        let bytes = std::fs::read(file).map_err(|e| e.to_string())?;
        let mut k = Kernel::default();
        let Ok(read) = read_breps(&mut k, bytes) else { eprintln!("read fail {file}"); continue };
        let (mut edges, mut isos, mut grids) = (Vec::new(), Vec::new(), Vec::new());
        for mut b in read.breps {
            for e in bl::edges(&b, None).unwrap_or_default() {
                let (Ok(Some(c)), Ok(i)) = (el::curve(&b, e), el::interval(&b, e)) else { continue };
                let pts: Option<Vec<Point3>> = (0..48).map(|j| c.evaluate_point(&k, i.min() + (i.max() - i.min()) * j as f64 / 47.0).ok()).collect();
                if let Some(p) = pts { edges.push(p); }
            }
            for f in bl::faces(&b, None).unwrap_or_default() {
                let s = fl::surface(&b, f).map(|s| s.cloned());
                let (Ok(Some(s)), Ok(d), Ok(tol)) = (s, fl::uv_domain(&b, f), fl::tolerance(&b, f)) else { continue };
                let (lo, hi) = (d.min(), d.max());
                const G: usize = 88;
                let mut g = String::from("[");
                for jv in 0..G {
                    for ju in 0..G {
                        let uv = Point2::new(lo.x + (hi.x - lo.x) * ju as f64 / (G - 1) as f64, lo.y + (hi.y - lo.y) * jv as f64 / (G - 1) as f64);
                        let p = if inside(&k, &mut b, f, uv, tol) { s.evaluate_point(&k, uv).ok() } else { None };
                        if ju + jv > 0 { g.push(','); }
                        match p { Some(p) => { let _ = write!(g, "[{:.6},{:.6},{:.6}]", p.x, p.y, p.z); } None => g.push_str("0") }
                    }
                }
                g.push(']');
                grids.push(g);
                for along_v in [true, false] {
                    for i in 1..=4 {
                        let fixed = i as f64 / 5.0;
                        let mut run = Vec::new();
                        for j in 0..32 {
                            let free = j as f64 / 31.0;
                            let (a, c) = if along_v { (fixed, free) } else { (free, fixed) };
                            let uv = Point2::new(lo.x + (hi.x - lo.x) * a, lo.y + (hi.y - lo.y) * c);
                            let p = if inside(&k, &mut b, f, uv, tol) { s.evaluate_point(&k, uv).ok() } else { None };
                            if let Some(p) = p { run.push(p) } else if run.len() > 1 { isos.push(std::mem::take(&mut run)) } else { run.clear() }
                        }
                        if run.len() > 1 { isos.push(run) }
                    }
                }
            }
        }
        let name = std::path::Path::new(file).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
        let mut s = String::new();
        let _ = write!(s, "{{\"name\":\"{name}\",\"edges\":[{}],\"isos\":[{}],\"grids\":[{}]}}", lines(&edges), lines(&isos), grids.join(","));
        eprintln!("{name}: {} edges {} isos", edges.len(), isos.len());
        items.push(s);
    }
    std::fs::write(out, format!("[{}]", items.join(","))).map_err(|e| e.to_string())
}

/// `--wire1 <file> <out.json>`: one model, the same JSON as `viewer/smlib_wire.cpp`
/// (edges at 48 params, an 88x88 UV grid per face, `null` for a face without surface).
pub fn dump1(args: &[String]) -> Result<(), String> {
    let [file, out] = args else { return Err("--wire1 <file> <out>".into()) };
    let bytes = std::fs::read(file).map_err(|e| e.to_string())?;
    let mut k = Kernel::default();
    let read = match read_breps(&mut k, bytes) {
        Ok(r) => r,
        Err(e) => {
            let msg = format!("{e}").replace('\\', "\\\\").replace('"', "'");
            std::fs::write(out, format!("{{\"status\":\"error\",\"error\":\"{msg}\"}}")).map_err(|e| e.to_string())?;
            std::process::exit(1);
        }
    };
    let nbreps = read.breps.len();
    let (mut edges, mut grids) = (Vec::new(), Vec::new());
    for mut b in read.breps {
        for e in bl::edges(&b, None).unwrap_or_default() {
            let (Ok(Some(c)), Ok(i)) = (el::curve(&b, e), el::interval(&b, e)) else { continue };
            let pts: Option<Vec<Point3>> = (0..48).map(|j| c.evaluate_point(&k, i.min() + (i.max() - i.min()) * j as f64 / 47.0).ok()).collect();
            if let Some(p) = pts { edges.push(p); }
        }
        for f in bl::faces(&b, None).unwrap_or_default() {
            let s = fl::surface(&b, f).map(|s| s.cloned());
            let (Ok(Some(s)), Ok(d), Ok(tol)) = (s, fl::uv_domain(&b, f), fl::tolerance(&b, f)) else { grids.push("null".to_string()); continue };
            let (lo, hi) = (d.min(), d.max());
            const G: usize = 88;
            let mut g = String::from("[");
            for jv in 0..G {
                for ju in 0..G {
                    let uv = Point2::new(lo.x + (hi.x - lo.x) * ju as f64 / (G - 1) as f64, lo.y + (hi.y - lo.y) * jv as f64 / (G - 1) as f64);
                    let p = if inside(&k, &mut b, f, uv, tol) { s.evaluate_point(&k, uv).ok() } else { None };
                    if ju + jv > 0 { g.push(','); }
                    match p { Some(p) => { let _ = write!(g, "[{:.6},{:.6},{:.6}]", p.x, p.y, p.z); } None => g.push('0') }
                }
            }
            g.push(']');
            grids.push(g);
        }
    }
    let s = format!("{{\"status\":\"ok\",\"nbreps\":{nbreps},\"edges\":[{}],\"grids\":[{}]}}", lines(&edges), grids.join(","));
    std::fs::write(out, s).map_err(|e| e.to_string())
}

fn all_faces(breps: &[Brep]) -> Vec<(usize, FaceId)> {
    let mut v = Vec::new();
    for (bi, b) in breps.iter().enumerate() {
        for f in bl::faces(b, None).unwrap_or_default() {
            v.push((bi, f));
        }
    }
    v
}

const EU_SAMPLES: usize = 24;

/// `--loops <file> <out.json>`: per face, the trim loops of the primary face use, as
/// `viewer/smlib_wire.cpp --loops` writes them.
pub fn loops(args: &[String]) -> Result<(), String> {
    use solidcore::nurbs::b_spline_surface_n_lib::p2::OrientType;
    use solidcore::topology::edgeuse::p1 as eu1;
    use solidcore::topology::loopuse as lul;
    let [file, out] = args else { return Err("--loops <file> <out>".into()) };
    let bytes = std::fs::read(file).map_err(|e| e.to_string())?;
    let mut k = Kernel::default();
    let read = match read_breps(&mut k, bytes) {
        Ok(r) => r,
        Err(e) => {
            let msg = format!("{e}").replace('\\', "\\\\").replace('"', "'");
            std::fs::write(out, format!("{{\"status\":\"error\",\"error\":\"{msg}\"}}")).map_err(|e| e.to_string())?;
            std::process::exit(1);
        }
    };
    let mut o = String::from("{\"status\":\"ok\",\"faces\":[");
    for (n, (bi, f)) in all_faces(&read.breps).into_iter().enumerate() {
        let b = &read.breps[bi];
        if n > 0 { o.push(','); }
        let s = fl::surface(b, f).map(|s| s.cloned());
        let fu = b.face(f).ok().and_then(|r| r.faceuse);
        let (Ok(Some(s)), Some(fu), Ok(d)) = (s, fu, fl::uv_domain(b, f)) else { o.push_str("null"); continue };
        let _ = write!(o, "{{\"dom\":[[{},{}],[{},{}]],\"loops\":[", d.min().x, d.min().y, d.max().x, d.max().y);
        for (li, lu) in eu1::faceuse_loopuses(b, fu, false, None).unwrap_or_default().into_iter().enumerate() {
            if li > 0 { o.push(','); }
            let eus = lul::edgeuses(b, lu, None, None).unwrap_or_default();
            let (mut uv, mut xyz) = (Vec::new(), Vec::new());
            let mut ok = !eus.is_empty();
            'eu: for &eu in &eus {
                let c = eu1::uv_trim_curve(b, eu).ok().and_then(|(c, _)| c);
                let e = eu1::edge(b, eu).ok().flatten();
                let (Some(c), Some(e)) = (c, e) else { ok = false; break };
                let Ok(iv) = el::interval(b, e) else { ok = false; break };
                let rev = eu1::orientation(b, eu).ok() != Some(OrientType::Same);
                for j in 0..EU_SAMPLES {
                    let jj = if rev { EU_SAMPLES - 1 - j } else { j };
                    let t = iv.min() + (iv.max() - iv.min()) * jj as f64 / (EU_SAMPLES - 1) as f64;
                    let Ok(q) = c.evaluate_point(&k, t) else { ok = false; break 'eu };
                    uv.push(format!("[{},{}]", q.x, q.y));
                    xyz.push(match s.evaluate_point(&k, Point2::new(q.x, q.y)) {
                        Ok(p) => format!("[{:.6},{:.6},{:.6}]", p.x, p.y, p.z),
                        Err(_) => "0".into(),
                    });
                }
            }
            if ok {
                let _ = write!(o, "{{\"n\":{},\"uv\":[{}],\"xyz\":[{}]}}", eus.len(), uv.join(","), xyz.join(","));
            } else {
                o.push_str("null");
            }
        }
        o.push_str("]}");
    }
    o.push_str("]}");
    std::fs::write(out, o).map_err(|e| e.to_string())
}

/// `--eval <file> <req.txt> <out.txt>`: surface points for `face count` + `u v` lines.
pub fn eval(args: &[String]) -> Result<(), String> {
    let [file, req, out] = args else { return Err("--eval <file> <req> <out>".into()) };
    let bytes = std::fs::read(file).map_err(|e| e.to_string())?;
    let mut k = Kernel::default();
    let read = read_breps(&mut k, bytes).map_err(|e| e.to_string())?;
    let faces = all_faces(&read.breps);
    let text = std::fs::read_to_string(req).map_err(|e| e.to_string())?;
    let mut it = text.split_ascii_whitespace();
    let mut o = String::new();
    while let (Some(fi), Some(n)) = (it.next(), it.next()) {
        let fi: usize = fi.parse().map_err(|_| "bad face")?;
        let n: usize = n.parse().map_err(|_| "bad count")?;
        let s = faces.get(fi).and_then(|&(bi, f)| fl::surface(&read.breps[bi], f).ok().flatten());
        for _ in 0..n {
            let u: f64 = it.next().and_then(|x| x.parse().ok()).ok_or("bad u")?;
            let v: f64 = it.next().and_then(|x| x.parse().ok()).ok_or("bad v")?;
            match s.map(|s| s.evaluate_point(&k, Point2::new(u, v))) {
                Some(Ok(p)) => { let _ = writeln!(o, "{:e} {:e} {:e}", p.x, p.y, p.z); }
                _ => o.push_str("nan\n"),
            }
        }
    }
    std::fs::write(out, o).map_err(|e| e.to_string())
}
