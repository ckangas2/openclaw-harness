"""Offline tests for engine/lake/truthinator.py: grouping rule and ladder, no model, no network.

    python3 -m unittest engine/tests/test_truthinator.py
"""
import json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import truthinator as t  # noqa: E402

# the 1993 hyper-IgM pair (PMIDs 7679206, 8094231): two independent groups, published back to back
KORTHAUER = ["Korthäuer U", "Graf D", "Mages HW", "Brière F", "Padayachee M", "Malcolm S", "Ugazio AG",
             "Notarangelo LD", "Levinsky RJ", "Kroczek RA"]
DISANTO = ["DiSanto JP", "Bonnefoy JY", "Gauchat JF", "Fischer A", "de Saint Basile G"]


def ev(eid, key, et="primary_study", source="this_paper"):
    return {"eid": eid, "key": key, "evidence_type": et, "source": source, "authors": [], "year": 1993,
            "claim": "c", "quote": "q", "sid": "S1", "section": "Abstract", "match": "exact"}


META = {
    "MED:7679206": {"authors": KORTHAUER, "journal": "Nature", "first_pub_date": "1993-02-11", "pub_year": 1993},
    "MED:8094231": {"authors": DISANTO, "journal": "Nature", "first_pub_date": "1993-02-11", "pub_year": 1993},
    "MED:3": {"authors": ["Smith A", "Jones B", "Lee C", "Kroczek RA"], "pub_year": 1994},       # same PI as Korthäuer
    "MED:4": {"authors": ["Trainee T", "Notarangelo LD", "Postdoc P"], "pub_year": 1996},       # 1 shared author only
    "MED:5": {"authors": ["Trainee T", "Postdoc P", "Newpi Q"], "pub_year": 1998},             # shares 2 with MED:4
    "MED:6": {"authors": ["Rev R"], "evidence_type": "review", "pub_year": 2000},
    "MED:7": {"authors": ["Other O", "Lab L"], "pub_year": 1995, "is_retracted": True},
    "MED:8": {"authors": ["Third X", "Group Y"], "pub_year": 1997},
}
EV = {e["eid"]: e for e in [ev("E1", "MED:7679206"), ev("E2", "MED:8094231"), ev("E3", "MED:3"), ev("E4", "MED:4"),
                            ev("E5", "MED:5"), ev("E6", "MED:6", et="review"), ev("E7", "MED:7"),
                            ev("E8", "MED:8", source="cited_work"), ev("E9", "MED:8")]}


def prof(eids, need=3):
    return t.profile({"id": "P", "eids": eids}, EV, META, need)


class TruthinatorTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(t.norm("Korthäuer U"), "korthauer u")
        self.assertEqual(t.norm("de Saint Basile G"), "de saint basile g")
        self.assertEqual(t.norm("Kroczek RA"), "kroczek r")

    def test_cd40l_pair_is_two_groups_published_together(self):
        p = prof(["E1", "E2"])
        self.assertEqual((p["groups"], p["papers"], p["label"]), (2, 2, "2 groups"))
        self.assertFalse(p["meets"])
        self.assertTrue(any("published together" in f for f in p["flags"]))

    def test_same_last_author_is_one_group(self):
        p = prof(["E1", "E3"])
        self.assertEqual((p["groups"], p["label"]), (1, "1 group"))
        self.assertIn("same last author", p["group_list"][0]["why"][0])

    def test_one_shared_author_is_not_enough_two_is(self):
        self.assertEqual(prof(["E1", "E4"])["groups"], 2)         # only Notarangelo shared
        self.assertEqual(prof(["E4", "E5"])["groups"], 1)         # two shared authors

    def test_transitive_only_within_the_point(self):
        # E1~E4 share one author (not grouped); E4~E5 share two (grouped). E1 stays separate.
        p = prof(["E1", "E4", "E5"])
        self.assertEqual(p["groups"], 2)

    def test_three_groups_meet_standard(self):
        p = prof(["E1", "E2", "E9"])
        self.assertEqual((p["groups"], p["label"]), (3, "3+ groups"))
        self.assertTrue(p["meets"])
        self.assertEqual(prof(["E1", "E2", "E9"], need=4)["meets"], False)

    def test_reviews_and_secondhand_do_not_count(self):
        p = prof(["E6", "E8"])
        self.assertEqual((p["label"], p["papers"]), ("relayed only", 0))
        self.assertEqual(sorted(r["why"] for r in p["relayed"]), ["cites another study for this", "review"])
        # the same paper with one first-hand item counts
        self.assertEqual(prof(["E8", "E9"])["papers"], 1)

    def test_single_paper_and_retraction_flag(self):
        p = prof(["E7"])
        self.assertEqual(p["label"], "1 paper")
        self.assertIn("cites a RETRACTED paper", p["flags"])

    def test_cli_writes_outputs_and_badges_without_touching_lake(self):
        with tempfile.TemporaryDirectory() as d:
            lake = Path(d) / "lake"; lake.mkdir()
            (lake / "plan.json").write_text(json.dumps({"question": "Q", "subquestions": [{"id": "S1", "text": "s"}]}))
            (lake / "evidence.jsonl").write_text("".join(json.dumps(e) + "\n" for e in EV.values()))
            (lake / "selected.json").write_text(json.dumps({"docs": [{"key": k, "title": f"T {k}", "evidence_type": "primary_study",
                                                                        "authors": v["authors"][:3], "pub_year": v["pub_year"]} for k, v in META.items()]}))
            (lake / "records.jsonl").write_text("".join(json.dumps(dict(v, key=k)) + "\n" for k, v in META.items()))
            (lake / "synthesis.json").write_text(json.dumps({"subquestions": {"S1": {"points": [
                {"id": "S1.1", "text": "CD40L mutations cause HIGM1.", "eids": ["E1", "E2", "E9"]},
                {"id": "S1.2", "text": "One lab says so.", "eids": ["E1", "E3"]}], "gaps": []}}, "summary": []}))
            before = {f.name: f.read_bytes() for f in lake.iterdir()}
            out = Path(d) / "out"
            self.assertEqual(t.main([str(lake), "--out", str(out), "--no-model"]), 0)
            truth = json.loads((out / "truth.json").read_text())
            self.assertTrue(truth["S1.1"]["meets"])
            html = (out / "report.html").read_text()
            self.assertIn('class="flag ok"', html)
            self.assertIn("3+ groups", html)
            self.assertIn("1 group · 2 papers", html)
            self.assertIn("last author Kroczek RA", html)     # full author list, not the 3 kept in selected.json
            sheet = (out / "verify.md").read_text()
            self.assertIn("## S1.2 — 1 group", sheet)
            self.assertIn("grouped because", sheet)
            for name, data in before.items():
                self.assertEqual((lake / name).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
