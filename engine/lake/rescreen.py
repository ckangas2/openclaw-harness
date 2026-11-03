#!/usr/bin/env python3
"""Diagnostic: re-screen a finished lake's records with ONE change to the relevance rubric.

Uses the lake's own screen_batch, record cards, schema, batch size and record order, and the
exact question/sub-question/scope inputs of the original run (read from its calls.jsonl). The
only difference is the "relevant" paragraph of screen.md. Nothing in the lake or the repo is
written: the modified prompt lives in a temp folder, and output goes to --out.

    python3 engine/lake/rescreen.py <lake-or-case-dir>                 # relaxed rubric (default)
    python3 engine/lake/rescreen.py <dir> --control                    # also re-run the ORIGINAL prompt
    python3 engine/lake/rescreen.py <dir> --rubric my_rubric.txt       # your own replacement paragraph
    python3 engine/lake/rescreen.py <dir> --dry-run                    # show the prompt change and counts, no model calls

Output (default <repo>/data/experiments/rescreen/<case>-<stamp>/): rescreen.jsonl, calls.jsonl, screen.md used,
summary.md (old vs new relevant per sub-question, flips, and the flipped titles for a human to judge).
Do not run during an agent eval: it uses the same GPU and model.
"""
import argparse, json, shutil, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import EXPERIMENTS  # noqa: E402  <repo>/data/experiments, or $HARNESS_DATA/experiments

RELAXED = ('Then decide "relevant": true if the document could contain evidence bearing on any part of the '
           "review's question, including outcomes, delivery, mechanisms or failures, even if it is not specific "
           "to this sub-question or studies a related system; false only if it is clearly about a different "
           "topic that merely shares keywords. The evidence type does not decide relevance, but keep it accurate: "
           "a new method's comparison to others is the authors' own claim, not an independent one.")
MARK = 'Then decide "relevant":'


def swap_rubric(prompt: str, new_para: str) -> str:
    lines = prompt.splitlines(keepends=True)
    hits = [i for i, l in enumerate(lines) if l.startswith(MARK)]
    if len(hits) != 1:
        raise SystemExit(f"screen.md: expected exactly one line starting with {MARK!r}, found {len(hits)}")
    i = hits[0]
    lines[i] = new_para.strip() + ("\n" if lines[i].endswith("\n") else "")
    return "".join(lines)


def original_inputs(lake: Path) -> dict:
    """sid -> the exact question/subquestion/scope inputs the original screening used, plus its prompt hash."""
    out = {}
    for l in open(lake / "calls.jsonl"):
        c = json.loads(l)
        st = c.get("step", "")
        if st.startswith("screen:") and st[7:] not in out:
            i = c.get("inputs", {})
            out[st[7:]] = {"question": i.get("question", ""), "subquestion": i.get("subquestion", ""),
                           "scope": i.get("scope", ""), "prompt_hash": c.get("prompt_hash")}
    return out


