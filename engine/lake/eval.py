#!/usr/bin/env python3
"""Run and score evidence-lake cases; check that the protocol reproduces.

  eval.py run   [--cases FILE] [--only id,id]   build a lake per case under data/lake-evals/<stamp>/, score it
  eval.py score <eval_dir> [--cases FILE]       re-score existing lakes (no model calls)
  eval.py repro <case_id> [--cases FILE]        build the same case twice from scratch and compare the two lakes

Scores are computed from lake files only. Key papers are matched by DOI or by a title pattern at three depths:
found by the searches (records), selected for reading (selected.json), cited in the report (references).
"""
import argparse, json, os, re, sys, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lake  # noqa: E402
import review  # noqa: E402
from store import read_json, read_jsonl  # noqa: E402

DEFAULT_CASES = HERE.parents[1] / "packs" / "deep-research" / "cases.json"
EVAL_ROOT = Path(os.environ.get("LAKE_EVAL_ROOT") or Path(os.environ.get("HARNESS_DATA") or Path(__file__).resolve().parents[2] / "data") / "lake-evals").expanduser()  # see paths.py


def _match(paper: dict, rec: dict) -> bool:
    """Is this record the key paper? DOI decides when both sides have one, then PMID, then
    title. The title fallback alone matched Letters, Commentaries, Addenda and Errata, which
    carry the original's title verbatim (27 Oct 2026); it now applies only when there is no
    identifier to compare."""
    pdoi, rdoi = (paper.get("doi") or "").lower(), (rec.get("doi") or "").lower()
    if pdoi and rdoi:
        return pdoi == rdoi
    ppmid = str(paper.get("pmid") or "")
    rpmid = str(rec.get("pmid") or "") or str(rec.get("id") or "")
    if ppmid and rpmid:
        return ppmid == rpmid
    pat = (paper.get("title") or "").lower()
    return bool(pat) and pat in re.sub(r"\s+", " ", (rec.get("title") or "").lower())


def cited_keys(d: Path) -> set:
    ev = {e["eid"]: e for e in read_jsonl(d / "evidence.jsonl")}
    syn = read_json(d / "synthesis.json", {}) or {}
    blocks = list(syn.get("subquestions", {}).values()) + [{"points": syn.get("summary", [])}]
    return {ev[x]["key"] for b in blocks for p in b.get("points", []) if not p.get("dropped") for x in p.get("eids", []) if x in ev}


def score_case(case: dict, d: Path) -> dict:
    recs = read_jsonl(d / "records.jsonl")
    sel = (read_json(d / "selected.json", {}) or {}).get("docs", [])
    cited = cited_keys(d)
    by_key = {r["key"]: r for r in recs}
    keys = []
    for p in case.get("key_papers", []):
        found = [r for r in recs if _match(p, r)]
        keys.append({"label": p["label"], "found": bool(found),
                     "selected": any(_match(p, s) for s in sel),
                     "cited": any(_match(p, by_key.get(k, {})) for k in cited)})
    m = read_json(d / "metrics.json", {}) or {}
    report = (d / "report.md").read_text() if (d / "report.md").exists() else ""
    syn = read_json(d / "synthesis.json", {}) or {}
    texts = [p["text"] for b in list(syn.get("subquestions", {}).values()) + [{"points": syn.get("summary", [])}]
             for p in b.get("points", []) if not p.get("dropped")]
    premises = read_json(d / "premises.json", []) or []
    out = {
        "case": case["id"], "lake": str(d), "report": bool(report),
        "key_found": sum(k["found"] for k in keys), "key_selected": sum(k["selected"] for k in keys),
        "key_cited": sum(k["cited"] for k in keys), "key_total": len(keys), "keys": keys,
        "points": m.get("points"), "points_ok_first_pass": m.get("points_ok_first_pass"), "points_fixed": m.get("points_fixed"),
        "points_unverified": m.get("points_unverified"), "points_dropped": m.get("points_dropped"),
        "evidence_kept": m.get("evidence_kept"), "quotes_rejected": m.get("quotes_rejected"),
        "references": m.get("references"), "independent_references": m.get("independent_references"),
        "out_of_scope_excluded": len(((read_json(d / "selected.json", {}) or {}).get("rule") or {}).get("excluded_out_of_scope", [])),
        "llm_calls": m.get("llm_calls"), "llm_secs": round(sum(c.get("secs") or 0 for c in read_jsonl(d / "calls.jsonl")), 1),
    }
    if case.get("forbidden"):
        hits = [w for w in case["forbidden"] for t in texts if w.lower() in t.lower()
                and not re.search(r"not found|no (record|evidence|study|paper)|could not (be )?(found|located)|does not (appear|exist)", t, re.I)]
        out["forbidden_mentions"] = sorted(set(hits))
    if case.get("expect_premise_missing"):
        want = [w.lower() for w in case["expect_premise_missing"]]
        out["premise_flagged_missing"] = all(any(w in p["name"].lower() and not p.get("hits") for p in premises) for w in want)
        out["summary_says_not_found"] = any(re.search(r"not found|no (record|evidence|study|paper)|could not|does not (appear|exist)", p["text"], re.I)
                                            for p in syn.get("summary", []))
    if case.get("expect_gap"):
        out["gaps_reported"] = report.count("*Gaps:*")
    return out


