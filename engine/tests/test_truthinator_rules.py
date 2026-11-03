"""Offline tests for the three rules added on 23 Oct, each from a real failure in the GM-CSF run.

    python3 -m unittest engine/tests/test_truthinator_rules.py
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import truthinator as t  # noqa: E402
import test_truthinator as tt  # noqa: E402

RULES = t.load_rules()


def ep(eid, method, system="human_patients", direct="direct"):
    return {"eid": eid, "system": system, "method": method, "directness": direct,
            "system_words": "x", "method_words": "y", "notes": []}


class SummaryCapTest(unittest.TestCase):
    """Incident: all three High grades in the GM-CSF report were summary points, each rated
    above every section point it summarised. A summary strung several single-lab clauses
    together, so the grader saw six labs where there were six separate one-lab findings."""

    def syn(self, summary_eids):
        return {"subquestions": {"S1": {"points": [{"id": "S1.1", "eids": ["E1"]},
                                                   {"id": "S1.2", "eids": ["E2"]}]}},
                "summary": [{"id": "Sum.1", "eids": summary_eids}]}

    def truth(self, s11, s12, sum1):
        return {"S1.1": {"grade": {"grade": s11, "steps": []}},
                "S1.2": {"grade": {"grade": s12, "steps": []}},
                "Sum.1": {"grade": {"grade": sum1, "steps": []}}}

    def test_summary_is_capped_at_its_best_part(self):
        truth = self.truth("Low", "Very low", "High")
        t.cap_summaries(self.syn(["E1", "E2"]), truth, RULES)
        self.assertEqual(truth["Sum.1"]["grade"]["grade"], "Low")
        self.assertTrue(any("S1.1" in s for s in truth["Sum.1"]["grade"]["steps"]))

    def test_a_summary_no_better_than_its_parts_is_left_alone(self):
        truth = self.truth("Moderate", "Low", "Low")
        t.cap_summaries(self.syn(["E1", "E2"]), truth, RULES)
        self.assertEqual(truth["Sum.1"]["grade"]["grade"], "Low")
        self.assertEqual(truth["Sum.1"]["grade"]["steps"], [])

    def test_a_summary_sharing_no_evidence_is_not_capped(self):
        truth = self.truth("Low", "Low", "High")
        t.cap_summaries(self.syn(["E9"]), truth, RULES)
        self.assertEqual(truth["Sum.1"]["grade"]["grade"], "High")


class MethodsAgreeTest(unittest.TestCase):
    """Incident: two points sat at Moderate on a single paper each, because three assays
    inside that one lab counted as three methods agreeing and cancelled the one-paper
    penalty that exists to catch exactly that."""

    def graded(self, eids, profs):
        pt = {"id": "P", "eids": eids}
        prof = t.profile(pt, tt.EV, tt.META, 3)
        return t.grade(pt, prof, {p["eid"]: p for p in profs}, tt.EV, RULES)

    def test_two_methods_from_one_group_earn_no_bonus(self):
        g = self.graded(["E4", "E5"], [ep("E4", "drug_or_inhibitor"),
                                       ep("E5", "genetic_knockout_or_knockdown")])
        self.assertFalse(any("methods agree" in s for s in g["steps"]))
        self.assertEqual(g["grade"], "Low")        # start High, -2 one group, no bonus

    def test_two_methods_from_two_groups_still_earn_it(self):
        g = self.graded(["E1", "E9"], [ep("E1", "drug_or_inhibitor"),
                                       ep("E9", "genetic_knockout_or_knockdown")])
        self.assertTrue(any("methods agree across 2 groups" in s for s in g["steps"]))


class GlobalGroupTest(unittest.TestCase):
    """Incident: the same two Hopkins trial papers counted as two groups in one point and
    one group in another, because a third paper bridging them happened to be cited only in
    the second. Whether two papers are one lab is a fact about the papers."""

    # shares two authors with Korthäuer and two with MED:4, so it bridges them
    META = dict(tt.META, **{"MED:9": {"authors": ["Mages HW", "Brière F", "Trainee T", "Postdoc P"],
                                      "pub_year": 1999}})

    def index(self):
        return t.group_index([{"key": k, "authors": v["authors"]} for k, v in sorted(self.META.items())])

    def test_bridged_papers_are_one_group_even_when_the_bridge_is_elsewhere(self):
        pt = {"id": "P", "eids": ["E1", "E4"]}
        self.assertEqual(t.profile(pt, tt.EV, self.META, 3)["groups"], 2)          # point-local
        p = t.profile(pt, tt.EV, self.META, 3, self.index())                       # review-wide
        self.assertEqual(p["groups"], 1)
        self.assertTrue(any("another paper" in w for w in p["group_list"][0]["why"]))

    def test_unrelated_papers_stay_apart_under_the_global_index(self):
        p = t.profile({"id": "P", "eids": ["E1", "E9"]}, tt.EV, self.META, 3, self.index())
        self.assertEqual(p["groups"], 2)


if __name__ == "__main__":
    unittest.main()