def run_arm(name, prompt_text, lake, out, plan, recs_by_key, screened, inputs, lakemod, llm):
    pdir = out / f"prompts-{name}"
    pdir.mkdir()
    for f in (HERE / "prompts").iterdir():
        shutil.copy(f, pdir / f.name)
    (pdir / "screen.md").write_text(prompt_text)
    old_prompts, llm.PROMPTS = llm.PROMPTS, pdir
    arm_dir = out / name
    arm_dir.mkdir()
    rows = []
    try:
        for sub in plan["subquestions"]:
            sid = sub["id"]
            keys = screened.get(sid, [])
            if not keys:
                continue
            todo = [recs_by_key[k] for k in keys if k in recs_by_key]
            inp = inputs.get(sid, {})
            subq = dict(sub, text=inp.get("subquestion") or sub["text"])
            got_rows = []
            for i in range(0, len(todo), lakemod.SCREEN_BATCH):
                batch = todo[i:i + lakemod.SCREEN_BATCH]
                r = lakemod.screen_batch(arm_dir, subq, batch, inp.get("scope", ""), inp.get("question") or plan["question"])
                got_rows += r
                print(f"{name}: {sid} {min(i + lakemod.SCREEN_BATCH, len(todo))}/{len(todo)}", file=sys.stderr)
            got = {r["key"] for r in got_rows}
            for rec in [t for t in todo if t["key"] not in got]:  # one retry, singly, as stage_screen does
                got_rows += lakemod.screen_batch(arm_dir, subq, [rec], inp.get("scope", ""),
                                                 inp.get("question") or plan["question"]) or \
                    [{"sid": sid, "key": rec["key"], "relevant": None, "reason": "unscreened (no valid reply)", "by": "rule"}]
            rows += got_rows
    finally:
        llm.PROMPTS = old_prompts
    with open(out / f"{name}.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    return rows


def compare(old_rows, new_rows):
    old = {(r["sid"], r["key"]): r for r in old_rows}
    new = {(r["sid"], r["key"]): r for r in new_rows}
    per, up, down = {}, [], []
    for k, n in new.items():
        o = old.get(k)
        s = per.setdefault(k[0], {"screened": 0, "old": 0, "new": 0, "up": 0, "down": 0})
        s["screened"] += 1
        s["old"] += bool(o and o.get("relevant"))
        s["new"] += bool(n.get("relevant"))
        if o and not o.get("relevant") and n.get("relevant"):
            s["up"] += 1; up.append((k, o, n))
        if o and o.get("relevant") and not n.get("relevant"):
            s["down"] += 1; down.append((k, o, n))
    return per, up, down


def table(per):
    lines = ["| sub-question | screened | relevant before | relevant after | rejected→relevant | relevant→rejected |",
             "|---|---|---|---|---|---|"]
    tot = {"screened": 0, "old": 0, "new": 0, "up": 0, "down": 0}
    for sid in sorted(per):
        s = per[sid]
        lines.append(f"| {sid} | {s['screened']} | {s['old']} | {s['new']} | {s['up']} | {s['down']} |")
        for k in tot:
            tot[k] += s[k]
    lines.append(f"| **all** | {tot['screened']} | {tot['old']} | {tot['new']} | {tot['up']} | {tot['down']} |")
    return "\n".join(lines), tot


def flips_md(flips, recs_by_key, title):
    out = [f"\n## {title} ({len(flips)})\n",
           "Judge each: **W** wanted (should be read), **B** borderline/adjacent, **J** junk.\n"]
    for (sid, key), o, n in sorted(flips, key=lambda x: (x[0][0], x[0][1])):
        r = recs_by_key.get(key, {})
        out.append(f"- [ ] **{sid}** {r.get('title', key)} ({r.get('pub_year', '')}) `{key}` — {n.get('evidence_type', '')}"
                   f"\n  - before: {o.get('reason', '')}\n  - after: {n.get('reason', '')}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lake", type=Path)
    ap.add_argument("--rubric", type=Path, help="file holding the replacement 'Then decide \"relevant\": ...' paragraph")
    ap.add_argument("--control", action="store_true", help="also re-run the original prompt (reproducibility check)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    import lake as lakemod  # the lake's own module: screen_batch, SCREEN_BATCH, record_card, schema
    import llm

    lake = a.lake.expanduser().resolve()
    plan = json.loads((lake / "plan.json").read_text())
    recs_by_key = {}
    for l in open(lake / "records.jsonl"):
        r = json.loads(l); recs_by_key[r["key"]] = r
    old_rows = [json.loads(l) for l in open(lake / "screen.jsonl") if l.strip()]
    llm_rows = [r for r in old_rows if r.get("by") == "llm"]
    # keep the original order per sub-question so batches see records in the same order
    order = {}
    for r in old_rows:
        if r.get("by") == "llm":
            order.setdefault(r["sid"], []).append(r["key"])
    inputs = original_inputs(lake)

    original = (HERE / "prompts" / "screen.md").read_text()
    new_para = a.rubric.read_text() if a.rubric else RELAXED
    relaxed = swap_rubric(original, new_para)
    cur_hash = llm.prompt_hash("screen.md")
    run_hashes = {v["prompt_hash"] for v in inputs.values()}

    print(f"lake: {lake.name}\nscreened by model: {len(llm_rows)} rows over {len(order)} sub-questions "
          f"(~{sum(-(-len(v) // lakemod.SCREEN_BATCH) for v in order.values())} calls per arm)")
    if run_hashes != {cur_hash}:
        print(f"WARNING: screen.md has changed since this run (run {run_hashes}, now {cur_hash}). "
              "Use --control to measure the drift.")
    print("\nrubric BEFORE:\n  " + next(l for l in original.splitlines() if l.startswith(MARK)))
    print("rubric AFTER:\n  " + new_para.strip())
    if a.dry_run:
        return 0

    out = (a.out or EXPERIMENTS / "rescreen" / f"{lake.name}-{time.strftime('%Y%m%d-%H%M')}")
    out.mkdir(parents=True, exist_ok=False)
    (out / "screen.relaxed.md").write_text(relaxed)
    t0 = time.time()
    new_rows = run_arm("relaxed", relaxed, lake, out, plan, recs_by_key, order, inputs, lakemod, llm)
    ctrl_rows = run_arm("control", original, lake, out, plan, recs_by_key, order, inputs, lakemod, llm) if a.control else None

    per, up, down = compare(llm_rows, new_rows)
    tbl, tot = table(per)
    md = [f"# Re-screen: {lake.name}", "",
          f"One change: the 'relevant' paragraph of screen.md. Same records, order, batches, inputs, model, seed. "
          f"{round(time.time() - t0)} s.", "",
          "**Before:** " + next(l for l in original.splitlines() if l.startswith(MARK)), "",
          "**After:** " + new_para.strip(), "", tbl]
    if ctrl_rows is not None:
        cper, cup, cdown = compare(llm_rows, ctrl_rows)
        ctbl, ctot = table(cper)
        md += ["", "## Control: original prompt re-run",
               f"Flips here are drift (Ollama/model/state), not the rubric. "
               f"{ctot['up'] + ctot['down']} of {ctot['screened']} decisions changed.", "", ctbl]
    md.append(flips_md(up, recs_by_key, "Rejected before, relevant now"))
    md.append(flips_md(down, recs_by_key, "Relevant before, rejected now"))
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n" + tbl)
    if ctrl_rows is not None:
        print(f"\ncontrol: {ctot['up'] + ctot['down']} of {ctot['screened']} decisions changed with the ORIGINAL prompt")
    print(f"\n{out / 'summary.md'}  <- flipped titles to judge (W/B/J)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
