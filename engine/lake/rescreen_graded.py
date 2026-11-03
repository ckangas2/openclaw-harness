#!/usr/bin/env python3
"""Diagnostic: screen a finished lake's records with a GRADED screening prompt and compare with the
original binary screening. Nothing in the lake or the repo is written.

The graded prompt sees all sub-questions at once, so each record is graded ONCE (not once per
sub-question that retrieved it). Same record cards, batch size, record order, question, scope,
model and seed as the original run; batches are split by query kind (topic / counter) because the
prompt is told which kind of search produced the batch.

Prompt inputs: {{question}} {{subquestions}} {{scope}} {{query_kind}} {{records}}
Reply per record: n, evidence_type, bearing[], in_scope, relevance 0-3, routes_to[], use_as, reason

    python3 engine/lake/rescreen_graded.py <lake-or-case-dir> --prompt ~/screen_graded.md --dry-run
    python3 engine/lake/rescreen_graded.py <lake-or-case-dir> --prompt ~/screen_graded.md

Output (default <repo>/data/experiments/rescreen/<case>-graded-<stamp>/): graded.jsonl, calls.jsonl, summary.md.
Do not run during an agent eval: same GPU and model.
"""
import argparse, json, shutil, sys, time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import EXPERIMENTS  # noqa: E402  <repo>/data/experiments, or $HARNESS_DATA/experiments

TYPES = ["independent_comparison", "new_method", "review", "primary_study", "other"]
BEARING = ["mechanism", "outcome", "implementation", "negative_or_limitation", "background"]
USE_AS = ["evidence", "citation_source", "both"]
NAME = "screen_graded.md"


def schema(sids):
    item = {"type": "object", "properties": {
        "n": {"type": "integer"},
        "evidence_type": {"type": "string", "enum": TYPES},
        "bearing": {"type": "array", "items": {"type": "string", "enum": BEARING}},
        "in_scope": {"type": "boolean"},
        "relevance": {"type": "integer", "enum": [0, 1, 2, 3]},
        "routes_to": {"type": "array", "items": {"type": "string", "enum": sids}},
        "use_as": {"type": "string", "enum": USE_AS},
        "reason": {"type": "string"}},
        "required": ["n", "evidence_type", "bearing", "in_scope", "relevance", "routes_to", "use_as", "reason"]}
    return {"type": "object", "properties": {"items": {"type": "array", "items": item}}, "required": ["items"]}


def valid(it, sids):
    """Problems with one reply item, as a list of short strings (empty = holds the shape)."""
    p = []
    if it.get("evidence_type") not in TYPES: p.append("evidence_type")
    b = it.get("bearing")
    if not isinstance(b, list) or not b or any(x not in BEARING for x in b): p.append("bearing")
    if not isinstance(it.get("in_scope"), bool): p.append("in_scope")
    if it.get("relevance") not in (0, 1, 2, 3): p.append("relevance")
    r = it.get("routes_to")
    if not isinstance(r, list) or any(x not in sids for x in r): p.append("routes_to")
    if it.get("use_as") not in USE_AS: p.append("use_as")
    if not str(it.get("reason", "")).strip(): p.append("reason")
    return p


def query_kinds(plan):
    """qid -> 'counter' or 'topic', from plan.json query kinds."""
    out = {}
    for s in plan.get("subquestions", []):
        for q in s.get("queries", []):
            out[q["qid"]] = "counter" if "counter" in str(q.get("kind", "")).lower() else "topic"
    return out


def record_kind(rec, qk):
    """'counter' if ANY counter query found it (the counter rule only stops negative results being
    downgraded, so it is safe for records a topic query also found)."""
    return "counter" if any(qk.get(h.get("qid")) == "counter" for h in rec.get("hits", [])) else "topic"


