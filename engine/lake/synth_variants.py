#!/usr/bin/env python3
"""Bolt-on synthesis variants for a finished lake. The lake itself and the default workflow are untouched:
every variant runs on a copy of the lake's pre-synthesis files, in a new output folder.

seeds        Re-run synthesize N times with sampling (default temperature 0.7, seeds 1..N). Only the
             synthesize call samples; check, revise and summary stay at the lake's normal settings
             (temperature 0), so every sample is checked exactly as the real report is. Then each point of
             the lake's own report is matched against every sample (same sub-question, same cited papers:
             Jaccard >= 0.5 on paper keys) and marked "in k/N samples". Points that only the samples produced
             are listed too.
hypotheses   One call over all evidence: up to three competing interpretations, each with a statement,
             supporting points, evidence it does not explain, and what would distinguish it. Every
             statement / supporting / unexplained point goes through the lake's own check_point and revise
             loop (synth.revise_points); "would distinguish" is a proposal and cites nothing.

    python3 engine/lake/synth_variants.py seeds <lake> [--n 5] [--temp 0.7]
    python3 engine/lake/synth_variants.py hypotheses <lake> [--temp 0] [--seed 42]

Output: <repo>/data/experiments/variants/<lake>-<mode>-<stamp>/  (report.html files to open; summary.md)
Do not run during an agent eval: same GPU and model.
"""
import argparse, json, shutil, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import EXPERIMENTS  # noqa: E402  <repo>/data/experiments, or $HARNESS_DATA/experiments

import llm  # noqa: E402
import synth  # noqa: E402
import render  # noqa: E402
from store import read_json, read_jsonl, write_json  # noqa: E402

PRE = ["plan.json", "evidence.jsonl", "evidence_rejected.jsonl", "selected.json", "premises.json",
       "manifest.json", "screen.jsonl"]
JACCARD = 0.5


def copy_pre(lake: Path, dest: Path, with_synthesis=False):
    dest.mkdir(parents=True)
    for f in PRE + (["synthesis.json", "synthesis_raw.json"] if with_synthesis else []):
        if (lake / f).exists():
            shutil.copy(lake / f, dest / f)
    m = read_json(dest / "manifest.json", {}) or {}
    if not with_synthesis:
        for st in ("synthesize", "check", "report"):
            m.get("stages", {}).pop(st, None)
    write_json(dest / "manifest.json", m)


class sampling:
    """Temporarily change the model options (used around the synthesize call only)."""
    def __init__(self, temp, seed):
        self.opts = {"temperature": temp, "seed": seed, "top_p": 1.0 if temp == 0 else 0.95}

    def __enter__(self):
        self.old = dict(llm.OPTIONS)
        llm.OPTIONS.clear(); llm.OPTIONS.update({**self.old, **self.opts})

    def __exit__(self, *a):
        llm.OPTIONS.clear(); llm.OPTIONS.update(self.old)


def papers(p, ev):
    return {ev[x]["key"] for x in p.get("eids", []) if x in ev}


