"""Offline tests for engine/tools/use_repo_data.py on a fake repo and a fake home folder.

    python3 -m unittest engine/tests/test_use_repo_data.py
"""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import use_repo_data as u  # noqa: E402

LAKE_PY = '''"""Lakes live under $LAKE_ROOT (default ~/research/lakes).
"""
import os
from pathlib import Path
ROOT = Path(os.environ.get("LAKE_ROOT", "~/research/lakes")).expanduser()
'''
EVAL_PY = '''import os
from pathlib import Path
EVAL_ROOT = Path(os.environ.get("LAKE_EVAL_ROOT", "~/research/evals")).expanduser()
'''
RUN_SH = 'REPO="$(cd "$(dirname "$0")/../.." && pwd)"\nRUNS_DIR="${RUNS_DIR:-$HOME/evals/runs}"\necho "$RUNS_DIR"\n'
OVN_SH = 'REPO="$(cd "$(dirname "$0")/../.." && pwd)"\nRUNS="${RUNS_DIR:-$HOME/evals/runs}"\necho "$RUNS"\n'


def files(root, spec):
    for rel, text in spec.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class UseRepoDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo, self.home = t / "repo", t / "home"
        files(self.repo, {"engine/lake/lake.py": LAKE_PY, "engine/lake/eval.py": EVAL_PY,
                          "engine/evals/run_evals.sh": RUN_SH, "engine/evals/overnight.sh": OVN_SH,
                          "config.env.example": 'X=1\nRUNS_DIR="$HOME/evals/runs"\n',
                          "config.env": 'RUNS_DIR="$HOME/evals/runs"\n',
                          "README.md": "see ~/research/lakes/<slug> and ~/evals/runs/<run>\n",
                          "GOALS.md": "old kit in ~/evals/\n",
                          "packs/deep-research/README.md": "held-out at ~/research/heldout/cases.json\n",
                          ".gitignore": "config.env\n"})
        files(self.home, {"research/lakes/q1/report.md": "r1", "research/lakes/q1/sources/a.json": "x" * 100,
                          "research/lakes/q2-graded-20261015-1253/report.md": "g",
                          "research/evals/20261014-2049/scores.md": "s",
                          "research/rescreen/q-graded-1225/graded.jsonl": "{}\n",
                          "research/heldout/cases.json": "KEYS",
                          "evals/runs/r1/answer.md": "a", "evals/overnight.log": "log",
                          "screen_graded.md": "prompt", "unrelated.txt": "keep"})
        files(self.repo, {"experiments/variants/v1/summary.md": "v"})

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args):
        return u.main(list(args), home=self.home, repo=self.repo)

    def test_plan_changes_nothing(self):
        before = sorted(str(p) for p in Path(self.tmp.name).rglob("*"))
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual(sorted(str(p) for p in Path(self.tmp.name).rglob("*")), before)
        self.assertIn('"~/research/lakes"', (self.repo / "engine/lake/lake.py").read_text())

    def test_apply_moves_and_rewrites(self):
        self.assertEqual(self.run_tool("--apply"), 0)
        d = self.repo / "data"
        self.assertEqual((d / "lakes/q1/sources/a.json").read_text(), "x" * 100)
        self.assertTrue((d / "experiments/graded-lakes/q2-graded-20261015-1253/report.md").exists())
        self.assertFalse((d / "lakes/q2-graded-20261015-1253").exists())
        self.assertTrue((d / "lake-evals/20261014-2049/scores.md").exists())
        self.assertTrue((d / "experiments/rescreen/q-graded-1225/graded.jsonl").exists())
        self.assertTrue((d / "experiments/variants/v1/summary.md").exists())
        self.assertTrue((d / "agent-evals/runs/r1/answer.md").exists())
        self.assertTrue((d / "agent-evals/overnight.log").exists())
        self.assertTrue((d / "scratch/screen_graded.md").exists())
        # left alone
        self.assertEqual((self.home / "research/heldout/cases.json").read_text(), "KEYS")
        self.assertTrue((self.home / "unrelated.txt").exists())
        self.assertFalse((self.home / "evals").exists())
        self.assertFalse((self.home / "research/lakes").exists())
        self.assertFalse((self.repo / "experiments").exists())
        # code
        lake = (self.repo / "engine/lake/lake.py").read_text()
        self.assertNotIn("~/research", lake)
        self.assertIn('"lakes").expanduser()', lake)
        self.assertIn("data/agent-evals/runs", (self.repo / "README.md").read_text())
        self.assertIn("data/lakes/<slug>", (self.repo / "README.md").read_text())
        self.assertEqual((self.repo / "GOALS.md").read_text(), "old kit in ~/evals/\n")
        self.assertIn("~/research/heldout", (self.repo / "packs/deep-research/README.md").read_text())
        self.assertIn("# RUNS_DIR", (self.repo / "config.env").read_text())
        self.assertIn("data/", (self.repo / ".gitignore").read_text().splitlines())
        # idempotent
        self.assertEqual(self.run_tool("--apply"), 0)

    def test_new_defaults_resolve(self):
        self.run_tool("--apply")
        code = (self.repo / "engine/lake/lake.py").read_text()
        for env, want in [({}, self.repo / "data/lakes"),
                          ({"HARNESS_DATA": "/x/d"}, Path("/x/d/lakes")),
                          ({"HARNESS_DATA": "/x/d", "LAKE_ROOT": "/y"}, Path("/y"))]:
            g = {"__file__": str(self.repo / "engine/lake/lake.py")}
            old = dict(os.environ)
            try:
                for k in ("HARNESS_DATA", "LAKE_ROOT"):
                    os.environ.pop(k, None)
                os.environ.update(env)
                exec(compile(code, "lake.py", "exec"), g)
            finally:
                os.environ.clear(); os.environ.update(old)
            self.assertEqual(g["ROOT"], want, env)
        for sh in ("engine/evals/run_evals.sh", "engine/evals/overnight.sh"):
            env = {k: v for k, v in os.environ.items() if k not in ("HARNESS_DATA", "RUNS_DIR")}
            out = subprocess.run(["bash", str(self.repo / sh)], capture_output=True, text=True, env=env).stdout.strip()
            self.assertEqual(Path(out).resolve(), (self.repo / "data/agent-evals/runs").resolve(), sh)

    def test_paths_module_agrees(self):
        import paths
        self.assertEqual(paths.DATA.name, "data")
        self.assertEqual(paths.LAKES, paths.DATA / "lakes") if not os.environ.get("LAKE_ROOT") else None

    def test_unexpected_code_stops_everything(self):
        (self.repo / "engine/lake/eval.py").write_text("EVAL_ROOT = somewhere_else\n")
        self.assertEqual(self.run_tool("--apply"), 2)
        self.assertTrue((self.home / "research/lakes/q1").exists())
        self.assertIn('"~/research/lakes"', (self.repo / "engine/lake/lake.py").read_text())

    def test_refuses_clash(self):
        (self.repo / "data/lakes/q1").mkdir(parents=True)
        self.assertEqual(self.run_tool("--apply"), 2)            # stopped in planning
        self.assertTrue((self.home / "research/lakes/q1/report.md").exists())
        self.assertTrue((self.home / "research/lakes/q2-graded-20261015-1253").exists())   # nothing moved
        self.assertIn('"~/research/lakes"', (self.repo / "engine/lake/lake.py").read_text())  # code unchanged


if __name__ == "__main__":
    unittest.main()