def summarize(rows: list) -> str:
    cols = ["case", "key_found", "key_selected", "key_cited", "key_total", "points", "points_fixed", "points_unverified",
            "quotes_rejected", "references", "independent_references", "out_of_scope_excluded", "wall_secs"]
    extra = ["forbidden_mentions", "premise_flagged_missing", "summary_says_not_found", "gaps_reported", "error"]
    L = ["| " + " | ".join(c.replace("_", " ") for c in cols) + " | notes |", "|" + "---|" * (len(cols) + 1)]
    for r in rows:
        notes = "; ".join(f"{k}={r[k]}" for k in extra if k in r)
        L.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + f" | {notes} |")
    return "\n".join(L)


def load_cases(path, only=None):
    cases = json.loads(Path(path).read_text())["cases"]
    if only:
        ids = set(only.split(","))
        cases = [c for c in cases if c["id"] in ids]
    return cases


def build(case: dict, root: Path) -> tuple[Path, float, str | None]:
    lake.ROOT = root
    t0 = time.time()
    err = None
    try:
        lake.main(["run", case["question"], "--fresh"])
    except (Exception, SystemExit) as e:  # one failing case must not stop the pack
        err = f"{type(e).__name__}: {e}"
        print(f"eval: {case['id']} FAILED {err}", file=sys.stderr)
    return lake.new_lake(case["question"]), round(time.time() - t0, 1), err


def cmd_run(a):
    root = EVAL_ROOT / time.strftime("%Y%m%d-%H%M")
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in load_cases(a.cases, a.only):
        print(f"eval: {c['id']}", file=sys.stderr)
        d, secs, err = build(c, root)
        r = score_case(c, d)
        r["wall_secs"] = secs
        if err:
            r["error"] = err
        rows.append(r)
        (root / "scores.json").write_text(json.dumps(rows, indent=1))
        (root / "scores.md").write_text(summarize(rows) + "\n")
    print(summarize(rows))
    print(root / "scores.md")
    render(root)


def cmd_score(a):
    root = Path(a.target).expanduser()
    old = {r["case"]: r for r in (read_json(root / "scores.json", []) or [])}
    rows = []
    for c in load_cases(a.cases):
        lake.ROOT = root
        d = lake.new_lake(c["question"]) if (root / lake.slug_for(c["question"])).exists() else None
        if not d:
            continue
        r = score_case(c, d)
        r["wall_secs"] = old.get(c["id"], {}).get("wall_secs")
        rows.append(r)
    (root / "scores.json").write_text(json.dumps(rows, indent=1))
    (root / "scores.md").write_text(summarize(rows) + "\n")
    print(summarize(rows))
    render(root)


def render(root: Path):
    """The browsable page; a failure here must not lose a finished run."""
    try:
        review.main([str(root)])
    except Exception as e:
        print(f"review page failed: {type(e).__name__}: {e}", file=sys.stderr)


def jaccard(a: set, b: set):
    return round(len(a & b) / len(a | b), 3) if a | b else 1.0


def compare(d1: Path, d2: Path) -> dict:
    p1, p2 = read_json(d1 / "plan.json"), read_json(d2 / "plan.json")
    sub = lambda p: [s["text"] for s in p["subquestions"]]
    qs = lambda p: {q["query"] for s in p["subquestions"] for q in s["queries"]}
    keys = lambda d: {r["key"] for r in read_jsonl(d / "records.jsonl")}
    sel = lambda d: {x["key"] for x in (read_json(d / "selected.json", {}) or {}).get("docs", [])}
    evq = lambda d: {(e["key"], e["quote"]) for e in read_jsonl(d / "evidence.jsonl")}
    pts = lambda d: {p["text"] for b in (read_json(d / "synthesis.json", {}) or {}).get("subquestions", {}).values() for p in b["points"]}
    scr = lambda d: {(r["sid"], r["key"]): r["relevant"] for r in read_jsonl(d / "screen.jsonl")}
    s1, s2 = scr(d1), scr(d2)
    both = set(s1) & set(s2)
    return {
        "plan_identical": sub(p1) == sub(p2) and qs(p1) == qs(p2),
        "queries_jaccard": jaccard(qs(p1), qs(p2)),
        "records_jaccard": jaccard(keys(d1), keys(d2)),
        "screen_agreement": round(sum(s1[k] == s2[k] for k in both) / len(both), 3) if both else None,
        "selected_jaccard": jaccard(sel(d1), sel(d2)),
        "evidence_jaccard": jaccard(evq(d1), evq(d2)),
        "report_points_jaccard": jaccard(pts(d1), pts(d2)),
        "cited_papers_jaccard": jaccard(cited_keys(d1), cited_keys(d2)),
    }


def cmd_repro(a):
    case = [c for c in load_cases(a.cases) if c["id"] == a.target]
    if not case:
        raise SystemExit(f"no case {a.target}")
    stamp = time.strftime("%Y%m%d-%H%M")
    dirs = []
    for run in ("A", "B"):
        d, secs, err = build(case[0], EVAL_ROOT / f"repro-{stamp}-{run}")
        dirs.append(d)
    res = compare(*dirs)
    res["lakes"] = [str(x) for x in dirs]
    out = EVAL_ROOT / f"repro-{stamp}.json"
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "score", "repro"])
    ap.add_argument("target", nargs="?", help="eval dir (score) or case id (repro)")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    {"run": cmd_run, "score": cmd_score, "repro": cmd_repro}[a.cmd](a)


if __name__ == "__main__":
    main()
