#!/usr/bin/env python3
"""One-time move: keep all harness data inside the repo (<repo>/data, git-ignored) instead of ~/research and ~/evals.

Step 1, code:  four default paths change (lake.py, eval.py, run_evals.sh, overnight.sh), RUNS_DIR in
               config.env(.example) is commented out so the default applies, docs are updated, data/ is
               added to .gitignore. Exact-match edits: anything unexpected stops the script. Safe to re-run.
Step 2, data:  ~/research/lakes -> data/lakes, ~/research/evals -> data/lake-evals, ~/evals -> data/agent-evals,
               today's experiment folders -> data/experiments, loose files in ~ -> data/scratch.
               Each move is checked: same number of files and bytes afterwards.
Not moved:     ~/.openclaw, ~/backups, ~/.bench-src (benchmark answer keys stay out of reach), and anything
               else in ~/research (e.g. a held-out folder) - listed, left alone.

    python3 engine/tools/use_repo_data.py            # show the plan, change nothing
    python3 engine/tools/use_repo_data.py --apply    # do it (refuses while lake/eval jobs are running)
"""
import argparse, os, shutil, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

DATA_EXPR_PY = 'Path(os.environ.get("HARNESS_DATA") or Path(__file__).resolve().parents[2] / "data")'
CODE_EDITS = [  # (file, old, new): old must occur exactly once unless new is already there
    ("engine/lake/lake.py",
     'ROOT = Path(os.environ.get("LAKE_ROOT", "~/research/lakes")).expanduser()',
     f'ROOT = Path(os.environ.get("LAKE_ROOT") or {DATA_EXPR_PY} / "lakes").expanduser()  # see paths.py'),
    ("engine/lake/lake.py",
     "Lakes live under $LAKE_ROOT (default ~/research/lakes).",
     "Lakes live under $LAKE_ROOT (default <repo>/data/lakes, or $HARNESS_DATA/lakes; see paths.py)."),
    ("engine/lake/eval.py",
     'EVAL_ROOT = Path(os.environ.get("LAKE_EVAL_ROOT", "~/research/evals")).expanduser()',
     f'EVAL_ROOT = Path(os.environ.get("LAKE_EVAL_ROOT") or {DATA_EXPR_PY} / "lake-evals").expanduser()  # see paths.py'),
    ("engine/evals/run_evals.sh",
     'RUNS_DIR="${RUNS_DIR:-$HOME/evals/runs}"',
     'RUNS_DIR="${RUNS_DIR:-${HARNESS_DATA:-$REPO/data}/agent-evals/runs}"'),
    ("engine/evals/overnight.sh",
     'RUNS="${RUNS_DIR:-$HOME/evals/runs}"',
     'RUNS="${RUNS_DIR:-${HARNESS_DATA:-$REPO/data}/agent-evals/runs}"'),
    ("config.env.example",
     'RUNS_DIR="$HOME/evals/runs"',
     '# RUNS_DIR=   # default: ${HARNESS_DATA:-<repo>/data}/agent-evals/runs. Set HARNESS_DATA to move all data.'),
]
OPTIONAL_EDITS = [  # untracked machine file: edited only if present and unchanged
    ("config.env", 'RUNS_DIR="$HOME/evals/runs"',
     '# RUNS_DIR="$HOME/evals/runs"   # now default: ${HARNESS_DATA:-<repo>/data}/agent-evals/runs'),
]
DOC_REPLACE = [  # plain-text paths in docs, help text and comments
    ("~/research/lakes", "data/lakes"), ("~/research/evals", "data/lake-evals"),
    ("~/evals/runs", "data/agent-evals/runs"), ("~/evals/overnight.log", "data/agent-evals/overnight.log"),
    ("~/evals/scbench-smoke.log", "data/agent-evals/scbench-smoke.log"),
]
DOC_SKIP = {"GOALS.md"}  # personal notes, historical


def tracked_files(repo):
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout
        return [repo / f for f in out.splitlines()]
    except Exception:
        return [p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts]


def shell_defines_repo_before(path: Path, line_text: str) -> bool:
    lines = path.read_text().splitlines()
    idx = next(i for i, l in enumerate(lines) if line_text in l)
    return any(l.lstrip().startswith(("REPO=", "export REPO=")) for l in lines[:idx])


def plan_code(repo):
    """-> (edits to make [(path, old, new)], problems [str], already done [str])"""
    todo, problems, done = [], [], []
    for rel, old, new in CODE_EDITS:
        p = repo / rel
        if not p.exists():
            problems.append(f"{rel}: file not found"); continue
        s = p.read_text()
        if new in s:
            done.append(rel); continue
        n = s.count(old)
        if n != 1:
            problems.append(f"{rel}: expected the line once, found {n}: {old}"); continue
        if rel.endswith(".sh") and not shell_defines_repo_before(p, old):
            problems.append(f"{rel}: $REPO is not set before the line to change; edit by hand"); continue
        todo.append((p, old, new))
    for rel, old, new in OPTIONAL_EDITS:
        p = repo / rel
        if p.exists() and p.read_text().count(old) == 1 and new not in p.read_text():
            todo.append((p, old, new))
    for p in tracked_files(repo):
        if p.name in DOC_SKIP or "use_repo_data" in p.name or p.suffix not in (".md", ".py", ".sh") or not p.exists():
            continue
        s = p.read_text(errors="replace")
        for old, new in DOC_REPLACE:
            if old in s:
                todo.append((p, old, new))
    return todo, problems, done


