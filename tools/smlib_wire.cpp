// Scratch viewer tool (not part of the harness): `smlib_wire <file> <out.json>` reads one
// upstream test file with the SMLib reference build and writes the same JSON as
// solidcore's `corpus --wire1`: edges sampled at 48 params, per face an 88x88 UV grid
// evaluated where SmFace::PointClassify (UV classification, face tolerance) says inside.
#include "StdAfx.h"
#include <SmBrep.h>
#include <SmBrepData.h>
#include <SmCacheMgr.h>
#include <SmContext.h>
#include <SmCurve.h>
#include <SmEdge.h>
#include <SmExtent1d.h>
#include <SmExtent2d.h>
#include <SmEdgeuse.h>
#include <SmFace.h>
#include <SmFaceuse.h>
#include <SmLoopuse.h>
#include <SmBSplineCurve.h>
#include <SmSurface.h>
#include <SmTArray.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static void pt(std::string& s, const SmPoint3d& p)
{
    char b[128];
    snprintf(b, sizeof b, "[%.6f,%.6f,%.6f]", p.x, p.y, p.z);
    s += b;
}

static bool inside(SmFace* f, const SmPoint2d& uv, double tol)
{
    SmSurface* srf = f->GetSurface();
    if (srf != nullptr && SmCacheMgr::GetObjectCache(SM_OC_TRIMSRF, srf) != nullptr)
        SmCacheMgr::DeleteObjectsCache(SM_OC_TRIMSRF, srf);
    SmPointClassification pc(SmZoneTol3d(tol), f->GetContext());
    SmStatus st = f->PointClassify(uv, SmZoneTol3d(tol), FALSE, TRUE, pc, FALSE, nullptr, TRUE);
    return st == SM_SUCCESS && pc.GetPointClass() != SM_PC_UNKNOWN;
}

static int write_out(const char* out, const std::string& s)
{
    FILE* fp = fopen(out, "wb");
    if (!fp)
        return 3;
    fwrite(s.data(), 1, s.size(), fp);
    fclose(fp);
    return 0;
}

static SmStatus read_model(SmContext& ctx, const char* path, SmTArray<SmBrep*>& breps)
{
    SmStatus st;
    {
        char head[20] = {0};
        FILE* fp = fopen(path, "rb");
        if (!fp)
            return SM_ERR;
        size_t n = fread(head, 1, 18, fp);
        fclose(fp);
        if (n == 18 && memcmp(head, "//[Output Summary]", 18) == 0)
        {
            SmTArray<SmCurve*> curves;
            SmTArray<SmSurface*> surfaces;
            SmTArray<long> trees;
            st = SmBrepData::ReadPartFromFile(ctx, path, curves, surfaces, trees, breps, SM_ASCII);
        }
        else
        {
            SmBrep* b = new (ctx) SmBrep();
            st = b->ReadFromFile(ctx, path, SM_ASCII, FALSE);
            breps.Add(b);
        }
    }
    return st;
}

static void all_faces(const SmTArray<SmBrep*>& breps, std::vector<SmFace*>& out)
{
    for (ULONG bi = 0; bi < breps.GetSize(); bi++)
    {
        SmTArray<SmFace*> fs;
        breps[bi]->GetFaces(fs);
        for (ULONG fi = 0; fi < fs.GetSize(); fi++)
            out.push_back(fs[fi]);
    }
}

static int loops_main(const char* path, const char* outp);
static int eval_main(const char* path, const char* req, const char* outp);