def jacc(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def live_points(syn):
    for sid, b in syn.get("subquestions", {}).items():
        for p in b.get("points", []):
            if not p.get("dropped"):
                yield sid, p


# ── seeds ───────────────────────────────────────────────────────────────────
def run_seeds(lake: Path, out: Path, n: int, temp: float):
    ev = {e["eid"]: e for e in read_jsonl(lake / "evidence.jsonl")}
    samples = []
    for i in range(1, n + 1):
        d = out / f"seed{i}"
        copy_pre(lake, d)
        m = read_json(d / "manifest.json")
        m["options"] = {**m.get("options", {}), "temperature": temp, "seed": i, "note": "sampling applies to synthesize only"}
        write_json(d / "manifest.json", m)
        print(f"\n== seed {i}/{n}", file=sys.stderr)
        with sampling(temp, i):
            synth.stage_synthesize(d)
        synth.stage_check(d)
        synth.stage_report(d)
        render.render_lake(d)
        samples.append(read_json(d / "synthesis.json"))

    ref_dir = out / "reference"
    copy_pre(lake, ref_dir, with_synthesis=True)
    ref = read_json(ref_dir / "synthesis.json")
    stab, unmatched = {}, []
    for sid, p in live_points(ref):
        kp = papers(p, ev)
        k = sum(1 for s in samples if any(ssid == sid and jacc(kp, papers(q, ev)) >= JACCARD for ssid, q in live_points(s)))
        stab[p["id"]] = {"in": k, "of": n}
    # points the samples made that the reference did not: cluster greedily across samples
    clusters = []
    for i, s in enumerate(samples, 1):
        for sid, q in live_points(s):
            kq = papers(q, ev)
            if any(rsid == sid and jacc(kq, papers(p, ev)) >= JACCARD for rsid, p in live_points(ref)):
                continue
            for c in clusters:
                if c["sid"] == sid and jacc(kq, c["papers"]) >= JACCARD:
                    c["seeds"].add(i); break
            else:
                clusters.append({"sid": sid, "papers": kq, "seeds": {i}, "text": q["text"], "eids": q.get("eids", [])})
    write_json(ref_dir / "stability.json", stab)
    render.render_lake(ref_dir)

    md = [f"# Synthesis stability: {lake.name}", "",
          f"{n} samples at temperature {temp} (seeds 1..{n}); synthesize only, checks at the lake's settings. "
          f"A reference point counts as present in a sample if that sample has a point in the same sub-question citing "
          f"mostly the same papers (Jaccard >= {JACCARD}).", "",
          "| point | in samples | text |", "|---|---|---|"]
    for sid, p in live_points(ref):
        s = stab[p["id"]]
        md.append(f"| {p['id']} | {s['in']}/{s['of']} | {p['text'][:140].replace('|', '/')} |")
    dist = {}
    for v in stab.values():
        dist[v["in"]] = dist.get(v["in"], 0) + 1
    md += ["", "Distribution: " + ", ".join(f"{k}/{n}: {dist[k]} points" for k in sorted(dist, reverse=True)), "",
           f"## Points only the samples made ({len(clusters)})", ""]
    for c in sorted(clusters, key=lambda c: (-len(c["seeds"]), c["sid"])):
        md.append(f"- **{c['sid']}**, in {len(c['seeds'])}/{n} samples: {c['text']} ({', '.join(c['eids'])})")
    md += ["", f"Open `reference/report.html` (the lake's own report, marked) and `seedN/report.html` (each sample)."]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:6 + len(stab)]))


# ── hypotheses ──────────────────────────────────────────────────────────────
ITEM = {"type": "object", "required": ["text", "eids"],
        "properties": {"text": {"type": "string"}, "eids": {"type": "array", "items": {"type": "string"}}}}
HYP_SCHEMA = {"type": "object", "required": ["hypotheses", "only_one_supported", "note"], "properties": {
    "hypotheses": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
        "type": "object", "required": ["label", "statement", "supporting", "unexplained", "would_distinguish"],
        "properties": {"label": {"type": "string"}, "statement": ITEM,
                       "supporting": {"type": "array", "maxItems": 4, "items": ITEM},
                       "unexplained": {"type": "array", "maxItems": 3, "items": ITEM},
                       "would_distinguish": {"type": "string"}}}},
    "only_one_supported": {"type": "boolean"}, "note": {"type": "string"}}}