def apply_code(repo, todo):
    by_file = {}
    for p, old, new in todo:
        by_file.setdefault(p, []).append((old, new))
    for p, reps in by_file.items():
        s = p.read_text()
        for old, new in reps:
            s = s.replace(old, new)
        p.write_text(s)
    gi = repo / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if "data/" not in lines:
        gi.write_text("\n".join(lines + ["data/"]) + "\n")


# ── data ────────────────────────────────────────────────────────────────────
LOOSE = ["screen_graded.md", "before.md5", "lake-prompts.txt", "lake.bundle", "d01-lake.tgz",
         "synth-context.txt", "rescreen-context.txt"]


def plan_data(home: Path, repo: Path):
    """-> list of (src, dst) and list of things left alone."""
    data = repo / "data"
    moves, left = [], []
    research = home / "research"
    lakes = research / "lakes"
    if lakes.is_dir():
        for d in sorted(lakes.glob("*-graded-*")):
            moves.append((d, data / "experiments" / "graded-lakes" / d.name))
        moves.append((lakes, data / "lakes"))
    for name, dst in (("evals", data / "lake-evals"), ("rescreen", data / "experiments" / "rescreen"),
                      ("variants", data / "experiments" / "variants")):
        if (research / name).is_dir():
            moves.append((research / name, dst))
    if research.is_dir():
        known = {"lakes", "evals", "rescreen", "variants"}
        left += [p for p in sorted(research.iterdir()) if p.name not in known]
    if (home / "evals").is_dir():
        moves.append((home / "evals", data / "agent-evals"))
    old_exp = repo / "experiments"
    if old_exp.is_dir():
        for d in sorted(old_exp.iterdir()):
            moves.append((d, data / "experiments" / d.name))
    for f in LOOSE:
        if (home / f).is_file():
            moves.append((home / f, data / "scratch" / f))
    return moves, left


def clashes(moves):
    """Every destination checked before anything changes: no overwrites, ever."""
    out, planned = [], set()
    for src, dst in moves:
        if dst in planned:
            out.append(f"{dst}: two sources would land here")
        planned.add(dst)
        if dst.exists():
            if not (dst.is_dir() and src.is_dir()):
                out.append(f"{dst} already exists")
            else:
                c = [x.name for x in src.iterdir() if (dst / x.name).exists()]
                if c:
                    out.append(f"{dst} already has {', '.join(c[:5])}")
    return out


def tally(p: Path):
    if p.is_file():
        return 1, p.stat().st_size
    n = b = 0
    for f in p.rglob("*"):
        if f.is_file() and not f.is_symlink():
            n += 1; b += f.stat().st_size
    return n, b


def move_one(src: Path, dst: Path):
    """Move src to dst; if dst is an existing dir, merge src's children into it (no overwrites)."""
    before = tally(src)
    if dst.exists():
        if not (dst.is_dir() and src.is_dir()):
            raise SystemExit(f"refusing: {dst} exists")
        clash = [c.name for c in src.iterdir() if (dst / c.name).exists()]
        if clash:
            raise SystemExit(f"refusing: {dst} already has {', '.join(clash[:5])}")
        for c in list(src.iterdir()):
            shutil.move(str(c), str(dst / c.name))
        src.rmdir()
        return before
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    after = tally(dst)
    if after != before:
        raise SystemExit(f"CHECK FAILED for {dst}: before {before}, after {after}. Nothing else was moved.")
    return before


def running_jobs():
    pat = r"lake\.py|lake/eval\.py|synth_variants|graded_run|rescreen|overnight\.sh|run_evals\.sh"
    try:
        out = subprocess.run(["pgrep", "-af", pat], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    return [l for l in out.splitlines() if str(os.getpid()) not in l.split()[:1] and "use_repo_data" not in l]


def main(argv=None, home=None, repo=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    home = Path(home or Path.home())
    repo = Path(repo or REPO)

    todo, problems, done = plan_code(repo)
    moves, left = plan_data(home, repo)
    problems += clashes(moves)
    print("== code")
    for p, old, new in todo:
        print(f"  {p.relative_to(repo)}: {old!r} -> {new!r}")
    for d in done:
        print(f"  {d}: already updated")
    print("  .gitignore: add data/")
    for pr in problems:
        print(f"  PROBLEM {pr}")
    print("\n== data")
    for s, d in moves:
        n, b = tally(s)
        print(f"  {s} -> {d.relative_to(repo)}   ({n} files, {b / 1e6:.1f} MB)")
    for p in left:
        print(f"  left alone: {p}")
    print("  left alone: ~/.openclaw, ~/backups, ~/.bench-src")
    if problems:
        print("\nStopped: fix the problems above first (nothing changed).")
        return 2
    if not a.apply:
        print("\nPlan only. Run again with --apply.")
        return 0
    jobs = running_jobs()
    if jobs:
        print("\nRefusing: these are running:\n  " + "\n  ".join(jobs))
        return 3
    apply_code(repo, todo)
    print("\ncode updated")
    for s, d in moves:
        n, b = move_one(s, d)
        print(f"moved {s} -> {d.relative_to(repo)}  ({n} files, checked)")
    research = home / "research"
    if research.is_dir() and not any(research.iterdir()):
        research.rmdir(); print(f"removed empty {research}")
    if (repo / "experiments").is_dir() and not any((repo / "experiments").iterdir()):
        (repo / "experiments").rmdir()
    print("\ndone. Next: run the tests, check `git status`, commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