def grade_batch(llm, lakemod, out_dir, batch, inputs, sids, kind):
    cards = "\n\n".join(lakemod.record_card(i, r) for i, r in enumerate(batch, 1))
    try:
        out, _ = llm.call(out_dir, f"screen_graded:{kind}", NAME,
                          dict(inputs, query_kind=kind, records=cards), schema(sids), prob_fields=("relevance",))
    except ValueError:
        return [], 0
    rows, seen, bad = [], set(), 0
    for it in out.get("items", []):
        n = it.get("n")
        if not isinstance(n, int) or not 1 <= n <= len(batch) or n in seen:
            continue
        seen.add(n)
        probs = valid(it, sids)
        bad += bool(probs)
        rows.append(dict(it, key=batch[n - 1]["key"], query_kind=kind, problems=probs))
    return rows, bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lake", type=Path)
    ap.add_argument("--prompt", type=Path, required=True, help="the graded screening prompt (.md)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="grade every stored record with an abstract (= LAKE_SCREEN_TOP=100), not only those screened originally")
    a = ap.parse_args(argv)

    import lake as lakemod
    import llm
    import rescreen  # original_inputs()

    lake = a.lake.expanduser().resolve()
    plan = json.loads((lake / "plan.json").read_text())
    sids = [s["id"] for s in plan["subquestions"]]
    recs = {}
    for l in open(lake / "records.jsonl"):
        r = json.loads(l); recs[r["key"]] = r
    old = [json.loads(l) for l in open(lake / "screen.jsonl") if l.strip()]
    llm_old = [r for r in old if r.get("by") == "llm"]
    keys = list(dict.fromkeys(r["key"] for r in llm_old if r["key"] in recs))  # unique, original order
    screened_before = set(keys)
    if a.all:  # same rules stage_screen applies in code: no retracted, abstract >= 100 chars
        keys += [k for k, r in recs.items() if k not in screened_before
                 and not r.get("is_retracted") and len(r.get("abstract") or "") >= 100]
    by_key = {}
    for r in llm_old:
        by_key.setdefault(r["key"], []).append(r)

    old_rel = {k: (any(r.get("relevant") for r in by_key[k]) if k in screened_before else None) for k in keys}
    old_sids = {k: sorted({r["sid"] for r in by_key.get(k, []) if r.get("relevant")}) for k in keys}
    retrieved_by = {k: sorted({h["sid"] for h in recs[k].get("hits", []) if h.get("sid")}
                              | {r["sid"] for r in by_key.get(k, [])}) for k in keys}
    orig = rescreen.original_inputs(lake)
    first = next(iter(orig.values()), {})
    subq_text = {s: (orig.get(s, {}).get("subquestion") or next(x["text"] for x in plan["subquestions"] if x["id"] == s)) for s in sids}
    inputs = {"question": first.get("question") or plan["question"],
              "scope": first.get("scope", "") or "(no restriction)",
              "subquestions": "\n".join(f"{s}: {subq_text[s]}" for s in sids)}

    qk = query_kinds(plan)
    by_kind = {"topic": [], "counter": []}
    for k in keys:
        by_kind[record_kind(recs[k], qk)].append(recs[k])
    B = lakemod.SCREEN_BATCH
    n_calls = sum(-(-len(v) // B) for v in by_kind.values())

    ptxt = a.prompt.expanduser().read_text()
    missing = [v for v in ("question", "subquestions", "scope", "query_kind", "records") if "{{" + v + "}}" not in ptxt]
    print(f"lake: {lake.name}\nrecords stored: {len(recs)}; to grade: {len(keys)} "
          f"({len(keys) - len(screened_before)} never screened before)" if a.all else f"lake: {lake.name}")
    print(f"unique records screened by the model originally: {len(screened_before)} "
          f"(from {len(llm_old)} per-sub-question rows); relevant then: {sum(1 for v in old_rel.values() if v)}")
    print(f"query kinds in plan: {dict(Counter(qk.values()))}; records by kind: "
          f"{ {k: len(v) for k, v in by_kind.items()} }; ~{n_calls} calls")
    if missing:
        print(f"WARNING: prompt has no placeholder for {missing}")
    if a.dry_run:
        print("\n--- inputs (records omitted) ---")
        for k, v in inputs.items():
            print(f"{k}: {v}")
        return 0

    out = a.out or EXPERIMENTS / "rescreen" / f"{lake.name}-graded-{time.strftime('%Y%m%d-%H%M')}"
    out.mkdir(parents=True, exist_ok=False)
    pdir = out / "prompts"
    shutil.copytree(HERE / "prompts", pdir)
    (pdir / NAME).write_text(ptxt)
    old_p, llm.PROMPTS = llm.PROMPTS, pdir
    rows, bad, t0 = [], 0, time.time()
    try:
        for kind, todo in by_kind.items():
            for i in range(0, len(todo), B):
                batch = todo[i:i + B]
                r, b = grade_batch(llm, lakemod, out, batch, inputs, sids, kind)
                got = {x["key"] for x in r}
                for rec in [x for x in batch if x["key"] not in got]:  # one retry, singly
                    r1, b1 = grade_batch(llm, lakemod, out, [rec], inputs, sids, kind)
                    r += r1; b += b1
                rows += r; bad += b
                print(f"graded {kind}: {min(i + B, len(todo))}/{len(todo)}", file=sys.stderr)
    finally:
        llm.PROMPTS = old_p
    with open(out / "graded.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)

    g = {r["key"]: r for r in rows}
    graded = [k for k in keys if k in g]
    rel = {k: g[k].get("relevance") for k in graded}
    md = [f"# Graded re-screen: {lake.name}", "",
          f"{len(graded)} of {len(keys)} unique records graded ({len(keys) - len(graded)} got no valid reply). "
          f"Shape problems in {bad} items. {round(time.time() - t0)} s, prompt `{a.prompt.name}`.", ""]

    md += ["## Old binary vs new grade (unique records)", "",
           "| old \\ new | 0 | 1 | 2 | 3 | total |", "|---|---|---|---|---|---|"]
    for lab, val in (("relevant", True), ("rejected", False), ("never screened", None)):
        c = Counter(rel[k] for k in graded if old_rel.get(k) == val)
        md.append(f"| old {lab} | " + " | ".join(str(c.get(i, 0)) for i in range(4)) + f" | {sum(c.values())} |")
    c = Counter(rel.values())
    md += ["| **all** | " + " | ".join(str(c.get(i, 0)) for i in range(4)) + f" | {len(graded)} |", "",
           f"At a cut of ≥2: **{sum(1 for v in rel.values() if v is not None and v >= 2)}** relevant "
           f"(old: {sum(1 for k in graded if old_rel[k])}). At ≥3: **{sum(1 for v in rel.values() if v == 3)}**.", ""]

    hi = [k for k in graded if (rel[k] or 0) >= 2]
    bc = Counter(b for k in hi for b in g[k].get("bearing", []))
    md += ["## Coverage among grade ≥2", "",
           "bearing: " + ", ".join(f"{b} {bc.get(b, 0)}" for b in BEARING),
           "use_as: " + ", ".join(f"{u} {sum(1 for k in hi if g[k].get('use_as') == u)}" for u in USE_AS),
           f"out of scope but grade ≥2: {sum(1 for k in hi if g[k].get('in_scope') is False)}",
           f"routes to a sub-question that did not retrieve it: "
           f"{sum(1 for k in hi if set(g[k].get('routes_to', [])) - set(retrieved_by[k]))}",
           f"grade ≥2 with empty routes_to (sub-questions may not cover the question): "
           f"{sum(1 for k in hi if not g[k].get('routes_to'))}",
           "routes_to (first choice): " + ", ".join(f"{s} {sum(1 for k in hi if (g[k].get('routes_to') or [None])[0] == s)}" for s in sids),
           ""]

    def lst(title, ks, note):
        o = [f"## {title} ({len(ks)})", "", note, ""]
        for k in sorted(ks, key=lambda k: (-(rel[k] or 0), k)):
            r, x = recs[k], g[k]
            o.append(f"- [ ] **{rel[k]}** {r.get('title', k)} ({r.get('pub_year', '')}) `{k}`  \n"
                     f"  {x.get('evidence_type')} · {', '.join(x.get('bearing', []))} · "
                     f"{'in scope' if x.get('in_scope') else 'OUT of scope'} · routes {', '.join(x.get('routes_to', [])) or '-'} "
                     f"(retrieved by {', '.join(retrieved_by[k])}; old relevant for {', '.join(old_sids[k]) or 'none'}) · {x.get('use_as')}  \n"
                     f"  {x.get('reason', '')}" + (f"  \n  shape problems: {x['problems']}" if x.get("problems") else ""))
        return o + [""]

    if a.all:
        md += lst("Never screened before, now grade ≥2", [k for k in hi if old_rel[k] is None],
                  "These were cut by SCREEN_TOP=30 before any model saw them. Mark W / B / J.")
    md += lst("Old rejected, now grade ≥2", [k for k in hi if old_rel[k] is False],
              "Mark each **W** wanted / **B** borderline / **J** junk. This is the real result.")
    md += lst("Old relevant, now grade ≤1", [k for k in graded if old_rel[k] and (rel[k] or 0) <= 1],
              "Losses: would the new rubric have dropped something you wanted?")
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:14]))
    print(f"\n{out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
