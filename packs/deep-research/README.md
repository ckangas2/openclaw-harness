# deep-research pack (evidence lake)

Dev cases for `engine/lake`. Run from the repo root:

    python3 engine/lake/eval.py run                    # all cases → data/lake-evals/<stamp>/scores.md
    python3 engine/lake/eval.py run --only d04-nonexistent-benchmark
    python3 engine/lake/eval.py repro d01-doublet-benchmarks   # same case twice from scratch, compared
    python3 engine/lake/eval.py score data/lake-evals/<stamp> # re-score without model calls

Scored from lake files only: key papers found / selected / cited (DOI or title pattern), points fixed by
the checks, points left UNVERIFIED, rejected quotes, independent references, out-of-scope exclusions,
premise check (trap cases), forbidden names presented as findings, time.

Held-out cases: same JSON format, kept outside the repo and away from whoever tunes the protocol
(e.g. `~/research/heldout/cases.json`), run with `--cases`. Do not change prompts or rules in
response to held-out results; record them and check whether dev-set changes carry over.
