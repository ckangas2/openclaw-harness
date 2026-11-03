# scBench on the harness

scBench (LatchBio) has 6 public evals, one per task (QC, normalization, dimensionality
reduction, clustering, cell typing, differential expression). Each has its own data and a
deterministic grader. Answers are numbers checked against tolerances, so no LLM judge is
involved. Published frontier results are in the scBench repo.

## How the answer keys stay away from the worker

The answer keys are in three places, and the worker is blocked from all of them in every arm.
The block works through `blockedPatterns`: a tool call whose arguments match a pattern is
blocked and logged as `gate: blocked … off-limits pattern`.

- **The eval JSONs.** Their `notes` field is the answer key, and the grader config holds the
  ground truth. They live in the clone at `~/.bench-src/scbench`, outside the workspace.
  `prepare.py` refuses to run if the clone is inside the workspace.
- **The scBench repo on GitHub.** Its README example contains real answers, and
  `trajectories/` holds other models' answers. Searches and fetches that mention
  `scbench` or `latch-eval-tools` are blocked.
- **Your Latch token** (`~/.latch/`) is blocked too.
- **The run folders.** `result.json` includes grader reasoning that can show the truth. The
  `RUNS_DIR` path is blocked.

The worker sees only the eval's `task` text and a short note on where the data is and where to
write the answer. That note is adapted from scBench's own reference prompt.

## One-time setup (your machine)

```bash
mkdir -p ~/.bench-src && cd ~/.bench-src
git clone https://github.com/latchbio/scbench
python3 -m venv venv
venv/bin/pip install -e ./scbench          # brings latch-eval-tools and the latch SDK
venv/bin/python -c "import latch_eval_tools, latch; print('ok')"
```

Check that your analysis environment (the one pig uses) has what the tasks need. Package
installs fail closed in eval runs, so anything missing has to be installed beforehand:

```bash
<your analysis python> -c "import scanpy, anndata, pydeseq2, decoupler; print('ok')"
```

DE01 asks for pseudobulk DE. Without pydeseq2 (or R with DESeq2/edgeR), pig has to improvise.

## Step 1: data staging only (no agent)

```bash
cd ~/openclaw-harness
~/.bench-src/venv/bin/python engine/evals/scbench/prepare.py \
  --src ~/.bench-src/scbench --ws ~/.openclaw/workspace/workspace-grounded --filter bd_rhapsody
ls -la ~/.openclaw/workspace/workspace-grounded/bench/*/agent_workspace/data
```

The data is fetched with the Latch SDK, which needs a login even for public data (it fails
with "Unable to find credentials"). Log in once with a free Latch account, then retry:
`~/.bench-src/venv/bin/latch login` (opens a browser sign-in; the token is saved in `~/.latch/`). Without `--filter` it stages all six. Data is cached in `<workspace>/bench/.cache`.

## Step 2: one-eval smoke test (about 45 minutes at most)

```bash
ARMS="noaudit" REPEAT=1 FILTER=bd_rhapsody nohup bash engine/evals/overnight.sh scbench > data/agent-evals/scbench-smoke.log 2>&1 &
tail -f data/agent-evals/scbench-smoke.log
```

The log ends with the benchmark table: passed or not, and whether the answer came from
`analysis_output.json` or from the reply's `<EVAL_ANSWER>` block. If a case shows "none",
check `files/` in the run folder.

## Step 3: overnight

Six evals × 3 arms × 2 repeats = 36 cases, at most 45 minutes each, so up to about 27 hours.
Expect fewer in practice. The run below takes up to about 13.5 hours:

```bash
ARMS="noaudit baseline" REPEAT=2 nohup bash engine/evals/overnight.sh scbench > data/agent-evals/overnight.log 2>&1 & disown -a
```

Then run the digest. Its "Benchmark" table gives passed / runs per case and arm:

```bash
python3 engine/evals/digest.py data/agent-evals/runs/<TS>-scbench-{noaudit,baseline}
```

Settings you can override: `SCBENCH_SRC` (the clone), `SCBENCH_PY` (venv python), and
`CASE_TIMEOUT` (seconds per case; default 2700).

## Caveats

- Six public evals cannot settle a comparison between arms. One pass is 17%. Report this as
  "n/6 with k repeats" next to the published numbers, not as a ranking.
- The published numbers come from scBench's mini-swe-agent harness in Docker, where installs
  are allowed. Our run is the OpenClaw worker with the user's own environment, where installs
  are not allowed. Say so whenever the two are compared.
- Never tune the harness on these six cases. They are an external check, not a training set.
