"""Protocol 0.9 checks in synth.py: scope of summary sentences, and the shared-author rule.
Offline: fake model, no network.

    python3 -m unittest engine/tests/test_synth_scope.py
"""
import json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import llm  # noqa: E402
import synth  # noqa: E402


def ev(eid, key, quote, et="primary_study", authors=("Smith A", "Lee B", "Park C"), source="this_paper"):
    return {"eid": eid, "key": key, "quote": quote, "claim": quote, "evidence_type": et, "source": source,
            "authors": list(authors), "year": 2020, "sid": "S1", "kind": "finding"}


EV = {e["eid"]: e for e in [
    ev("E1", "MED:1", "High concentrations of the cytokine expand suppressive myeloid cells.", et="review",
       authors=("Rev A", "Rev B")),
    ev("E2", "PPR:9", "After switching off the oncogene, tumour cells transiently released the cytokine, driving regression in mice.",
       authors=("Pre A", "Pre B", "Pre C")),
    ev("E3", "MED:3", "Patients with high serum levels had shorter survival.", authors=("Kim A", "Ito B", "Wong Z")),
    ev("E4", "MED:4", "Treated mice showed fewer myeloid cells.", authors=("Diaz A", "Roe B", "Wong Z")),     # same last author as E3
    ev("E5", "MED:5", "In knockout mice, recruitment was lost.", authors=("Kim A", "Ito B", "Ng Y")),        # shares 2 authors with E3
    ev("E6", "MED:6", "Blocking antibodies reduced infiltration in mice.", authors=("Fox A", "Ito B", "Hall X")),  # shares 1 with E3
]}
META = {"PPR:9": {"is_preprint": True}, "MED:1": {}, "MED:3": {}, "MED:4": {}, "MED:5": {}, "MED:6": {}}


def issues(text, eids, scope=True, question="What does the cytokine do in pancreatic cancer?"):
    return synth.check_point({"text": text, "eids": eids}, EV, (), question, META, scope)


class ScopeTest(unittest.TestCase):
    # the brief's acceptance test: a review-backed rule plus a preprint-only finding in one summary sentence
    def test_repro_preprint_clause_without_marker_is_flagged(self):
        got = issues("High concentrations expand suppressive myeloid cells, and transient releases promote regression.", ["E1", "E2"])
        self.assertTrue(any("preprint" in i and "E2" in i for i in got), got)

    def test_preprint_named_passes(self):
        got = issues("High concentrations expand suppressive myeloid cells; a preprint reports that transient release "
                     "drove regression in mice.", ["E1", "E2"])
        self.assertFalse([i for i in got if "preprint" in i], got)

    def test_single_study_must_be_named(self):
        self.assertTrue(any("single study" in i for i in issues("High serum levels mean shorter survival.", ["E3"])))
        single = lambda t: [i for i in issues(t, ["E3"]) if "single study" in i]
        self.assertFalse(single("Kim et al. reported that high serum levels went with shorter survival."))
        self.assertFalse(single("In one patient cohort, high serum levels went with shorter survival."))
        self.assertFalse(single("Kim 2020 found that high serum levels went with shorter survival."))
        self.assertFalse(single("Its authors report that high serum levels went with shorter survival."))   # test_lake end-to-end wording
        # a bare year is not a source name
        self.assertTrue(any("single study" in i for i in issues("In 2020, high serum levels meant shorter survival.", ["E3"])))

    def test_animal_only_evidence_must_say_so(self):
        got = issues("Blocking the cytokine reduces myeloid infiltration.", ["E4", "E6"])
        self.assertTrue(any("animal" in i for i in got), got)
        self.assertFalse([i for i in issues("In mouse models, blocking the cytokine reduces myeloid infiltration.", ["E4", "E6"])
                          if "animal" in i])
        self.assertFalse([i for i in issues("Blocking reduces infiltration.", ["E4", "E6"], question="What does it do in mice?")
                          if "animal" in i])

    def test_scope_rules_only_when_asked(self):
        self.assertFalse([i for i in issues("Transient releases promote regression.", ["E2"], scope=False)
                          if "preprint" in i or "single" in i or "animal" in i])


class SharedAuthorTest(unittest.TestCase):
    def test_same_last_author_as_separate_studies_is_flagged(self):
        got = issues("Kim et al. and another study by Diaz et al. found effects in patients and mice.", ["E3", "E4"], scope=False)
        self.assertTrue(any("share authors" in i for i in got), got)

    def test_two_shared_authors_count_as_one_group(self):
        got = issues("Kim et al. and a separate study by Kim et al. agreed.", ["E3", "E5"], scope=False)
        self.assertTrue(any("share authors" in i for i in got), got)

    def test_one_shared_middle_author_is_not_a_group(self):
        got = issues("Kim et al. and an independent group (Fox et al.) agreed.", ["E3", "E6"], scope=False)
        self.assertFalse([i for i in got if "share authors" in i], got)

    def test_contrasting_conditions_is_fine(self):
        # previously fired on 'while' with any shared surname
        got = issues("Serum levels tracked survival in patients, while treated mice showed fewer myeloid cells.", ["E3", "E4"], scope=False)
        self.assertFalse([i for i in got if "share authors" in i], got)


class ReviseLoopTest(unittest.TestCase):
    def test_summary_point_gets_one_rewrite_with_the_scope_problem(self):
        seen = []

        def fake(body, timeout):
            p = body["messages"][0]["content"]; seen.append(p)
            return {"message": {"content": json.dumps({"drop": False, "eids": ["E1", "E2"],
                    "text": "High concentrations expand suppressive myeloid cells; a preprint reports transient release "
                            "drove regression in mice."})}, "logprobs": []}
        old, llm._post = llm._post, fake
        try:
            with tempfile.TemporaryDirectory() as d:
                pts = [{"id": "Sum.1", "text": "High concentrations expand suppressive cells, and transient releases promote regression.",
                        "eids": ["E1", "E2"]}]
                st = synth.revise_points(Path(d), pts, EV, list(EV), "summary", (), "Q", meta=META, scope=True)
        finally:
            llm._post = old
        self.assertEqual(st["fixed"], 1)
        self.assertIn("preprint", seen[0])
        self.assertIn("a preprint", pts[0]["text"])
        self.assertEqual(pts[0]["issues"], [])


if __name__ == "__main__":
    unittest.main()
