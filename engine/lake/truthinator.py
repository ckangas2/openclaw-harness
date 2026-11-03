#!/usr/bin/env python3
"""The Truthinator: the best depiction of truth we can establish from the literature record.

It does not decide what is true. For every point in a lake's report it measures how independently the
claim has been found, from the cited papers' metadata alone (no model calls):

  ladder   relayed only  - every cited item is a review or a secondhand citation
           1 paper       - one primary paper
           1 group       - several papers, all from one group
           2 groups      - two independent groups
           3+ groups     - meets the standard (--groups, default 3)
  groups   papers are the same group if they share the last author, or share >= 2 authors.
           Grouping is transitive ONLY among the papers the point itself cites (a whole-literature chain
           would merge collaborating fields into one "group").
  counted  reviews relay others' work and do not count; evidence marked as citing another study
           (source=cited_work) is that other study's finding and does not count.
  flags    retracted paper, preprint, published together (same journal and date: shown, never counted
           as dependence - back-to-back papers are the classic signature of independent discovery).

v2 adds, per evidence item, the system it was observed in and the method (one narrow model call per batch of
8 items, prompts/evidence_profile.md; the model must copy the words that name system and method, and code checks
they occur in the item's claim or quote, otherwise the item counts as "unclear"). Then a GRADE-style grade per
point from grade_rules.toml: start level from the strongest method, rated down (one group, indirect, disagreement,
unverified, retracted, ...) and up (two methods agree, human genetics), every fired rule listed with the grade.

Not visible to it (said in the output): shared patients, cell lines, reagents or unpublished data.

    python3 engine/lake/truthinator.py <lake> [--groups 3] [--stability <variants-dir>] [--no-model]
    -> data/experiments/truthinator/<lake>-<stamp>/  report.html (badges), truth.json, verify.md (check by hand)
"""
import argparse, json, shutil, sys, time, tomllib, unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import EXPERIMENTS  # noqa: E402

COPY = ["plan.json", "evidence.jsonl", "selected.json", "premises.json", "manifest.json",
        "synthesis.json", "synthesis_raw.json", "stability.json"]
LADDER = {0: "relayed only", 1: "1 paper", 2: "1 group", 3: "2 groups", 4: "3+ groups"}
RULES = HERE / "grade_rules.toml"
SYSTEMS = ["human_patients", "human_primary_cells", "human_cell_line", "animal_in_vivo", "animal_cells",
           "in_vitro_biochemical", "computational", "unclear"]
METHODS = ["human_genetic_variant", "genetic_knockout_or_knockdown", "blocking_antibody_or_receptor_blockade",
           "drug_or_inhibitor", "overexpression_or_recombinant", "intervention_trial", "observational_correlation",
           "computational_prediction", "descriptive_other"]
INTERVENTION = set(METHODS[:6])
OBSERVATIONAL = {"observational_correlation", "descriptive_other"}
PROFILE_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["eid", "system", "system_words", "method", "method_words", "directness",
                                   "subject_words", "setting_words", "scope_match"],
    "properties": {"eid": {"type": "string"}, "system": {"type": "string", "enum": SYSTEMS},
                   "system_words": {"type": "string"}, "method": {"type": "string", "enum": METHODS},
                   "method_words": {"type": "string"},
                   "directness": {"type": "string", "enum": ["direct", "adjacent", "unclear"]},
                   # what the claim is about, and under what conditions -- the scope is applied to
                   # documents at screening and never to claims, so a passage about another subject
                   # inside an in-scope review reaches the report unmarked (S4, 27 Oct).
                   "subject_words": {"type": "string"}, "setting_words": {"type": "string"},
                   "scope_match": {"type": "string", "enum": ["direct", "adjacent", "unclear"]}}}}}}


# ── evidence profile (model, words checked in code) ─────────────────────────
def _found(words, text):
    w = " ".join((words or "").split()).lower()
    return bool(w) and w in " ".join((text or "").split()).lower()


