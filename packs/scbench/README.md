# scbench (external benchmark adapter)

Cases here are generated, not written by hand: `engine/evals/scbench/prepare.py` turns each
public scBench eval (LatchBio, github.com/latchbio/scbench) into a case containing only the
eval's task text plus workspace instructions, and stages its data under
`<worker workspace>/bench/<eval_id>/agent_workspace/data`. Grading uses scBench's own
deterministic graders (`engine/evals/scbench/grade.py`). Answer keys never enter this repo or
the worker's workspace, and `blockedPatterns` stops the worker from fetching the benchmark,
its published trajectories, or the run folders.

Run it through overnight.sh (setup in engine/evals/scbench/SETUP.md).