int main(int argc, char** argv)
{
    if (argc == 4 && strcmp(argv[1], "--loops") == 0)
        return loops_main(argv[2], argv[3]);
    if (argc == 5 && strcmp(argv[1], "--eval") == 0)
        return eval_main(argv[2], argv[3], argv[4]);
    if (argc != 3)
    {
        fprintf(stderr, "usage: smlib_wire <file> <out.json> | --loops <file> <out.json> | --eval <file> <req.txt> <out.txt>\n");
        return 2;
    }
    const char* path = argv[1];
    SmContext ctx;
    SmTArray<SmBrep*> breps;
    SmStatus st = read_model(ctx, path, breps);
    if (st != SM_SUCCESS)
    {
        std::string s = "{\"status\":\"error\",\"error\":\"read status " + std::to_string((int)st) + "\"}";
        write_out(argv[2], s);
        return 1;
    }
    std::string edges, grids;
    size_t ne = 0, nf = 0;
    const int G = 88;
    for (ULONG bi = 0; bi < breps.GetSize(); bi++)
    {
        SmBrep* b = breps[bi];
        SmTArray<SmEdge*> es;
        b->GetEdges(es);
        for (ULONG ei = 0; ei < es.GetSize(); ei++)
        {
            SmCurve* c = es[ei]->GetCurve();
            if (c == nullptr)
                continue;
            SmExtent1d iv = es[ei]->GetInterval();
            std::string e = "[";
            bool ok = true;
            for (int j = 0; j < 48 && ok; j++)
            {
                SmPoint3d p;
                ok = c->EvaluatePoint(iv.GetMin() + (iv.GetMax() - iv.GetMin()) * (double)j / 47.0, p) == SM_SUCCESS;
                if (j)
                    e += ",";
                pt(e, p);
            }
            if (!ok)
                continue;
            e += "]";
            if (ne++)
                edges += ",";
            edges += e;
        }
        SmTArray<SmFace*> fs;
        b->GetFaces(fs);
        for (ULONG fi = 0; fi < fs.GetSize(); fi++)
        {
            SmFace* f = fs[fi];
            if (nf++)
                grids += ",";
            SmSurface* s = f->GetSurface();
            if (s == nullptr)
            {
                grids += "null";
                continue;
            }
            SmExtent2d d = f->GetUVDomain();
            double tol = f->GetTolerance();
            SmPoint2d lo = d.GetMin(), hi = d.GetMax();
            grids += "[";
            for (int jv = 0; jv < G; jv++)
                for (int ju = 0; ju < G; ju++)
                {
                    SmPoint2d uv(lo.x + (hi.x - lo.x) * (double)ju / (double)(G - 1),
                                 lo.y + (hi.y - lo.y) * (double)jv / (double)(G - 1));
                    if (ju + jv > 0)
                        grids += ",";
                    SmPoint3d p;
                    if (inside(f, uv, tol) && s->EvaluatePoint(uv, p) == SM_SUCCESS)
                        pt(grids, p);
                    else
                        grids += "0";
                }
            grids += "]";
        }
    }
    std::string out = "{\"status\":\"ok\",\"nbreps\":" + std::to_string(breps.GetSize()) +
                      ",\"edges\":[" + edges + "],\"grids\":[" + grids + "]}";
    return write_out(argv[2], out);
}

// `--loops`: per face (GetFaces order over all B-reps), the trim loops of the primary
// face use: per loop use, its edge uses in order; per edge use EU_SAMPLES points of its
// UV trim curve over the edge interval (reversed for an opposite edge use), and the
// face surface evaluated at each (0 where evaluation fails). A face or loop without
// surface or trim curve is null.
static const int EU_SAMPLES = 24;

static void pt2(std::string& s, double u, double v)
{
    char b[96];
    snprintf(b, sizeof b, "[%.17g,%.17g]", u, v);
    s += b;
}