def check_profile(item: dict, e: dict) -> dict:
    """Keep the model's system/method only if the words it cites are really in the claim or quote."""
    text = f"{e.get('claim', '')} {e.get('quote', '')}"
    out = {"eid": e["eid"], "system": item.get("system", "unclear"), "method": item.get("method", "descriptive_other"),
           "directness": item.get("directness", "unclear"), "system_words": item.get("system_words", ""),
           "method_words": item.get("method_words", ""),
           "scope_match": item.get("scope_match", "unclear"), "subject_words": item.get("subject_words", ""),
           "setting_words": item.get("setting_words", ""), "notes": []}
    if out["system"] != "unclear" and not _found(out["system_words"], text):
        out["notes"].append(f"system '{out['system']}' not backed by the quote (words: {out['system_words']!r})")
        out["system"], out["directness"] = "unclear", "unclear"
    if out["method"] != "descriptive_other" and not _found(out["method_words"], text):
        out["notes"].append(f"method '{out['method']}' not backed by the quote (words: {out['method_words']!r})")
        out["method"] = "descriptive_other"
    # scope_match stands only if the words naming the subject are really there; a judgement about
    # what a claim is about cannot rest on words the model supplied itself.
    if out["scope_match"] != "unclear":
        if not _found(out["subject_words"], text):
            out["notes"].append(f"scope_match '{out['scope_match']}' not backed by the quote "
                                f"(subject words: {out['subject_words']!r})")
            out["scope_match"] = "unclear"
        elif out["setting_words"] and not _found(out["setting_words"], text):
            out["notes"].append(f"setting words not in the quote ({out['setting_words']!r})")
            out["setting_words"] = ""
    return out


def profile_evidence(out_dir: Path, evs: list, plan: dict, batch=8) -> dict:
    import llm
    res = {}
    scope = plan.get("scope") or "(not stated)"
    for i in range(0, len(evs), batch):
        chunk = evs[i:i + batch]
        # Without the paper, a claim is judged on its own wording: Ikeda 1999 says "oncolytic
        # viruses/vectors" and "intravascular", so in isolation it reads adjacent to a scope naming
        # HSV-1 and systemic delivery, though the paper is squarely that (27 Oct).
        cards = "\n".join(f"[{e['eid']}] from: {e.get('title', '?')} ({e.get('evidence_type', '?')})\n"
                          f"    claim: {e.get('claim', '')}\n    quote: \"{e.get('quote', '')[:600]}\"" for e in chunk)
        try:
            got, _ = llm.call(out_dir, "evidence_profile", "evidence_profile.md",
                              {"question": plan["question"], "scope": scope, "evidence": cards}, PROFILE_SCHEMA)
        except ValueError:
            got = {"items": []}
        by = {it.get("eid"): it for it in got.get("items", [])}
        for e in chunk:
            res[e["eid"]] = check_profile(by.get(e["eid"], {}), e)
        print(f"evidence profile: {min(i + batch, len(evs))}/{len(evs)}", file=sys.stderr)
    return res


# ── grade (code, rules from grade_rules.toml) ───────────────────────────────
def load_rules(path=RULES):
    with open(path, "rb") as f:
        return tomllib.load(f)


