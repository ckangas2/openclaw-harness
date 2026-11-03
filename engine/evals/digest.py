#!/usr/bin/env python3
"""Compact digest of one or more eval runs (e.g. the arms of an overnight comparison).

usage: digest.py RUN_DIR [RUN_DIR ...] [--rubric FILE] [--answers] [--similarity [MODEL]]

Prints a short markdown report (a few KB) meant to be pasted into a chat instead of
uploading run folders:
  - per arm: cases, agent minutes, hard violations, skill reads, briefs, flagged links,
    plan-only endings, audit verdicts, and whether any model ran partly on CPU
  - per case: rubric criteria met across repeats (e.g. 2/3), per arm
The rubric (packs/<pack>/evals/rubric.json) is a keyword screen, not a grade: it finds
answers worth reading. --answers also writes answers.md into each run folder (all final
answers, for spot checks); it is not printed.
Consistency across repeats of the same case: "rubric agreement" is the share of
(case, criterion) pairs where every repeat gave the same result. --similarity adds the mean
pairwise cosine similarity of the repeats' answers, embedded locally through ollama
(default model bge-m3).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if not args:
    sys.exit(__doc__)
write_answers = "--answers" in args
rubric_path = None
if "--rubric" in args:
    rubric_path = Path(args[args.index("--rubric") + 1])
sim_model = None
if "--similarity" in args:
    i = args.index("--similarity")
    nxt = args[i + 1] if i + 1 < len(args) else ""
    sim_model = nxt if nxt and not nxt.startswith("--") and not Path(nxt).is_dir() else "bge-m3"
runs = [Path(a) for a in args if not a.startswith("--") and Path(a).is_dir()]


def embed(texts, model):
    import urllib.request
    req = urllib.request.Request("http://localhost:11434/api/embed",
                                 data=json.dumps({"model": model, "input": texts, "truncate": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def mean_pairwise_cos(vecs):
    import math
    def cos(a, b):
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0
    pairs = [(i, j) for i in range(len(vecs)) for j in range(i + 1, len(vecs))]
    return sum(cos(vecs[i], vecs[j]) for i, j in pairs) / len(pairs) if pairs else None
HERE = Path(__file__).resolve().parent


def answer_text(d: Path) -> str:
    texts = []
    try:
        data = json.loads((d / "reply.json").read_text(errors="replace"))
        for p in data.get("result", {}).get("payloads", []) or []:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                texts.append(p["text"])
    except Exception:
        pass
    cli = "\n".join(texts).strip()
    last = ""
    ev = d / "agent" / "events.jsonl"
    if ev.exists():
        for line in ev.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("source") != "transcript" or e.get("type") != "assistant.message":
                continue
            msg = e.get("data", {}).get("message", e.get("data", {}))
            c = msg.get("content")
            if isinstance(c, list):
                c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            c = (c or "").strip()
            if len(c) > 40 and c not in ("REPLY_SKIP", "ANNOUNCE_SKIP"):
                last = c
    return cli if len(cli) >= 200 else (last or cli)


def case_name(d: Path) -> str:
    return re.sub(r"^r\d+-", "", d.name)


def load_rubric(run: Path):
    if rubric_path:
        return json.loads(rubric_path.read_text())
    try:
        pack = json.loads((run / "run.json").read_text()).get("pack", "")
    except Exception:
        pack = ""
    for cand in (HERE.parent.parent / "packs" / pack / "evals" / "rubric.json",):
        if cand.exists():
            return json.loads(cand.read_text())
    return {}


def check(rule, text, glog):
    """rule: {"name", "any": [regex], "none": [regex], "log": regex}"""
    ok = True
    if rule.get("any"):
        ok = ok and any(re.search(r, text, re.I | re.S) for r in rule["any"])
    if rule.get("all"):
        ok = ok and all(re.search(r, text, re.I | re.S) for r in rule["all"])
    if rule.get("none"):
        ok = ok and not any(re.search(r, text, re.I | re.S) for r in rule["none"])
    if rule.get("log"):
        ok = ok and bool(re.search(rule["log"], glog, re.I))
    return ok


arms = []
for run in runs:
    rj = {}
    try:
        rj = json.loads((run / "run.json").read_text())
    except Exception:
        pass
    label = rj.get("label") or run.name
    rubric = load_rubric(run)
    cases = sorted(p for p in run.iterdir() if p.is_dir() and (p / "meta.json").exists())
    st = {"label": label, "n": len(cases), "secs": 0, "skills": 0, "skill_cases": 0, "briefs": 0,
          "links_flagged": 0, "noanswer": 0, "warned": 0, "pass": 0, "revise": 0, "stamped": 0,
          "premise_conflict": 0, "rubric": {}, "commit": rj.get("commit", "?"), "by_case": {}, "bench": {}}
    answers_md = []
    for d in cases:
        meta = json.loads((d / "meta.json").read_text())
        st["secs"] += meta.get("seconds", 0) or 0
        glog = (d / "grounded.log").read_text(errors="replace") if (d / "grounded.log").exists() else ""
        alog = (d / "audit-gate.log").read_text(errors="replace") if (d / "audit-gate.log").exists() else ""
        ans = answer_text(d)
        # skill reads
        n_sk = 0
        ev = d / "agent" / "events.jsonl"
        if ev.exists():
            for line in ev.read_text(errors="replace").splitlines():
                if '"tool.call"' in line and re.search(r"SKILL\.md|/skills/", line):
                    n_sk += 1
        st["skills"] += n_sk
        st["skill_cases"] += 1 if n_sk else 0
        st["briefs"] += 1 if re.search(r"tool \S+ brief ", glog) else 0
        m = re.findall(r"links: cited=\d+ read=\d+ seen-only=(\d+) unknown=(\d+)", glog)
        st["links_flagged"] += sum(1 for a, b in m if int(a) + int(b) > 0)
        st["noanswer"] += 1 if "no-answer" in glog else 0
        st["warned"] += 1 if "warning written into saved answer" in glog else 0
        st["pass"] += len(re.findall(r"finalize pass=\d+ verdict=PASS", alog))
        st["revise"] += len(re.findall(r"finalize pass=\d+ verdict=REVISE", alog))
        st["stamped"] += alog.count("stamped unverified PASS")
        st["premise_conflict"] += len(re.findall(r"problems=\[[^\]]*premise", alog))
        cname = case_name(d)
        st["by_case"].setdefault(cname, []).append(ans)
        # External benchmark result (engine/evals/scbench/grade.py), if graded
        if (d / "result.json").exists():
            try:
                passed = bool(json.loads((d / "result.json").read_text()).get("passed"))
            except Exception:
                passed = False
            h, t = st["bench"].get(cname, (0, 0))
            st["bench"][cname] = (h + passed, t + 1)
        for rule in rubric.get(cname, []):
            # Log-based criteria need tool logging (absent in the gates-only baseline)
            if rule.get("log") and not re.search(r"\btool ", glog):
                continue
            k = (cname, rule["name"])
            st.setdefault("per_rep", {}).setdefault(k, []).append(check(rule, ans, glog))
            hit, tot = st["rubric"].get(k, (0, 0))
            st["rubric"][k] = (hit + (1 if check(rule, ans, glog) else 0), tot + 1)
        answers_md.append(f"\n\n## {d.name} ({round((meta.get('seconds') or 0) / 60, 1)} min)\n\n{ans}")
    # CPU spill: any loaded model not at 100% GPU
    ps = run / "ollama-ps.log"
    spill = ""
    if ps.exists():
        bad = [l for l in ps.read_text(errors="replace").splitlines() if re.search(r"\d+%/\d+%\s+CPU/GPU|\b\d+% CPU\b", l)]
        spill = f"{len(bad)} sample(s) with CPU use" if bad else "all GPU"
    st["spill"] = spill or "n/a"
    # hard violations from the scorecard
    sc = run / "scorecard.md"
    hv = re.search(r"\*\*(\d+) hard violation", sc.read_text()) if sc.exists() else None
    st["hard"] = hv.group(1) if hv else "?"
    reps = [v for v in st.get("per_rep", {}).values() if len(v) > 1]
    st["agree"] = f"{100 * sum(1 for v in reps if len(set(v)) == 1) / len(reps):.0f}%" if reps else "n/a"
    st["sim"] = "n/a"
    if sim_model:
        try:
            vals = []
            for cname, answers in st["by_case"].items():
                texts = [a[:8000] for a in answers if a.strip()]
                if len(texts) > 1:
                    vals.append(mean_pairwise_cos(embed(texts, sim_model)))
            st["sim"] = f"{sum(vals) / len(vals):.3f}" if vals else "n/a"
        except Exception as e:
            st["sim"] = f"error ({str(e)[:40]})"
    if write_answers:
        (run / "answers.md").write_text(f"# Answers: {label}\n" + "".join(answers_md))
    arms.append(st)

out = ["# Digest", "", f"Runs: {', '.join(r.name for r in runs)} · commit {arms[0]['commit'] if arms else '?'}", ""]
out.append("| Arm | Cases | Agent min (mean/case) | Hard | Skill reads (cases) | Briefs | Links flagged | Plan-only endings | Warnings | Audit PASS / REVISE | Stamped PASS | Premise conflicts | GPU | Rubric agreement across repeats | Answer similarity |")
out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for s in arms:
    n = max(s["n"], 1)
    out.append(f"| {s['label']} | {s['n']} | {s['secs'] // 60} ({s['secs'] / n / 60:.1f}) | {s['hard']} | {s['skills']} ({s['skill_cases']}) | "
               f"{s['briefs']} | {s['links_flagged']} | {s['noanswer']} | {s['warned']} | {s['pass']} / {s['revise']} | {s['stamped']} | {s['premise_conflict']} | {s['spill']} | {s['agree']} | {s['sim']} |")
bench_cases = []
for s in arms:
    for c in s["bench"]:
        if c not in bench_cases:
            bench_cases.append(c)
if bench_cases:
    out += ["", "## Benchmark (the benchmark's own graders: passed / runs)", "",
            "| Case | " + " | ".join(s["label"] for s in arms) + " |", "|---|" + "---|" * len(arms)]
    for c in bench_cases:
        out.append(f"| {c} | " + " | ".join(
            (lambda ht: f"{ht[0]}/{ht[1]}" if ht[1] else "-")(s["bench"].get(c, (0, 0))) for s in arms) + " |")
    tot = [(sum(h for h, _ in s["bench"].values()), sum(t for _, t in s["bench"].values())) for s in arms]
    out.append("| **total** | " + " | ".join(f"**{h}/{t}**" if t else "-" for h, t in tot) + " |")
keys = []
for s in arms:
    for k in s["rubric"]:
        if k not in keys:
            keys.append(k)
if keys:
    out += ["", "## Rubric screen (criteria met across repeats; keyword screen, not a grade)", "",
            "| Case | Criterion | " + " | ".join(s["label"] for s in arms) + " |",
            "|---|---|" + "---|" * len(arms)]
    for k in keys:
        cells = []
        for s in arms:
            h, t = s["rubric"].get(k, (0, 0))
            cells.append(f"{h}/{t}" if t else "-")
        out.append(f"| {k[0]} | {k[1]} | " + " | ".join(cells) + " |")
print("\n".join(out))
