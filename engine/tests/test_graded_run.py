"""Offline tests for engine/lake/graded_run.py: adapter and selection only (no model, no network).

    python3 -m unittest engine/tests/test_graded_run.py
"""
import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import graded_run as gr  # noqa: E402

EV = SimpleNamespace(TYPE_WEIGHT={"independent_comparison": 3.0, "review": 2.0, "primary_study": 1.5,
                                  "new_method": 1.0, "other": 0.5},
                     ALWAYS_KEEP={"independent_comparison", "review"}, K_PER_SUB=2, MAX_DOCS=20)


def rec(k, sids, cited=0):
    return {"key": k, "title": f"T{k}", "cited_by": cited, "hits": [{"qid": f"{s}-q1", "sid": s, "rank": 1} for s in sids]}


def g(k, grade, routes, et="primary_study", in_scope=True):
    return {"key": k, "relevance": grade, "routes_to": routes, "evidence_type": et, "in_scope": in_scope,
            "bearing": ["outcome"], "use_as": "evidence", "reason": "r", "query_kind": "topic"}


class GradedRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.case = Path(self.tmp.name) / "case"
        self.case.mkdir()
        recs = [rec("A", ["S1"]), rec("B", ["S1"]), rec("C", ["S1"]), rec("D", ["S2"], cited=500),
                rec("E", ["S1"]), rec("F", ["S2"]), rec("G", ["S2"]), rec("H", ["S2"]), rec("N", ["S1"])]
        (self.case / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        (self.case / "plan.json").write_text(json.dumps({"question": "Q", "subquestions": [{"id": "S1"}, {"id": "S2"}]}))
        orig = [{"sid": "S1", "key": "N", "relevant": None, "reason": "no abstract", "by": "rule"},
                {"sid": "S1", "key": "A", "relevant": True, "by": "llm"}]
        (self.case / "screen.jsonl").write_text("".join(json.dumps(r) + "\n" for r in orig))
        self.graded = [g("A", 3, ["S1", "S2"]),            # routed to both, incl. one that did not retrieve it
                       g("B", 1, ["S1"]),                  # below cut
                       g("C", 2, []),                      # relevant but fits no sub-question -> retrieving sid
                       g("D", 2, ["S2"]),                  # high citations, grade 2
                       g("E", 3, ["S1"], in_scope=False),  # out of scope, grade 3
                       g("F", 3, ["S2"], in_scope=False), g("G", 3, ["S2"], in_scope=False),
                       g("H", 3, ["S2"], in_scope=False)]  # third OOS in S2 -> over quota

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return gr.adapt(self.case, self.graded, cut=2)

    def test_adapter_explodes_routes_and_keeps_rule_rows(self):
        rows = self.rows()
        a = [r for r in rows if r["key"] == "A"]
        self.assertEqual(sorted(r["sid"] for r in a), ["S1", "S2"])
        self.assertTrue(all(r["relevant"] for r in a))
        self.assertTrue([r for r in a if r["sid"] == "S2"][0]["rerouted"])
        self.assertIn({"sid": "S1", "key": "N", "relevant": None, "reason": "no abstract", "by": "rule"}, rows)
        b = [r for r in rows if r["key"] == "B"]
        self.assertEqual(len(b), 1)
        self.assertFalse(b[0]["relevant"])
        c = [r for r in rows if r["key"] == "C"]
        self.assertEqual([r["sid"] for r in c], ["S1"])
        self.assertTrue(c[0]["relevant"])

    def test_cut_is_adjustable(self):
        rows = gr.adapt(self.case, self.graded, cut=3)
        self.assertFalse([r for r in rows if r["key"] == "D"][0]["relevant"])

    def lake(self):
        lake = Path(self.tmp.name) / "lake"
        lake.mkdir()
        for f in ("records.jsonl", "plan.json"):
            (lake / f).write_text((self.case / f).read_text())
        (lake / "screen.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.rows()))
        return lake

    def test_select_uses_grade_and_keeps_out_of_scope_in_quota(self):
        sel = gr.select(self.lake(), EV)
        docs = {d["key"]: d for d in sel["docs"]}
        self.assertNotIn("B", docs)                         # below cut
        self.assertIn("E", docs)                            # out of scope, no longer dropped
        self.assertFalse(docs["E"]["in_scope"])
        oos_s2 = [d for d in sel["docs"] if "S2" in d["sids"] and d["in_scope"] is False]
        self.assertEqual(len(oos_s2), gr.OOS_PER_SUB)       # quota holds
        self.assertEqual(sorted(docs["A"]["sids"]), ["S1", "S2"])
        self.assertEqual(docs["A"]["grade"], 3)
        # grade counts: A (grade 3, 0 citations) outranks D (grade 2, 500 citations)
        self.assertGreater(docs["A"]["score"], docs["D"]["score"])
        self.assertEqual(sel["rule"]["source"], "graded_run.py")
        self.assertIn("grade", sel["rule"]["score"])

    def test_out_of_scope_does_not_use_in_scope_slots(self):
        sel = gr.select(self.lake(), EV)
        in_s1 = [d for d in sel["docs"] if "S1" in d["sids"] and d["in_scope"]]
        self.assertEqual(sorted(d["key"] for d in in_s1), ["A", "C"])   # K_PER_SUB=2, E took the OOS slot


if __name__ == "__main__":
    unittest.main()