def grade(pt: dict, prof: dict, eprof: dict, ev: dict, rules: dict, stab=None) -> dict:
    levels = rules["levels"]
    counted_keys = {p["key"] for p in prof["primary"]}
    items = [eprof[x] for x in pt.get("eids", []) if x in eprof and x in ev and ev[x]["key"] in counted_keys
             and ev[x].get("source") != "cited_work"]
    item_keys = [ev[x]["key"] for x in pt.get("eids", []) if x in eprof and x in ev and ev[x]["key"] in counted_keys
                 and ev[x].get("source") != "cited_work"]
    methods = {i["method"] for i in items}
    systems = {i["system"] for i in items if i["system"] != "unclear"}
    if not prof["primary"]:
        start_kind = "none"
    elif methods & INTERVENTION:
        start_kind = "intervention"
    elif methods & OBSERVATIONAL:
        start_kind = "observational"
    elif methods & {"computational_prediction"}:
        start_kind = "computational"
    else:
        start_kind = "observational"
    lvl = levels.index(rules["start"][start_kind])
    steps = [f"start {rules['start'][start_kind]} ({start_kind.replace('_', ' ')})"]
    D, U = rules["down"], rules["up"]

    def down(key, why):
        nonlocal lvl
        if D.get(key, 0):
            lvl -= D[key]; steps.append(f"-{D[key]} {why}")
    if prof["primary"]:
        if prof["groups"] <= 1:
            down("one_group", "one group" if prof["papers"] > 1 else "one paper")
        elif prof["groups"] < rules["groups_needed"]:
            down("two_groups", f"{prof['groups']} groups (standard {rules['groups_needed']})")
        if items and not any(i["directness"] == "direct" for i in items):
            down("indirect_only", "no evidence in the question's own system")
    if pt.get("kind") == "disagreement":
        down("disagreement", "the point reports disagreeing evidence")
    if pt.get("issues"):
        down("failed_check", "point still unverified after checks")
    if any("RETRACTED" in f for f in prof["flags"]):
        down("retracted", "cites a retracted paper")
    if prof["primary"] and all(p.get("is_preprint") for p in prof["primary"]):
        down("preprint_only", "preprints only")
    if stab and stab.get("of") and stab["in"] * 2 <= stab["of"]:
        down("unstable", f"in {stab['in']}/{stab['of']} seed samples")
    lvl = max(lvl, 0)
    # Two methods only count as agreeing when they come from two independent groups:
    # several assays inside one lab are one source, and must not cancel the one-paper penalty.
    group_of = {}
    for gi, g in enumerate(prof.get("group_list") or []):
        for k in g["keys"]:
            group_of[k] = gi
    inter_groups = {group_of.get(k) for i, k in zip(items, item_keys) if i["method"] in INTERVENTION}
    inter_groups.discard(None)
    inter = methods & INTERVENTION
    if len(inter) >= 2 and len(inter_groups) >= 2:
        lvl += U["two_methods"]
        steps.append(f"+{U['two_methods']} {len(inter)} methods agree across {len(inter_groups)} groups "
                     f"({', '.join(sorted(m.replace('_', ' ') for m in inter))})")
    if "human_genetic_variant" in methods:
        lvl += U["human_genetics"]; steps.append(f"+{U['human_genetics']} human genetic evidence")
    top = len(levels) - 1
    if rules.get("high_requires_groups") and prof["groups"] < rules["groups_needed"]:
        top = len(levels) - 2
        if lvl > top:
            steps.append(f"capped at {levels[top]}: High needs {rules['groups_needed']} groups")
    if pt.get("issues") and rules.get("unverified_max") in levels:
        cap = levels.index(rules["unverified_max"])
        if min(max(lvl, 0), top) > cap:
            steps.append(f"capped at {levels[cap]}: the point failed the report's checks")
        top = min(top, cap)
    lvl = min(max(lvl, 0), top)
    return {"grade": levels[lvl], "steps": steps, "issues": list(pt.get("issues") or []), "methods": sorted(methods), "systems": sorted(systems),
            "direct": sum(1 for i in items if i["directness"] == "direct"), "items": len(items)}


def cap_summaries(syn, truth, rules):
    """A summary point cannot be graded above the best section point it draws on.

    Summary points cite evidence ids, not point ids, so a section point counts as a
    constituent when it shares at least one evidence id with the summary. Without this,
    a summary that strings several single-lab clauses together collects all their papers
    and is scored as if they converged on one claim.
    """
    levels = rules["levels"]
    section = [q for b in syn.get("subquestions", {}).values() for q in b.get("points", [])]
    for sp in syn.get("summary", []):
        t = truth.get(sp.get("id"))
        if not t or not t.get("grade"):
            continue
        eids = set(sp.get("eids", []))
        parts = [q for q in section
                 if q.get("id") in truth and truth[q["id"]].get("grade")
                 and eids & set(q.get("eids", []))]
        if not parts:
            continue
        best_pt = max(parts, key=lambda q: levels.index(truth[q["id"]]["grade"]["grade"]))
        best = levels.index(truth[best_pt["id"]]["grade"]["grade"])
        if levels.index(t["grade"]["grade"]) > best:
            t["grade"]["grade"] = levels[best]
            t["grade"]["steps"].append(
                f"capped at {levels[best]}: no better supported than {best_pt['id']}, "
                f"its strongest supporting point")


