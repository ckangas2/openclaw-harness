#!/usr/bin/env python3
"""Build a report from a graded re-screen, on a COPY of the case. The original run is not touched.

1. copy     the case folder -> <repo>/data/experiments/graded-lakes/<case>-graded-<stamp>/ (original screen.jsonl kept as screen.orig.jsonl)
2. adapt    graded.jsonl -> screen.jsonl: one row per sub-question in routes_to (all of them, not just the first),
            relevant = grade >= --cut. A grade>=cut record with empty routes_to goes to the sub-questions that
            retrieved it. The original rule rows (no abstract, retracted) are kept.
3. select   like stage_select, with three changes, all logged in selected.json:
            - score = type_weight + GRADE_W * grade + 0.3*log1p(cited_by)   (grade was ignored before)
            - out-of-scope records are no longer dropped: up to OOS_PER_SUB per sub-question, in their own
              quota so they cannot crowd out in-scope evidence; marked in_scope=false on the doc
            - K_PER_SUB and MAX_DOCS from the usual env vars (LAKE_K_PER_SUB, LAKE_MAX_DOCS)
4. run      the lake's own stages after select (fetch, extract, synthesize, check, report), unchanged

    python3 engine/lake/graded_run.py <case-dir> <graded-dir> --until select     # look at the selection first
    python3 engine/lake/graded_run.py --resume <new-lake>                        # then run the rest
    python3 engine/lake/graded_run.py <case-dir> <graded-dir>                    # or everything in one go

Do not run during an agent eval: the later stages use the GPU.
"""
import argparse, json, math, os, shutil, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import EXPERIMENTS  # noqa: E402  <repo>/data/experiments, or $HARNESS_DATA/experiments

GRADE_W = float(os.environ.get("LAKE_GRADE_W", "2.0"))  # one grade step ~ an 800-fold citation difference
OOS_PER_SUB = int(os.environ.get("LAKE_OOS_PER_SUB", "2"))


def adapt(case: Path, graded_rows: list, cut: int) -> list:
    """graded rows -> screen.jsonl rows in the lake's format (plus grade/bearing/use_as/routes)."""
    recs = {}
    for l in open(case / "records.jsonl"):
        r = json.loads(l); recs[r["key"]] = r
    orig = [json.loads(l) for l in open(case / "screen.jsonl") if l.strip()]
    out = [r for r in orig if r.get("by") == "rule"]
    ruled = {(r["sid"], r["key"]) for r in out}
    for g in graded_rows:
        k = g["key"]
        if k not in recs:
            continue
        grade = g.get("relevance") or 0
        retrieved = sorted({h["sid"] for h in recs[k].get("hits", []) if h.get("sid")})
        routes = [s for s in (g.get("routes_to") or [])]
        sids = routes if (routes and grade >= cut) else retrieved
        for s in dict.fromkeys(sids):
            if (s, k) in ruled:
                continue
            out.append({"sid": s, "key": k, "relevant": grade >= cut, "grade": grade,
                        "evidence_type": g.get("evidence_type", "other"), "in_scope": g.get("in_scope", True),
                        "bearing": g.get("bearing", []), "use_as": g.get("use_as"), "routes_to": routes,
                        "rerouted": s not in retrieved, "reason": g.get("reason", ""), "by": "graded"})
    return out


def select(lake: Path, ev) -> dict:
    """stage_select with grade in the score and an out-of-scope quota. Same output format."""
    plan = json.loads((lake / "plan.json").read_text())
    recs = {}
    for l in open(lake / "records.jsonl"):
        r = json.loads(l); recs[r["key"]] = r
    rows = [json.loads(l) for l in open(lake / "screen.jsonl") if l.strip()]
    rel = [r for r in rows if r.get("relevant") and r["key"] in recs]
    chosen = {}
    for s in plan["subquestions"]:
        scored = []
        for r in (x for x in rel if x["sid"] == s["id"]):
            rec, et, gr = recs[r["key"]], r.get("evidence_type", "other"), r.get("grade") or 0
            score = ev.TYPE_WEIGHT.get(et, 0.5) + GRADE_W * gr + 0.3 * math.log1p(rec.get("cited_by") or 0)
            scored.append((round(score, 3), et, r, rec))
        scored.sort(key=lambda x: (-x[0], x[3]["key"]))
        n_other = n_oos = 0
        for score, et, r, rec in scored:
            oos = r.get("in_scope") is False
            if oos:
                if n_oos >= OOS_PER_SUB:
                    continue
                n_oos += 1
            else:
                if not (et in ev.ALWAYS_KEEP or n_other < ev.K_PER_SUB):
                    continue
                if et not in ev.ALWAYS_KEEP:
                    n_other += 1
            c = chosen.setdefault(rec["key"], {
                "key": rec["key"], "pmcid": rec.get("pmcid"), "title": rec.get("title"), "pub_year": rec.get("pub_year"),
                "doi": rec.get("doi"), "url": rec.get("url"), "journal": rec.get("journal"),
                "authors": (rec.get("authors") or [])[:3], "cited_by": rec.get("cited_by"),
                "is_preprint": rec.get("is_preprint"), "is_retracted": rec.get("is_retracted"),
                "evidence_type": et, "sids": [], "score": 0, "grade": r.get("grade"), "in_scope": True,
                "bearing": r.get("bearing", []), "use_as": r.get("use_as")})
            c["sids"].append(s["id"])
            c["score"] = max(c["score"], score)
            c["grade"] = max(c.get("grade") or 0, r.get("grade") or 0)
            if oos:
                c["in_scope"] = False
            if et in ev.ALWAYS_KEEP:
                c["evidence_type"] = et
    docs = sorted(chosen.values(), key=lambda c: (-c["score"], c["key"]))[:ev.MAX_DOCS]
    sel = {"rule": {"type_weight": ev.TYPE_WEIGHT, "always_keep": sorted(ev.ALWAYS_KEEP), "k_per_sub": ev.K_PER_SUB,
                    "max_docs": ev.MAX_DOCS, "grade_weight": GRADE_W, "out_of_scope_per_sub": OOS_PER_SUB,
                    "excluded_out_of_scope": [],
                    "score": f"type_weight + {GRADE_W}*grade + 0.3*log1p(cited_by); out-of-scope in own quota",
                    "source": "graded_run.py"}, "docs": docs}
    (lake / "selected.json").write_text(json.dumps(sel, indent=1))
    return sel


