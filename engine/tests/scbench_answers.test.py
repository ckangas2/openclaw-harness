"""Benchmark answer pickup (engine/evals/scbench/answers.py). Needs only python3."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "scbench"))
from answers import find_answer, parse_tag  # noqa: E402

ok = bad = 0


def t(name, cond):
    global ok, bad
    ok, bad = ok + bool(cond), bad + (not cond)
    print(("PASS " if cond else "FAIL ") + name)


with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "r1-DE01"
    ws = d / "files" / "home" / "u" / "ws" / "bench"
    (ws / "DE01" / "agent_workspace").mkdir(parents=True)
    (ws / "OTHER" / "agent_workspace").mkdir(parents=True)
    (ws / "OTHER" / "agent_workspace" / "analysis_output.json").write_text('{"n_degs": 1}')
    t("no answer → none", find_answer(d, "DE01") == (None, "none"))
    (d / "reply.json").write_text(json.dumps({"result": {"payloads": [{"text":
        "Done.\n<EVAL_ANSWER>\n```json\n{\"n_degs\": 412}\n```\n</EVAL_ANSWER>"}]}}))
    t("reply tag parsed (fenced json)", find_answer(d, "DE01") == ({"n_degs": 412}, "reply <EVAL_ANSWER>"))
    (ws / "DE01" / "agent_workspace" / "analysis_output.json").write_text('{"n_degs": 410}')
    t("answer file wins over reply, other eval's file ignored", find_answer(d, "DE01") == ({"n_degs": 410}, "analysis_output.json"))
    t("format placeholder in task echo not taken", parse_tag("<EVAL_ANSWER>{\"n\": <int>}</EVAL_ANSWER>") is None)
    t("last valid tag wins", parse_tag("<EVAL_ANSWER>{\"n\": 1}</EVAL_ANSWER> later <EVAL_ANSWER>{\"n\": 2}</EVAL_ANSWER>") == {"n": 2})

print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