def norm(name: str) -> str:
    """'Korthäuer U' -> 'korthauer u'; 'de Saint Basile G' -> 'de saint basile g'. Surname + first initial."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower().strip()
    parts = s.replace(".", " ").replace(",", " ").split()
    if not parts:
        return ""
    if len(parts) > 1 and parts[-1].isalpha() and len(parts[-1]) <= 3:
        return " ".join(parts[:-1]) + " " + parts[-1][0]
    return " ".join(parts)


def same_group(a: list, b: list) -> tuple:
    """-> (bool, reason). a, b: author lists in order."""
    na, nb = [norm(x) for x in a if norm(x)], [norm(x) for x in b if norm(x)]
    if not na or not nb:
        return False, ""
    if na[-1] == nb[-1]:
        return True, f"same last author ({a[-1]})"
    shared = sorted(set(na) & set(nb))
    if len(shared) >= 2:
        return True, f"share {len(shared)} authors ({', '.join(shared[:4])})"
    return False, ""


def group_index(papers: list) -> tuple:
    """papers: [{key, authors}] -> ({key: root}, {(a, b): reason}). Union-find over the whole set.

    Built once over every paper in the review, not per point: whether two papers are one
    lab is a fact about the papers, and must not change with what else a point happens to
    cite. A third paper sharing authors with both still bridges them, wherever it sits.
    """
    parent = {p["key"]: p["key"] for p in papers}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]; k = parent[k]
        return k
    why = {}
    for i, a in enumerate(papers):
        for b in papers[i + 1:]:
            ok, reason = same_group(a.get("authors") or [], b.get("authors") or [])
            if ok:
                ra, rb = find(a["key"]), find(b["key"])
                if ra != rb:
                    parent[rb] = ra
                why.setdefault((a["key"], b["key"]), reason)
    return {p["key"]: find(p["key"]) for p in papers}, why


def groups_of(papers: list, index: tuple = None) -> list:
    """papers: [{key, authors, ...}] -> list of groups [{keys:[...], why:[...]}]."""
    root_of, why = index if index else group_index(papers)
    out = {}
    for p in papers:
        out.setdefault(root_of.get(p["key"], p["key"]), []).append(p["key"])
    res = []
    for root, keys in out.items():
        ks = set(keys)
        reasons = [f"{x} ~ {y}: {r}" for (x, y), r in why.items() if x in ks and y in ks]
        if len(keys) > 1 and not reasons:
            reasons = ["through a shared author on another paper in this review"]
        res.append({"keys": keys, "why": reasons})
    return res


def profile(pt: dict, ev: dict, meta: dict, need: int, index: tuple = None) -> dict:
    cited = [ev[x] for x in pt.get("eids", []) if x in ev]
    by_key = {}
    for e in cited:
        by_key.setdefault(e["key"], []).append(e)
    primary, relayed = [], []
    for k, es in by_key.items():
        m = meta.get(k, {})
        et = m.get("evidence_type") or es[0].get("evidence_type")
        if et == "review":
            relayed.append({"key": k, "why": "review"}); continue
        if all(e.get("source") == "cited_work" for e in es):
            relayed.append({"key": k, "why": "cites another study for this"}); continue
        primary.append({"key": k, "authors": m.get("authors") or es[0].get("authors") or [], "is_preprint": m.get("is_preprint"),
                        "year": m.get("pub_year") or es[0].get("year"), "journal": m.get("journal"),
                        "date": m.get("first_pub_date"), "evidence_type": et})
    grps = groups_of(primary, index)
    n = len(grps)
    level = 0 if not primary else (1 if len(primary) == 1 else (2 if n == 1 else (3 if n == 2 else 4)))
    flags = []
    if any(meta.get(k, {}).get("is_retracted") for k in by_key):
        flags.append("cites a RETRACTED paper")
    if any(meta.get(k, {}).get("is_preprint") for k in by_key):
        flags.append("cites a preprint")
    together = {}
    for p in primary:
        if p.get("journal") and p.get("date"):
            together.setdefault((p["journal"], p["date"]), []).append(p["key"])
    for (j, d), ks in together.items():
        if len(ks) > 1:
            flags.append(f"published together: {len(ks)} papers in {j} on {d} (shown, not counted as dependence)")
    return {"level": level, "label": LADDER[level], "groups": n, "papers": len(primary), "meets": n >= need,
            "need": need, "group_list": grps, "primary": primary, "relayed": relayed, "flags": flags}


def load_meta(lake: Path, keys: set) -> dict:
    meta = {}
    sel = json.loads((lake / "selected.json").read_text()) if (lake / "selected.json").exists() else {"docs": []}
    for d in sel.get("docs", []):
        meta[d["key"]] = dict(d)
    rp = lake / "records.jsonl"
    if rp.exists():  # full author lists (selected.json keeps only 3)
        with open(rp) as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("key") in keys:
                    m = meta.setdefault(r["key"], {})
                    for fld in ("authors", "journal", "first_pub_date", "is_retracted", "is_preprint", "pub_year", "title"):
                        if r.get(fld) is not None and (fld == "authors" or m.get(fld) is None):
                            m[fld] = r[fld]
    return meta


def surname(a):
    return a.split()[0] if a else "?"


def verify_sheet(lake_name, plan, syn, truth, meta, need, eprof=None, ev=None):
    L = [f"# Truthinator check sheet: {lake_name}", "",
         "For each point: are the groups right? Tick **ok**, or note **merge** (two listed groups are really one lab / "
         "lineage) or **split** (one listed group is really two). This sheet is the answer key for the grouping rule.", "",
         f"Rule: same group = same last author, or >= 2 shared authors; applied across every paper in the review. Standard: {need} groups.", ""]
    L += ["Also tick each evidence line if its system / method / directness is right.", ""]
    for sid, pts in [("Summary", syn.get("summary", []))] + [(s["id"], syn["subquestions"].get(s["id"], {}).get("points", []))
                                                             for s in plan["subquestions"]]:
        for pt in pts:
            if pt.get("dropped") or pt.get("id") not in truth:
                continue
            t = truth[pt["id"]]
            g = t.get("grade", {}).get("grade")
            head = f"## {pt['id']} — {(g + ' · ') if g else ''}{t['label']} ({t['groups']} groups, {t['papers']} papers)"
            L += [head, "", f"> {pt['text']}", ""]
            if t.get("grade"):
                L += ["  grade: " + " · ".join(t["grade"]["steps"]), ""]
            if eprof and ev:
                for x in pt.get("eids", []):
                    if x in eprof:
                        q = eprof[x]
                        L.append(f"- [ ] {x} ({ev[x]['key']}): system **{q['system']}** ({q['system_words'] or '-'}), "
                                 f"method **{q['method']}** ({q['method_words'] or '-'}), {q['directness']}"
                                 + (f" — {'; '.join(q['notes'])}" if q["notes"] else ""))
                L.append("")
            for i, g in enumerate(t["group_list"], 1):
                L.append(f"- [ ] group {i}:")
                for k in g["keys"]:
                    m = meta.get(k, {})
                    au = m.get("authors") or []
                    L.append(f"    - {surname(au[0]) if au else '?'} {m.get('pub_year', '')} — last author "
                             f"{au[-1] if au else '?'} — `{k}` — {(m.get('title') or '')[:90]}")
                for w in g["why"]:
                    L.append(f"    - grouped because {w}")
            for r in t["relayed"]:
                L.append(f"- not counted: `{r['key']}` ({r['why']})")
            for fl in t["flags"]:
                L.append(f"- note: {fl}")
            L += ["", "  verdict: ok / merge / split — notes:", ""]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lake", type=Path)
    ap.add_argument("--groups", type=int, default=3, help="independent groups needed to meet the standard (default 3)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--stability", type=Path, help="a synth_variants seeds folder: use its stability marks in the grade")
    ap.add_argument("--no-model", action="store_true", help="groups only (v1): no evidence profile, no grade")
    ap.add_argument("--rules", type=Path, default=RULES)
    ap.add_argument("--profile-from", type=Path, help="reuse evidence_profile.jsonl from an earlier Truthinator run (no model calls): "
                                                     "for re-grading after editing grade_rules.toml")
    a = ap.parse_args(argv)
    lake = a.lake.expanduser().resolve()
    if not (lake / "synthesis.json").exists():
        raise SystemExit(f"{lake}: no synthesis.json (run the lake through check first)")
    out = a.out or EXPERIMENTS / "truthinator" / f"{lake.name}-{time.strftime('%Y%m%d-%H%M')}"
    out.mkdir(parents=True, exist_ok=False)
    for f in COPY:
        if (lake / f).exists():
            shutil.copy(lake / f, out / f)
    plan = json.loads((out / "plan.json").read_text())
    syn = json.loads((out / "synthesis.json").read_text())
    ev = {}
    with open(out / "evidence.jsonl") as f:
        for line in f:
            if line.strip():
                e = json.loads(line); ev[e["eid"]] = e
    meta = load_meta(lake, {e["key"] for e in ev.values()})

    pts = [p for b in syn.get("subquestions", {}).values() for p in b.get("points", [])] + syn.get("summary", [])
    gidx = group_index([{"key": k, "authors": (meta.get(k) or {}).get("authors") or []}
                        for k in sorted({e["key"] for e in ev.values()})])
    truth = {p["id"]: profile(p, ev, meta, a.groups, gidx) for p in pts if not p.get("dropped") and p.get("id")}
    eprof = None
    if not a.no_model:
        rules = load_rules(a.rules)
        rules["groups_needed"] = a.groups
        stab = {}
        if a.stability:
            sp = a.stability.expanduser() / "reference" / "stability.json"
            stab = json.loads(sp.read_text()) if sp.exists() else {}
            if stab:
                shutil.copy(sp, out / "stability.json")
        _pos = {k: i for i, k in enumerate(ev)}        # eids are content hashes, not counters
        cited = sorted({x for p in pts if not p.get("dropped") for x in p.get("eids", []) if x in ev}, key=lambda x: _pos.get(x, 1 << 30))
        old = (a.profile_from.expanduser() / "evidence_profile.jsonl") if a.profile_from else None
        if old and old.exists():
            with open(old) as f:
                eprof = {q["eid"]: q for q in (json.loads(l) for l in f if l.strip())}
            missing = [x for x in cited if x not in eprof]
            if missing:
                eprof.update(profile_evidence(out, [ev[x] for x in missing], plan))
            print(f"evidence profile reused from {old.parent.name} ({len(cited) - len(missing)} items)", file=sys.stderr)
        else:
            eprof = profile_evidence(out, [ev[x] for x in cited], plan)
        with open(out / "evidence_profile.jsonl", "w") as f:
            f.writelines(json.dumps(eprof[x]) + "\n" for x in cited)
        shutil.copy(a.rules, out / "grade_rules.toml")
        for p in pts:
            if p.get("id") in truth:
                truth[p["id"]]["grade"] = grade(p, truth[p["id"]], eprof, ev, rules, stab.get(p["id"]))
        cap_summaries(syn, truth, rules)
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    (out / "verify.md").write_text(verify_sheet(lake.name, plan, syn, truth, meta, a.groups, eprof, ev))
    import render
    render.render_lake(out)
    if eprof is not None:
        import check_view
        check_view.main([str(out)])

    dist = {}
    for t in truth.values():
        dist[t["label"]] = dist.get(t["label"], 0) + 1
    print(out)
    print(f"\n{len(truth)} points: " + ", ".join(f"{LADDER[i]} {dist.get(LADDER[i], 0)}" for i in range(5)))
    print(f"meet the {a.groups}-group standard: {sum(1 for t in truth.values() if t['meets'])}")
    if eprof is not None:
        gd = {}
        for t in truth.values():
            g = t.get("grade", {}).get("grade")
            gd[g] = gd.get(g, 0) + 1
        print("grades: " + ", ".join(f"{g} {gd.get(g, 0)}" for g in reversed(load_rules(a.rules)["levels"])))
        unb = sum(1 for q in eprof.values() if q["notes"])
        print(f"evidence profiled: {len(eprof)} items, {unb} with a system/method the quote did not back (set to unclear)")
    print(f"\nstart here: {out / 'check.html'}\nfull report: {out / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