def describe(sel, plan):
    docs = sel["docs"]
    print(f"\nselected {len(docs)} docs (K_PER_SUB={sel['rule']['k_per_sub']}, MAX_DOCS={sel['rule']['max_docs']}, "
          f"grade weight {sel['rule']['grade_weight']}, out-of-scope quota {sel['rule']['out_of_scope_per_sub']}/sub)")
    for s in plan["subquestions"]:
        ds = [d for d in docs if s["id"] in d["sids"]]
        print(f"  {s['id']}: {len(ds)} docs, grades {sorted((d.get('grade') or 0 for d in ds), reverse=True)}, "
              f"out of scope {sum(1 for d in ds if d.get('in_scope') is False)}")
    print(f"  grade 3: {sum(1 for d in docs if d.get('grade') == 3)}, out of scope total: "
          f"{sum(1 for d in docs if d.get('in_scope') is False)}")


def run_after_select(lake: Path, lakemod, until: str):
    stages = lakemod.STAGES
    for st in stages[stages.index("select") + 1: stages.index(until) + 1]:
        print(f"\n== {st}", file=sys.stderr)
        lakemod.FUNCS[st](lake)
    lakemod.status(lake)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", nargs="?", type=Path)
    ap.add_argument("graded", nargs="?", type=Path, help="the rescreen_graded output folder (holds graded.jsonl)")
    ap.add_argument("--cut", type=int, default=2, help="grade counted as relevant (default 2)")
    ap.add_argument("--until", default="report")
    ap.add_argument("--resume", type=Path, help="a lake made by this script: run the stages after select")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)

    import lake as lakemod
    import evidence as ev

    if a.resume:
        lake = a.resume.expanduser().resolve()
        if not (lake / "selected.json").exists() or json.loads((lake / "selected.json").read_text())["rule"].get("source") != "graded_run.py":
            raise SystemExit(f"{lake} has no selection made by graded_run.py")
        run_after_select(lake, lakemod, a.until)
        return 0
    if not (a.case and a.graded):
        ap.error("give <case-dir> <graded-dir>, or --resume <lake>")

    case, gdir = a.case.expanduser().resolve(), a.graded.expanduser().resolve()
    graded = [json.loads(l) for l in open(gdir / "graded.jsonl") if l.strip()]
    lake = (a.out or EXPERIMENTS / "graded-lakes" / f"{case.name}-graded-{time.strftime('%Y%m%d-%H%M')}").expanduser()
    if lake.exists():
        raise SystemExit(f"{lake} exists")
    lake.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case, lake)
    lakemod.invalidate(lake, "select")            # clears select and every later stage's outputs, in the copy
    for f in ("selected.json",):
        (lake / f).unlink(missing_ok=True)
    shutil.copy(lake / "screen.jsonl", lake / "screen.orig.jsonl")
    rows = adapt(case, graded, a.cut)
    with open(lake / "screen.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    (lake / "graded_from.txt").write_text(f"{gdir}\ncut={a.cut}\n")
    print(lake)
    print(f"screen.jsonl: {len(rows)} rows, relevant {sum(1 for r in rows if r.get('relevant'))}, "
          f"rerouted {sum(1 for r in rows if r.get('relevant') and r.get('rerouted'))}")
    sel = select(lake, ev)
    from store import manifest_update
    manifest_update(lake, "select", {"docs": len(sel["docs"]), "source": "graded_run.py"})
    describe(sel, json.loads((lake / "plan.json").read_text()))
    if a.until == "select":
        print(f"\nlooks right? then:  python3 engine/lake/graded_run.py --resume {lake}")
        return 0
    run_after_select(lake, lakemod, a.until)
    return 0


if __name__ == "__main__":
    sys.exit(main())
