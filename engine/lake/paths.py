"""Where the harness keeps its data.

Everything lives under one data root: $HARNESS_DATA, default <repo>/data (git-ignored).

    data/lakes/          evidence lakes                     (override: LAKE_ROOT)
    data/lake-evals/     lake eval runs, eval.py            (override: LAKE_EVAL_ROOT)
    data/agent-evals/    agent harness runs and logs        (runs/ override: RUNS_DIR)
    data/experiments/    rescreen, graded lakes, variants   (override: HARNESS_EXPERIMENTS)
    data/scratch/        loose files

lake.py and eval.py compute the same defaults inline (no import needed); test_paths.py checks they agree.
The shell runners use ${HARNESS_DATA:-$REPO/data}.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _p(env, default):
    return Path(os.environ.get(env) or default).expanduser()


DATA = _p("HARNESS_DATA", REPO / "data")
LAKES = _p("LAKE_ROOT", DATA / "lakes")
LAKE_EVALS = _p("LAKE_EVAL_ROOT", DATA / "lake-evals")
AGENT_EVALS = DATA / "agent-evals"
AGENT_RUNS = _p("RUNS_DIR", AGENT_EVALS / "runs")
EXPERIMENTS = _p("HARNESS_EXPERIMENTS", DATA / "experiments")
SCRATCH = DATA / "scratch"
