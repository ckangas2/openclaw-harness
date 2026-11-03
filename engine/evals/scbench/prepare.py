#!/usr/bin/env python3
"""Stage scBench evals for the worker and write them as cases of the `scbench` pack.

Runs under the scBench venv (it needs latch-eval-tools and the latch SDK):
  $SCBENCH_PY engine/evals/scbench/prepare.py --src ~/.bench-src/scbench --ws "$WORKER_WS" [--filter ID]

For each eval JSON under <src>/evals:
  - downloads its data (latch-eval-tools' own download_data, cached) into
    <ws>/bench/<eval_id>/agent_workspace/data, made read-only
  - writes packs/scbench/evals/cases/<eval_id>.md: the eval's `task` text plus workspace
    instructions adapted from the reference harness (latch-eval-tools data_instructions.md)

Only `task` reaches the worker. The eval JSON's grader config (ground truth) and `notes`
(the answer key) stay in <src>, which the worker is blocked from reading (blockedPatterns).
"""
import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True, help="scBench clone (outside the worker's workspace)")
ap.add_argument("--ws", required=True, help="worker workspace")
ap.add_argument("--filter", default="", help="only evals whose id contains this (several: a|b|c)")
ap.add_argument("--no-download", action="store_true", help="write cases only (for tests)")
a = ap.parse_args()

src = Path(a.src).expanduser().resolve()
ws = Path(a.ws).expanduser().resolve()
repo = Path(__file__).resolve().parents[3]
cases = repo / "packs" / "scbench" / "evals" / "cases"
bench = ws / "bench"
if str(src).startswith(str(ws) + os.sep):
    sys.exit(f"refusing: {src} is inside the worker workspace; keep the answer keys out of its reach")

evals = sorted(p for p in (src / "evals").rglob("*.json") if p.name != "manifest.json")
if a.filter:
    pats = [f for f in a.filter.split("|") if f]  # one id fragment, or several separated by |
    evals = [p for p in evals if any(f in p.stem for f in pats)]
if not evals:
    sys.exit(f"no evals found under {src}/evals (filter {a.filter!r})")

if not a.no_download:
    import latch_eval_tools.harness.utils as U
    # Cache and workspaces under <ws>/bench, so the cache-to-workspace hardlinks stay on one filesystem.
    U.get_project_root = lambda: bench

cases.mkdir(parents=True, exist_ok=True)
for old in cases.glob("*.md"):
    old.unlink()

INSTRUCTIONS = """

---
Workspace for this task: {d}
- The data is in {d}/data. Treat it as read-only: do not modify, move or delete it.
- Write your scripts and intermediate files in {d} (outside data/).
- You should not need to download any other data. The data can be large; be mindful of RAM.
- Package installs are not available in this run; use the analysis environment you normally use.
- When you are done, write the JSON object with the required fields to {d}/analysis_output.json,
  and end your reply with the same <EVAL_ANSWER>...</EVAL_ANSWER> block.
"""

for p in evals:
    ev = json.loads(p.read_text())
    eid = ev["id"]
    work = bench / eid
    d = work / "agent_workspace"
    if not a.no_download:
        print(f"== {eid}: staging {ev.get('data_node')}", flush=True)
        try:
            U.download_data(ev.get("data_node") or [], work, ".cache")
        except Exception as e:
            if "credentials" in str(e).lower() or type(e).__name__ == "AuthenticationError":
                sys.exit("Latch login needed to download scBench data (free account): "
                         f"run  {Path(sys.executable).parent}/latch login  and retry.")
            raise
        for f in (d / "data").rglob("*"):
            if re.search(r"scbench|latch-eval-tools|bench-src", str(f.relative_to(d)), re.I):
                print(f"   WARNING: {f.name} matches a blocked pattern; the worker will not be able to open it")
            if f.is_file():
                f.chmod(f.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    else:
        (d / "data").mkdir(parents=True, exist_ok=True)
    (cases / f"{eid}.md").write_text(ev["task"].rstrip() + INSTRUCTIONS.format(d=d))
    print(f"   case {cases / (eid + '.md')}")
print(f"{len(evals)} case(s) ready")
