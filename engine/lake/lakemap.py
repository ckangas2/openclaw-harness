#!/usr/bin/env python3
"""Where are we in the pipeline? One read-only page of pipeline state for a lake or an eval run.

No model calls, no network, nothing written. Reads whatever stage files exist and degrades
gracefully when they don't (an eval case folder carries fewer files than a live lake).

    python3 engine/lake/lakemap.py <lake>                     # one lake
    python3 engine/lake/lakemap.py <eval-dir>                 # every case in a run
    python3 engine/lake/lakemap.py <dir> --cases packs/deep-research/cases-ohsv.json
    python3 engine/lake/lakemap.py <dir> --drops 12           # show more dropped-at-select rows
    python3 engine/lake/lakemap.py <dir> --json               # machine-readable

Gold recall: a cases file is auto-detected from <repo>/packs/deep-research/*.json and matched to a
lake by slug (same rule as lake.py). Key papers are matched on DOI, PMID or a title substring, at
three depths -- found by the searches, selected for reading, cited in the report.

If a case's key_papers carry "role"/"group" (see --explain-roles), requirements are scored as
  all role=required found AND >=1 found in each role=alternative group
which is what a prose "must_surface" rule usually means. Without those fields it falls back to the
flat count eval.py uses, and says so.
"""
import argparse, collections, hashlib, json, re, sys
from pathlib import Path

ALWAYS_KEEP = {"independent_comparison", "review"}


# ── io (never raises on a missing or half-written file) ─────────────────────
def rj(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def rl(p):
    try:
        return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]
    except Exception:
        return []


def slug_for(question: str) -> str:
    """lake.py's rule, reimplemented so this tool imports nothing from the engine."""
    words = re.findall(r"[a-z0-9]+", question.lower())[:6]
    return "-".join(words) + "-" + hashlib.sha1(question.encode()).hexdigest()[:6]


