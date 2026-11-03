"""Offline tests for the Truthinator v2: evidence profile (words checked in code) and GRADE-style rules.
Fake model; uses the lake's real llm.py.

    python3 -m unittest engine/tests/test_truthinator_grade.py
"""
import json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import truthinator as t  # noqa: E402
import test_truthinator as tt  # noqa: E402

RULES = t.load_rules()


def ep(eid, method, system="human_patients", direct="direct"):
    return {"eid": eid, "system": system, "method": method, "directness": direct,
            "system_words": "x", "method_words": "y", "notes": []}


def graded(eids, profs, pt_extra=None, stab=None):
    pt = dict({"id": "P", "eids": eids}, **(pt_extra or {}))
    prof = t.profile(pt, tt.EV, tt.META, 3)
    return t.grade(pt, prof, {p["eid"]: p for p in profs}, tt.EV, RULES, stab)


class GradeTest(unittest.TestCase):
    def test_cd40l_three_groups_genetics_and_knockout_is_high(self):
        g = graded(["E1", "E2", "E9"], [ep("E1", "human_genetic_variant"), ep("E2", "human_genetic_variant"),
                                        ep("E9", "genetic_knockout_or_knockdown", "animal_in_vivo", "adjacent")])
        self.assertEqual(g["grade"], "High")
        self.assertTrue(any("methods agree" in s for s in g["steps"]))
        self.assertTrue(any("human genetic" in s for s in g["steps"]))

    def test_the_1993_pair_alone_is_capped_below_high(self):
        g = graded(["E1", "E2"], [ep("E1", "human_genetic_variant"), ep("E2", "human_genetic_variant")])
        self.assertEqual(g["grade"], "Moderate")
        self.assertTrue(any("2 groups" in s for s in g["steps"]))
        self.assertTrue(any("capped" in s for s in g["steps"]))

    def test_single_correlational_paper_is_very_low(self):
        g = graded(["E7"], [ep("E7", "observational_correlation")])
        # start Low, one paper -2, retracted -3 -> floor
        self.assertEqual(g["grade"], "Very low")

    def test_indirect_only_and_disagreement_rate_down(self):
        base = [ep("E1", "drug_or_inhibitor", "animal_in_vivo", "adjacent"), ep("E2", "drug_or_inhibitor", "animal_in_vivo", "adjacent"),
                ep("E9", "drug_or_inhibitor", "animal_in_vivo", "adjacent")]
        g = graded(["E1", "E2", "E9"], base)
        self.assertEqual(g["grade"], "Moderate")            # High -1 indirect
        g2 = graded(["E1", "E2", "E9"], base, {"kind": "disagreement"})
        self.assertEqual(g2["grade"], "Low")
        g3 = graded(["E1", "E2", "E9"], base, stab={"in": 1, "of": 5})
        self.assertEqual(g3["grade"], "Low")

    def test_checker_flag_does_not_lower_the_evidence_grade(self):
        profs = [ep("E1", "human_genetic_variant"), ep("E2", "human_genetic_variant"),
                 ep("E9", "genetic_knockout_or_knockdown", "animal_in_vivo", "adjacent")]
        g = graded(["E1", "E2", "E9"], profs, {"issues": ["numbers not found in the cited evidence: 40%"]})
        self.assertEqual(g["grade"], "High")
        self.assertEqual(g["issues"], ["numbers not found in the cited evidence: 40%"])
        strict = dict(RULES, unverified_max="Low")                       # the cap still exists as an option
        prof = t.profile({"id": "P", "eids": ["E1", "E2", "E9"]}, tt.EV, tt.META, 3)
        g2 = t.grade({"id": "P", "eids": ["E1", "E2", "E9"], "issues": ["x"]}, prof, {p["eid"]: p for p in profs}, tt.EV, strict)
        self.assertEqual(g2["grade"], "Low")

    def test_relayed_only_is_very_low(self):
        self.assertEqual(graded(["E6", "E8"], [])["grade"], "Very low")

    def test_words_must_be_in_the_quote(self):
        e = {"eid": "E1", "claim": "CD40L-deficient patients lacked IgG", "quote": "Five boys with X-linked hyper-IgM carried mutations"}
        ok = t.check_profile({"system": "human_patients", "system_words": "boys", "method": "human_genetic_variant",
                              "method_words": "carried mutations", "directness": "direct"}, e)
        self.assertEqual((ok["system"], ok["method"], ok["notes"]), ("human_patients", "human_genetic_variant", []))
        bad = t.check_profile({"system": "animal_in_vivo", "system_words": "knockout mice", "method": "genetic_knockout_or_knockdown",
                               "method_words": "gene targeting", "directness": "adjacent"}, e)
        self.assertEqual((bad["system"], bad["method"], bad["directness"]), ("unclear", "descriptive_other", "unclear"))
        self.assertEqual(len(bad["notes"]), 2)

    def test_cli_with_fake_model(self):
        calls = []

        def fake(body, timeout):
            p = body["messages"][0]["content"]; calls.append(p)
            items = []
            for eid in ("E1", "E2", "E3", "E9"):
                if f"[{eid}]" in p:
                    items.append({"eid": eid, "system": "human_patients", "system_words": "q", "method": "human_genetic_variant",
                                  "method_words": "q", "directness": "direct"})
            return {"message": {"content": json.dumps({"items": items})}, "logprobs": []}
        old, llm._post = llm._post, fake
        try:
            with tempfile.TemporaryDirectory() as d:
                lake = Path(d) / "lake"; lake.mkdir()
                (lake / "plan.json").write_text(json.dumps({"question": "Do CD40L mutations cause HIGM?", "scope": "patients",
                                                            "subquestions": [{"id": "S1", "text": "s"}]}))
                (lake / "evidence.jsonl").write_text("".join(json.dumps(e) + "\n" for e in tt.EV.values()))
                (lake / "records.jsonl").write_text("".join(json.dumps(dict(v, key=k)) + "\n" for k, v in tt.META.items()))
                (lake / "selected.json").write_text(json.dumps({"docs": []}))
                (lake / "synthesis.json").write_text(json.dumps({"subquestions": {"S1": {"points": [
                    {"id": "S1.1", "text": "CD40L mutations cause HIGM1.", "eids": ["E1", "E2", "E9"]}], "gaps": []}}, "summary": []}))
                out = Path(d) / "out"
                self.assertEqual(t.main([str(lake), "--out", str(out)]), 0)
                truth = json.loads((out / "truth.json").read_text())
                self.assertEqual(truth["S1.1"]["grade"]["grade"], "High")   # 3 groups, human genetics, direct
                self.assertTrue((out / "evidence_profile.jsonl").exists())
                self.assertTrue((out / "grade_rules.toml").exists())
                self.assertIn("High · 3+ groups", (out / "report.html").read_text())
                self.assertIn("grade: start High", (out / "verify.md").read_text())
                self.assertFalse((lake / "calls.jsonl").exists())            # model calls logged in out/, not the lake
                n = len(calls)
                out2 = Path(d) / "out2"
                self.assertEqual(t.main([str(lake), "--out", str(out2), "--profile-from", str(out)]), 0)
                self.assertEqual(len(calls), n)                                # re-grade: no model calls
                self.assertEqual(json.loads((out2 / "truth.json").read_text())["S1.1"]["grade"]["grade"], "High")
                page = (out / "check.html").read_text()
                self.assertIn("Truthinator check", page)
                self.assertIn("(de Saint Basile lab)", page)
                self.assertIn("Rated up: people with a natural mutation", page)
                self.assertIn("Lab 1:", page)
                self.assertIn("About right", page)
                self.assertIn("not whether each claim is true", page)
        finally:
            llm._post = old


if __name__ == "__main__":
    unittest.main()
