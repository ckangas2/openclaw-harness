"""Offline tests for engine/lake/synth_variants.py with the lake's real synth/llm modules and a fake model.

    python3 -m unittest engine/tests/test_synth_variants.py
"""
import json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import llm  # noqa: E402
import synth_variants as sv  # noqa: E402

EVID = [
    {"eid": "E1", "sid": "S1", "key": "K1", "claim": "A raises B by 40%", "quote": "A raised B by 40%",
     "kind": "finding", "evidence_type": "primary_study", "authors": ["Xi N"], "year": 2020, "section": "Results",
     "match": "exact", "source": "this_paper", "title": "T1"},
    {"eid": "E2", "sid": "S1", "key": "K2", "claim": "A lowers B", "quote": "A lowered B in mice",
     "kind": "finding", "evidence_type": "primary_study", "authors": ["Li J"], "year": 2021, "section": "Results",
     "match": "exact", "source": "this_paper", "title": "T2"},
    {"eid": "E3", "sid": "S2", "key": "K3", "claim": "C needs A", "quote": "C required A",
     "kind": "finding", "evidence_type": "review", "authors": ["Wu K"], "year": 2019, "section": "Abstract",
     "match": "exact", "source": "this_paper", "title": "T3"},
]
SYN = {"subquestions": {
    "S1": {"points": [{"id": "S1.1", "text": "A raised B by 40%.", "eids": ["E1"], "kind": "finding"},
                      {"id": "S1.2", "text": "In mice A lowered B.", "eids": ["E2"], "kind": "finding"}], "gaps": []},
    "S2": {"points": [{"id": "S2.1", "text": "C required A.", "eids": ["E3"], "kind": "finding"}], "gaps": []}},
    "summary": [], "checked": "x"}
MANIFEST = {"slug": "lk", "protocol": "lake-0.8", "model": "fake", "options": {"temperature": 0, "seed": 42},
            "created": "2026-10-15T00:00:00", "stages": {"synthesize": {}, "check": {}, "report": {}, "extract": {"evidence": 3}}}


def make_lake(root):
    d = Path(root) / "lake"
    d.mkdir()
    (d / "plan.json").write_text(json.dumps({"question": "Does A affect B?", "subquestions": [
        {"id": "S1", "text": "Effect of A on B", "queries": []}, {"id": "S2", "text": "What needs A", "queries": []}]}))
    (d / "evidence.jsonl").write_text("".join(json.dumps(e) + "\n" for e in EVID))
    (d / "selected.json").write_text(json.dumps({"docs": [{"key": k, "title": f"T{k}", "evidence_type": "primary_study"} for k in ("K1", "K2", "K3")]}))
    (d / "manifest.json").write_text(json.dumps(MANIFEST))
    (d / "synthesis.json").write_text(json.dumps(SYN))
    (d / "report.md").write_text("ORIGINAL")
    return d


class FakeModel:
    """Answers each prompt type; synthesize output depends on the seed so samples differ."""
    def __init__(self):
        self.calls = []

    def __call__(self, body, timeout):
        p, opts = body["messages"][0]["content"], body["options"]
        self.calls.append((p[:60], dict(opts)))
        if "one section of an evidence review" in p:
            seed = opts["seed"]
            if "Effect of A on B" in p:
                pts = [{"text": "A raised B by 40%.", "eids": ["E1"], "kind": "finding"}]
                if seed % 2:   # odd seeds also report the mouse result
                    pts.append({"text": "In mice A lowered B.", "eids": ["E2"], "kind": "finding"})
                if seed == 3:  # a point only one sample makes, with an unsupported number -> revised
                    pts.append({"text": "A raised B by 90%.", "eids": ["E1", "E2"], "kind": "disagreement"})
            else:
                pts = [{"text": "C required A.", "eids": ["E3"], "kind": "finding"}]
            out = {"points": pts, "gaps": []}
        elif "failed an automatic check" in p:
            out = {"drop": True, "text": "", "eids": []}
        elif "summary of an evidence review" in p:
            out = {"points": [{"text": "A raised B by 40%.", "eids": ["E1"]}]}
        elif "competing interpretations" in p:
            out = {"hypotheses": [
                {"label": "A raises B", "statement": {"text": "A raises B.", "eids": ["E1"]},
                 "supporting": [{"text": "A raised B by 40%.", "eids": ["E1"]}],
                 "unexplained": [{"text": "In mice A lowered B.", "eids": ["E2"]}],
                 "would_distinguish": "Measure B after A in human cells."},
                {"label": "Species-dependent", "statement": {"text": "The effect differs by species.", "eids": ["E1", "E2"]},
                 "supporting": [{"text": "A raised B by 70%.", "eids": ["E9"]}],   # bad id + number -> dropped
                 "unexplained": [], "would_distinguish": "Same assay in mouse and human."}],
                "only_one_supported": False, "note": ""}
        else:
            raise AssertionError("unexpected prompt: " + p[:80])
        return {"message": {"content": json.dumps(out)}, "logprobs": []}


class VariantsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lake = make_lake(self.tmp.name)
        self.before = {f.name: f.read_bytes() for f in self.lake.iterdir()}
        self.fake = FakeModel()
        self._post, llm._post = llm._post, self.fake
        self.opts = dict(llm.OPTIONS)

    def tearDown(self):
        llm._post = self._post
        self.tmp.cleanup()

    def unchanged(self):
        for name, data in self.before.items():
            self.assertEqual((self.lake / name).read_bytes(), data, name)
        self.assertEqual(sorted(f.name for f in self.lake.iterdir()), sorted(self.before))

    def test_seeds(self):
        out = Path(self.tmp.name) / "out"
        self.assertEqual(sv.main(["seeds", str(self.lake), "--n", "4", "--temp", "0.7", "--out", str(out)]), 0)
        # only synthesize sampled; check/summary/revise at the lake's settings
        for head, opts in self.fake.calls:
            if "one section" in head:
                self.assertEqual(opts["temperature"], 0.7)
            else:
                self.assertEqual(opts["temperature"], self.opts["temperature"])
        self.assertEqual(llm.OPTIONS, self.opts)
        stab = json.loads((out / "reference" / "stability.json").read_text())
        self.assertEqual(stab["S1.1"], {"in": 4, "of": 4})
        self.assertEqual(stab["S1.2"], {"in": 2, "of": 4})   # seeds 1 and 3
        self.assertEqual(stab["S2.1"], {"in": 4, "of": 4})
        html = (out / "reference" / "report.html").read_text()
        self.assertIn("in 2/4 samples", html)
        for i in range(1, 5):
            self.assertTrue((out / f"seed{i}" / "report.html").exists())
            self.assertTrue((out / f"seed{i}" / "report.md").exists())
        self.assertIn("temperature 0.7", (out / "seed1" / "report.md").read_text())
        self.assertIn("| S1.2 | 2/4 |", (out / "summary.md").read_text())
        self.unchanged()

    def test_hypotheses(self):
        out = Path(self.tmp.name) / "out2"
        self.assertEqual(sv.main(["hypotheses", str(self.lake), "--out", str(out)]), 0)
        syn = json.loads((out / "hypotheses" / "synthesis.json").read_text())
        h1, h2 = syn["subquestions"]["H1"]["points"], syn["subquestions"]["H2"]["points"]
        self.assertEqual([p["kind"] for p in h1], ["statement", "finding", "unexplained"])
        self.assertFalse(any(p.get("issues") or p.get("dropped") for p in h1))
        bad = [p for p in h2 if p["id"] == "H2.s1"][0]
        self.assertTrue(bad["dropped"])                     # nonexistent eid -> check -> revise -> dropped
        self.assertIn("would distinguish", syn["subquestions"]["H1"]["gaps"][0].lower())
        html = (out / "hypotheses" / "report.html").read_text()
        self.assertIn("A raises B", html)
        self.assertIn("UNEXPLAINED", html.upper())
        md = (out / "summary.md").read_text()
        self.assertIn("1 dropped", md)
        self.unchanged()


if __name__ == "__main__":
    unittest.main()