def norm_title(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", (t or "")).lower()).strip()


def match_paper(p, rec):
    doi = (p.get("doi") or "").lower()
    if doi and (rec.get("doi") or "").lower() == doi:
        return True
    pmid = str(p.get("pmid") or "")
    if pmid and pmid in (str(rec.get("pmid") or ""), str(rec.get("id") or "")):
        return True
    pat = norm_title(p.get("title"))
    return bool(pat) and pat in norm_title(rec.get("title"))


# ── per-lake state ──────────────────────────────────────────────────────────
def read_lake(d: Path) -> dict:
    m = rj(d / "manifest.json", {}) or {}
    st = m.get("stages", {})
    plan = rj(d / "plan.json", {}) or {}
    sel = rj(d / "selected.json", {}) or {}
    syn = rj(d / "synthesis.json", {}) or {}
    screen = rl(d / "screen.jsonl")
    recs = rl(d / "records.jsonl")
    ev = rl(d / "evidence.jsonl")
    prof = {r["eid"]: r for r in rl(d / "evidence_profile.jsonl")}
    rej = rl(d / "evidence_rejected.jsonl")
    report = ""
    try:
        report = (d / "report.md").read_text()
    except Exception:
        pass
    docs = sel.get("docs", [])
    rel = [r for r in screen if r.get("relevant") and r.get("in_scope", True)]
    # unique relevant papers, and which of them selection admits without a quota
    types = {}
    for r in rel:
        types.setdefault(r["key"], set()).add(r.get("evidence_type"))
    unlimited = sum(1 for ts in types.values() if ts & ALWAYS_KEEP)
    sids = [s["id"] for s in plan.get("subquestions", [])]
    allp = [p for b in syn.get("subquestions", {}).values() for p in b.get("points", [])] + syn.get("summary", [])
    return {
        "dir": d, "slug": m.get("slug") or d.name, "question": m.get("question") or plan.get("question") or "?",
        "scope": plan.get("scope"), "protocol": m.get("protocol"), "model": m.get("model"),
        "created": (m.get("created") or "")[:16], "settings": m.get("settings", {}),
        "stages_done": [k for k in ("plan", "collect", "screen", "chain", "select", "fetch",
                                    "extract", "synthesize", "check", "report") if k in st],
        "st": st, "plan": plan, "sel": sel, "syn": syn, "sids": sids,
        "screen": screen, "recs": recs, "ev": ev, "rej": rej, "docs": docs, "report": report, "prof": prof,
        "metrics": rj(d / "metrics.json", {}) or st.get("report", {}).get("metrics", {}) or {},
        "n_records": len(recs) or st.get("collect", {}).get("unique_records") or 0,
        "n_rel_rows": len(rel), "n_rel_papers": len(types), "n_unlimited": unlimited,
        "points": allp,
    }


def funnel(L):
    st, m = L["st"], L["metrics"]
    rows = [
        ("queries", st.get("plan", {}).get("queries") or m.get("queries")),
        ("records (unique)", L["n_records"]),
        ("chain added", st.get("chain", {}).get("added")),
        ("screen rows", len(L["screen"]) or None),
        ("relevant rows", L["n_rel_rows"] or None),
        ("relevant papers (unique)", L["n_rel_papers"] or None),
        ("selected docs", len(L["docs"]) or m.get("docs_selected")),
        ("fulltext fetched", st.get("fetch", {}).get("fulltext") if st.get("fetch") else m.get("fulltext")),
        ("evidence kept", len(L["ev"]) or m.get("evidence_kept")),
        ("quotes rejected", len(L["rej"]) if L["rej"] else m.get("quotes_rejected")),
        ("points", len(L["points"]) or m.get("points")),
        ("references", m.get("references")),
    ]
    return [(k, v) for k, v in rows if v is not None]


def gold(L, cases):
    """[{label, role, group, found, selected, cited}] for the case whose slug matches this lake."""
    case = next((c for c in cases if slug_for(c["question"]) == L["slug"]), None)
    if not case:
        return None, None
    out = []
    for p in case.get("key_papers", []):
        found = any(match_paper(p, r) for r in L["recs"]) if L["recs"] else None
        hit = [d for d in L["docs"] if match_paper(p, d)]
        doi = (p.get("doi") or "").lower()
        cited = bool(doi and doi in (L["report"] or "").lower()) or any(
            (d.get("doi") or "").lower() and (d.get("doi") or "").lower() in (L["report"] or "").lower() for d in hit)
        out.append({"label": p.get("label") or p.get("title", "?"), "role": p.get("role"),
                    "group": p.get("group"), "found": found, "selected": bool(hit), "cited": bool(cited)})
    return case, out


def score_requirements(rows, key="cited"):
    """(satisfied, total) under role/group semantics; None when the pack has no roles.

    Scored on "cited" by default, not "selected": a paper selection kept but the report never
    quoted satisfies nothing, and scoring on "selected" reported 2/2 for the 27 Oct run where one
    slot was held by a paper extract had skipped."""
    if not any(r.get("role") for r in rows):
        return None
    req = [r for r in rows if r["role"] == "required"]
    groups = collections.defaultdict(list)
    for r in rows:
        if r["role"] == "alternative":
            groups[r.get("group") or "_"].append(r)
    total = len(req) + len(groups)
    ok = sum(1 for r in req if r[key]) + sum(1 for g in groups.values() if any(x[key] for x in g))
    return ok, total


def flags(L, grows):
    f = []
    m, sel, st = L["metrics"], L["sel"], L["st"]
    by_type = collections.Counter(d.get("evidence_type") for d in L["docs"])
    rule = sel.get("rule") or {}
    max_docs = rule.get("max_docs") or L["settings"].get("max_docs")
    # effective ceiling: with a per-sub-question quota on ALWAYS_KEEP, no-quota papers can take at
    # most quota x n_subquestions slots, so a large candidate pool is no longer a problem.
    quota = rule.get("always_keep_per_sub") or 0
    ceiling = min(L["n_unlimited"], quota * max(1, len(L["sids"]))) if quota else L["n_unlimited"]
    if ceiling and max_docs and ceiling > max_docs:
        f.append(f"SELECTION SATURATED: {L['n_unlimited']} relevant papers enter without a quota "
                 f"(evidence_type in {sorted(ALWAYS_KEEP)}) but max_docs is {max_docs}. Everything subject to "
                 f"k_per_sub is unreachable; selection is ranking reviews by citations.")
    elif L["n_rel_papers"] and max_docs and L["n_rel_papers"] <= max_docs:
        f.append(f"selection not exercised: {L['n_rel_papers']} relevant papers <= max_docs {max_docs} "
                 "(everything relevant was taken, so this case does not test the ranking)")
    if L["docs"] and not by_type.get("primary_study"):
        f.append(f"no primary study selected (type mix {dict(by_type)})")
    if m.get("independent_references") == 0 and m.get("references"):
        f.append(f"0 of {m['references']} references are independent comparisons")
    if m.get("quotes_rejected") == 0 and (m.get("evidence_kept") or 0) > 20:
        f.append(f"quote check never fired: 0 rejected of {m['evidence_kept']} kept "
                 "(cannot tell a clean extractor from an inert check)")
    nf = len(st.get("fetch", {}).get("failed", []) or [])
    noa = len(st.get("fetch", {}).get("not_open_access", []) or [])
    if nf:
        f.append(f"{nf} full-text fetches failed for papers that claim to be open access "
                 "(these are real failures, unlike the non-OA ones)")
    if noa and L["docs"]:
        f.append(f"{noa} of {len(L['docs'])} selected papers have no open-access full text and were read "
                 "as abstracts; not a failure, but the evidence from them is abstract-depth")
    if L["prof"]:
        n = len(L["prof"])
        unclear = sum(1 for r in L["prof"].values() if r.get("system") == "unclear")
        if unclear > n // 2:
            f.append(f"{unclear} of {n} evidence items have an unclear system; the profile is not "
                     "telling a reader which findings are human and which are preclinical")
    if grows:
        miss = [g["label"].split("(")[0].strip() for g in grows if not g["selected"]]
        if miss:
            f.append("key papers not selected: " + ", ".join(miss))
        dead = [g["label"].split("(")[0].strip() for g in grows if g["selected"] and not g["cited"]]
        if dead:
            f.append("key papers selected but never cited in the report: " + ", ".join(dead))
        # found=False with selected=True cannot happen in a consistent lake: select reads records.jsonl
        broken = [g["label"].split("(")[0].strip() for g in grows if g["found"] is False and g["selected"]]
        if broken:
            f.append("INCONSISTENT LAKE: " + ", ".join(broken) + " is in selected.json but absent from "
                     "records.jsonl, so extract had no text for it. A collect run rebuilt records.jsonl "
                     "without the chain-added records; re-run chain, then select.")
    return f


# ── printing ────────────────────────────────────────────────────────────────
def show(L, cases, n_drops):
    W = 78
    print("=" * W)
    print(f"{L['slug']}")
    print(f"  {L['question'][:W - 2]}")
    if L["scope"]:
        print(f"  scope: {str(L['scope'])[:W - 9]}")
    print(f"  protocol {L['protocol']}  model {L['model']}  built {L['created']}")
    print(f"  stages: {' > '.join(L['stages_done']) or '(none recorded)'}")

    print("\n  pipeline")
    for k, v in funnel(L):
        print(f"    {k:26} {v}")

    sel_rule = L["sel"].get("rule") or {}
    if sel_rule or L["docs"]:
        print("\n  selection")
        print(f"    score        {sel_rule.get('score', '?')}")
        knobs = {k: sel_rule.get(k) for k in ("k_per_sub", "max_docs", "always_keep_per_sub", "min_primary",
                                              "breadth_w", "grade_weight", "out_of_scope_per_sub") if sel_rule.get(k) is not None}
        if knobs:
            print(f"    knobs        {knobs}")
        print(f"    admitted w/o quota  {L['n_unlimited']} relevant papers "
              f"(type in {sorted(ALWAYS_KEEP)}) vs max_docs {sel_rule.get('max_docs', '?')}")
        print(f"    selected by type    {dict(collections.Counter(d.get('evidence_type') for d in L['docs']))}")

    if L["sids"]:
        print("\n  per sub-question")
        per = L["st"].get("screen", {}).get("per_subquestion", {})
        print(f"    {'sid':4} {'screened':>8} {'relevant':>8} {'selected':>8} {'evidence':>8} {'points':>6}")
        for s in L["plan"].get("subquestions", []):
            sid = s["id"]
            p = per.get(sid, {})
            nsel = sum(1 for d in L["docs"] if sid in (d.get("sids") or []))
            nev = sum(1 for e in L["ev"] if e.get("sid") == sid)
            npt = len(L["syn"].get("subquestions", {}).get(sid, {}).get("points", []))
            print(f"    {sid:4} {p.get('screened', '-'):>8} {p.get('relevant', '-'):>8} "
                  f"{nsel:>8} {nev or '-':>8} {npt or '-':>6}")
            print(f"         {s['text'][:W - 10]}")
            for q in s.get("queries", []):
                tag = "TAGGED" if re.search(r"\b[A-Z_]{3,}:", q.get("query", "")) else q.get("kind", "?")
                print(f"           [{tag:7}] {q.get('query', '')[:W - 22]}")

    if L["prof"]:
        import collections as _c
        cited = {x for x in L["prof"] if x in (L["report"] or "")}
        sysc = _c.Counter(r.get("system") for r in L["prof"].values())
        print(f"\n  evidence profile ({len(L['prof'])} items, {len(cited)} of them cited)")
        for k, v in sysc.most_common():
            print(f"    {k or '?':24} {v}")
        print(f"    reset by word check      {sum(1 for r in L['prof'].values() if r.get('notes'))}")

    drops = (L["sel"].get("rule") or {}).get("dropped") or []
    if drops:
        print(f"\n  dropped at select ({len(drops)} rows; worst {min(n_drops, len(drops))} by citations)")
        seen, shown = set(), 0
        for r in sorted(drops, key=lambda r: -(r.get("cited_by") or 0)):
            if r["key"] in seen or shown >= n_drops:
                continue
            seen.add(r["key"]); shown += 1
            print(f"    {r.get('cited_by', '?'):>6} cites  {r.get('evidence_type', '?'):20} {r.get('why', '')[:34]}  {r['key']}")

    case, grows = gold(L, cases) if cases else (None, None)
    if grows:
        req = score_requirements(grows, "cited")
        req_sel = score_requirements(grows, "selected")
        print(f"\n  key papers  (case {case['id']})")
        for g in grows:
            fnd = {True: "Y", False: "n", None: "?"}[g["found"]]
            role = f" [{g['role']}{'/' + g['group'] if g.get('group') else ''}]" if g.get("role") else ""
            print(f"    found={fnd} selected={'Y' if g['selected'] else 'n'} cited={'Y' if g['cited'] else 'n'}"
                  f"{role}  {g['label'][:W - 40]}")
        if req:
            line = f"    requirements satisfied: {req[0]}/{req[1]} cited (role/group aware)"
            if req_sel and req_sel[0] != req[0]:
                line += f"; {req_sel[0]}/{req_sel[1]} merely selected — a paper the report never cites counts for nothing"
            print(line)
        else:
            print(f"    flat count: {sum(1 for g in grows if g['selected'])}/{len(grows)} selected "
                  "— pack has no role/group fields, so an OR-pair scores the same as two requirements")
        if case.get("scoring"):
            print("    NOTE: this case carries a prose `scoring` rule that nothing checks automatically:")
            for k, v in case["scoring"].items():
                print(f"      {k}: {str(v)[:200]}")

    fl = flags(L, grows)
    if fl:
        print("\n  flags")
        for x in fl:
            print(f"    ! {x}")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("--cases", type=Path, action="append", default=[],
                    help="a cases .json; repeatable. Default: every packs/deep-research/*.json under the repo")
    ap.add_argument("--drops", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--explain-roles", action="store_true", help="print the key_papers role/group schema and exit")
    a = ap.parse_args(argv)

    if a.explain_roles:
        print(__doc__.split("If a case's")[1].split("\n\n")[0])
        print('\nAdd to a key_paper:  "role": "required"')
        print('                     "role": "alternative", "group": "<slot name>"')
        print('Papers with no role are treated as required only by the flat count, never by requirements.')
        return 0

    t = a.target.expanduser().resolve()
    paths = list(a.cases)
    if not paths:
        for up in [t, *t.parents][:6]:
            paths += sorted((up / "packs" / "deep-research").glob("*.json"))
            if paths:
                break
    cases = []
    for p in paths:
        d = rj(p, {}) or {}
        cases += d.get("cases", d if isinstance(d, list) else [])

    lakes = [t] if (t / "manifest.json").exists() else sorted(c for c in t.iterdir() if (c / "manifest.json").exists())
    if not lakes:
        print(f"no lake found at {t} (looked for manifest.json here and one level down)", file=sys.stderr)
        return 1

    if a.json:
        out = []
        for d in lakes:
            L = read_lake(d)
            case, grows = gold(L, cases) if cases else (None, None)
            out.append({"slug": L["slug"], "question": L["question"], "stages": L["stages_done"],
                        "funnel": dict(funnel(L)), "selected_by_type": dict(collections.Counter(
                            x.get("evidence_type") for x in L["docs"])),
                        "admitted_without_quota": L["n_unlimited"], "relevant_papers": L["n_rel_papers"],
                        "key_papers": grows, "case": (case or {}).get("id"), "flags": flags(L, grows)})
        print(json.dumps(out, indent=1))
        return 0

    if len(lakes) > 1:
        print(f"{t}\n{len(lakes)} lakes; cases loaded: {len(cases)}\n")
    for d in lakes:
        show(read_lake(d), cases, a.drops)
    return 0


if __name__ == "__main__":
    sys.exit(main())