def run_hypotheses(lake: Path, out: Path, temp: float, seed: int):
    d = out / "hypotheses"
    copy_pre(lake, d)
    plan = read_json(d / "plan.json")
    evs = read_jsonl(d / "evidence.jsonl")
    ev = {e["eid"]: e for e in evs}
    order = {"independent_comparison": 0, "review": 1, "primary_study": 2, "new_method": 3, "other": 4}
    _pos = {e["eid"]: i for i, e in enumerate(evs)}    # eids are content hashes, not counters
    evs = sorted(evs, key=lambda e: (order.get(e["evidence_type"], 9), _pos.get(e["eid"], 1 << 30)))
    with sampling(temp, seed):
        res, _ = llm.call(d, "hypotheses", "hypotheses.md",
                          {"question": plan["question"],
                           "subquestions": "\n".join(f"{s['id']}: {s['text']}" for s in plan["subquestions"]),
                           "evidence": "\n".join(synth.card(e, quote_chars=200) for e in evs)}, HYP_SCHEMA)
    write_json(d / "hypotheses_raw.json", res)

    # every claim through the lake's own check + revise (temperature 0)
    sections, pts_all, subqs = {}, [], []
    for i, h in enumerate(res.get("hypotheses", [])[:3], 1):
        hid = f"H{i}"
        pts = [{"id": f"{hid}.0", "text": h["statement"]["text"], "eids": h["statement"].get("eids", []), "kind": "statement"}]
        pts += [{"id": f"{hid}.s{j}", "text": x["text"], "eids": x.get("eids", []), "kind": "finding"}
                for j, x in enumerate(h.get("supporting", []), 1)]
        pts += [{"id": f"{hid}.u{j}", "text": x["text"], "eids": x.get("eids", []), "kind": "unexplained"}
                for j, x in enumerate(h.get("unexplained", []), 1)]
        st = synth.revise_points(d, pts, ev, list(ev), hid, question=plan["question"])
        print(f"check {hid}: {st}", file=sys.stderr)
        sections[hid] = {"points": pts, "gaps": [f"Would distinguish (a proposal, not a finding): {h.get('would_distinguish', '')}"]}
        subqs.append({"id": hid, "text": h.get("label", hid)})
        pts_all += pts
    summary = []
    if res.get("only_one_supported"):
        summary.append({"id": "note", "text": f"Only one interpretation is supported by this evidence. {res.get('note', '')}",
                        "eids": [], "kind": "note"})
    elif res.get("note"):
        summary.append({"id": "note", "text": res["note"], "eids": [], "kind": "note"})
    write_json(d / "synthesis.json", {"subquestions": sections, "summary": summary, "checked": time.strftime("%Y-%m-%dT%H:%M:%S")})
    plan_view = dict(plan, question=f"{plan['question']} (competing interpretations)", subquestions=subqs)
    write_json(d / "plan.json", plan_view)
    write_json(d / "plan.original.json", plan)
    render.render_lake(d)

    md = [f"# Competing interpretations: {lake.name}", "",
          f"temperature {temp}, seed {seed}; each statement checked by the lake's own check_point/revise loop.", ""]
    if res.get("only_one_supported"):
        md += [f"**Only one interpretation supported.** {res.get('note', '')}", ""]
    for i, h in enumerate(res.get("hypotheses", [])[:3], 1):
        pts = sections[f"H{i}"]["points"]
        flags = lambda p: " **(dropped by check)**" if p.get("dropped") else (" **UNVERIFIED**" if p.get("issues") else "")
        md += [f"## H{i}. {h.get('label', '')}", "", f"{pts[0]['text']} ({', '.join(pts[0]['eids'])}){flags(pts[0])}", "",
               "Supporting:"] + [f"- {p['text']} ({', '.join(p['eids'])}){flags(p)}" for p in pts if p["kind"] == "finding"] + \
              ["", "Does not explain:"] + [f"- {p['text']} ({', '.join(p['eids'])}){flags(p)}" for p in pts if p["kind"] == "unexplained"] + \
              ["", f"Would distinguish: {h.get('would_distinguish', '')}", ""]
    n_issue = sum(1 for p in pts_all if p.get("issues") and not p.get("dropped"))
    n_drop = sum(1 for p in pts_all if p.get("dropped"))
    md += [f"Checks: {len(pts_all)} claims, {n_drop} dropped, {n_issue} unverified.",
           "", "Open `hypotheses/report.html`."]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["seeds", "hypotheses"])
    ap.add_argument("lake", type=Path)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--temp", type=float)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    lake = a.lake.expanduser().resolve()
    if not (lake / "evidence.jsonl").exists():
        raise SystemExit(f"{lake}: no evidence.jsonl (run the lake through extract first)")
    out = a.out or EXPERIMENTS / "variants" / f"{lake.name}-{a.mode}-{time.strftime('%Y%m%d-%H%M')}"
    out.mkdir(parents=True, exist_ok=False)
    print(out)
    if a.mode == "seeds":
        if not (lake / "synthesis.json").exists():
            raise SystemExit("seeds compares against the lake's own synthesis; run the lake through check first")
        run_seeds(lake, out, a.n, 0.7 if a.temp is None else a.temp)
    else:
        run_hypotheses(lake, out, 0.0 if a.temp is None else a.temp, a.seed)
    print(f"\n{out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