static int loops_main(const char* path, const char* outp)
{
    SmContext ctx;
    SmTArray<SmBrep*> breps;
    SmStatus st = read_model(ctx, path, breps);
    if (st != SM_SUCCESS)
    {
        write_out(outp, "{\"status\":\"error\",\"error\":\"read status " + std::to_string((int)st) + "\"}");
        return 1;
    }
    std::vector<SmFace*> faces;
    all_faces(breps, faces);
    std::string o = "{\"status\":\"ok\",\"faces\":[";
    for (size_t fi = 0; fi < faces.size(); fi++)
    {
        if (fi)
            o += ",";
        SmFace* f = faces[fi];
        SmSurface* srf = f->GetSurface();
        SmFaceuse *fu1 = nullptr, *fu2 = nullptr;
        f->GetFaceuses(fu1, fu2);
        if (srf == nullptr || fu1 == nullptr)
        {
            o += "null";
            continue;
        }
        SmExtent2d d = f->GetUVDomain();
        o += "{\"dom\":[";
        pt2(o, d.GetMin().x, d.GetMin().y);
        o += ",";
        pt2(o, d.GetMax().x, d.GetMax().y);
        o += "],\"loops\":[";
        SmTArray<SmLoopuse*> lus;
        fu1->GetLoopuses(lus);
        for (ULONG li = 0; li < lus.GetSize(); li++)
        {
            if (li)
                o += ",";
            SmTArray<SmEdgeuse*> eus;
            lus[li]->GetEdgeuses(eus);
            // per edge use a segment {"uv":[..],"xyz":[..]}, or {"miss":why} when it has
            // no edge, no UV trim curve, or the curve does not evaluate
            o += "{\"n\":" + std::to_string(eus.GetSize()) + ",\"segs\":[";
            for (ULONG ei = 0; ei < eus.GetSize(); ei++)
            {
                if (ei)
                    o += ",";
                SmEdgeuse* eu = eus[ei];
                SmBSplineCurve* c = eu->GetUVTrimCurve();
                SmEdge* e = eu->GetEdge();
                if (e == nullptr || c == nullptr)
                {
                    o += e == nullptr ? "{\"miss\":\"noedge\"}" : "{\"miss\":\"nocurve\"}";
                    continue;
                }
                SmExtent1d iv = e->GetInterval();
                bool rev = eu->GetOrientation() != SM_OT_SAME;
                std::string uv, xyz;
                bool ok = true;
                for (int j = 0; j < EU_SAMPLES && ok; j++)
                {
                    int jj = rev ? EU_SAMPLES - 1 - j : j;
                    double t = iv.GetMin() + (iv.GetMax() - iv.GetMin()) * (double)jj / (double)(EU_SAMPLES - 1);
                    SmPoint3d q;
                    if (c->EvaluatePoint(t, q) != SM_SUCCESS)
                    {
                        ok = false;
                        break;
                    }
                    if (j)
                    {
                        uv += ",";
                        xyz += ",";
                    }
                    pt2(uv, q.x, q.y);
                    SmPoint3d p;
                    if (srf->EvaluatePoint(SmPoint2d(q.x, q.y), p) == SM_SUCCESS)
                        pt(xyz, p);
                    else
                        xyz += "0";
                }
                if (ok)
                    o += "{\"uv\":[" + uv + "],\"xyz\":[" + xyz + "]}";
                else
                    o += "{\"miss\":\"evalfail\"}";
            }
            o += "]}";
        }
        o += "]}";
    }
    o += "]}";
    return write_out(outp, o);
}

// `--eval`: request lines `face_index count` then `count` lines `u v`; writes one line
// `x y z` (or `nan`) per point, in request order.
static int eval_main(const char* path, const char* req, const char* outp)
{
    SmContext ctx;
    SmTArray<SmBrep*> breps;
    if (read_model(ctx, path, breps) != SM_SUCCESS)
        return 1;
    std::vector<SmFace*> faces;
    all_faces(breps, faces);
    FILE* in = fopen(req, "r");
    FILE* out = fopen(outp, "w");
    if (!in || !out)
        return 3;
    long fi, n;
    while (fscanf(in, "%ld %ld", &fi, &n) == 2)
    {
        SmSurface* srf = (fi >= 0 && (size_t)fi < faces.size()) ? faces[(size_t)fi]->GetSurface() : nullptr;
        for (long k = 0; k < n; k++)
        {
            double u, v;
            if (fscanf(in, "%lf %lf", &u, &v) != 2)
                return 4;
            SmPoint3d p;
            if (srf && srf->EvaluatePoint(SmPoint2d(u, v), p) == SM_SUCCESS)
                fprintf(out, "%.17g %.17g %.17g\n", p.x, p.y, p.z);
            else
                fprintf(out, "nan\n");
        }
    }
    fclose(in);
    fclose(out);
    return 0;
}
