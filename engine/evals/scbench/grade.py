#!/usr/bin/env python3
"""Grade one eval run of the `scbench` pack with scBench's own graders.

Runs under the scBench venv:
  $SCBENCH_PY engine/evals/scbench/grade.py RUN_DIR --src ~/.bench-src/scbench

For each case folder: the answer is <agent_workspace>/analysis_output.json (or the legacy
eval_answer.json) as copied into files/ by run_evals.sh, else the last
<EVAL_ANSWER>{json}</EVAL_ANSWER> block in the final reply. It is graded by the eval's own
grader (latch-eval-tools GRADER_REGISTRY) and written to <case>/result.json; a table goes
to RUN_DIR/benchmark.md and stdout. result.json can contain ground truth (grader reasoning):
run folders must stay out of the worker's reach (they are blocked by pattern).
"""
import argparse
import json
import re
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--src", required=True)
a = ap.parse_args()
run = Path(a.run)
src = Path(a.src).expanduser()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from answers import find_answer  # noqa: E402

try:
    from latch_eval_tools.graders import GRADER_REGISTRY
except ImportError:
    sys.exit("latch_eval_tools not importable: run this with the scBench venv python (SCBENCH_PY)")

evals = {json.loads(p.read_text())["id"]: json.loads(p.read_text())
         for p in (src / "evals").rglob("*.json") if p.name != "manifest.json"}

rows = []
for d in sorted(p for p in run.iterdir() if p.is_dir() and (p / "meta.json").exists()):
    eid = re.sub(r"^r\d+-", "", d.name)
    ev = evals.get(eid)
    if not ev:
        print(f"skip {d.name}: no eval {eid} in {src}")
        continue
    answer, source = find_answer(d, eid)
    res = {"eval_id": eid, "answer_source": source, "answer": answer, "passed": False, "score": 0.0}
    g = ev.get("grader") or {}
    if answer is None:
        res["reasoning"] = "no answer found (no analysis_output.json, no parseable <EVAL_ANSWER>)"
    elif g.get("type") not in GRADER_REGISTRY:
        res["reasoning"] = f"unknown grader {g.get('type')}"
    else:
        try:
            gr = GRADER_REGISTRY[g["type"]]().evaluate_answer(answer, g.get("config", {}))
            res.update(passed=bool(gr.passed), score=float(getattr(gr, "score", 0.0) or 0.0),
                       metrics={k: v for k, v in (gr.metrics or {}).items() if not isinstance(v, (list, dict))},
                       reasoning=(gr.reasoning or "")[:4000])
        except Exception as e:  # malformed answer
            res["reasoning"] = f"grader error: {e}"
    (d / "result.json").write_text(json.dumps(res, indent=2, default=str))
    rows.append((d.name, res))

n = len(rows)
p = sum(1 for _, r in rows if r["passed"])
out = [f"# Benchmark: scBench ({run.name})", "", f"Passed {p}/{n}", "",
       "| Case | Passed | Score | Answer from |", "|---|---|---|---|"]
out += [f"| {name} | {'yes' if r['passed'] else 'no'} | {r['score']:.2f} | {r['answer_source']} |" for name, r in rows]
(run / "benchmark.md").write_text("\n".join(out) + "\n")
print("\n".join(out))
