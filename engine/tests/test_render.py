"""Offline tests for engine/lake/render.py: a fake lake on disk, no model, no network.

    python3 -m unittest engine/tests/test_render.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import render  # noqa: E402


def write_lake(d, *, evidence, synthesis, selected=None, plan=None, premises=None):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    if premises is not None:
        (d / "premises.json").write_text(json.dumps(premises))
    (d / "plan.json").write_text(json.dumps(plan or {
        "question": "Which doublet detectors work best?",
        "scope": "droplet scRNA-seq",
        "subquestions": [{"id": "S1", "text": "Which benchmarks exist?"},
                         {"id": "S2", "text": "Which tools rank highest?"}]}))
    (d / "synthesis.json").write_text(json.dumps(synthesis))
    (d / "evidence.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evidence))
    (d / "selected.json").write_text(json.dumps(selected or {"docs": []}))
    return d


def ev(eid, key, authors, year, quote="q", etype="independent_comparison", **kw):
    base = dict(eid=eid, key=key, authors=authors, year=year, quote=quote, claim="c",
                evidence_type=etype, title=f"T {key}", doi=f"10.1/{key}", section="Abstract",
                match="exact", match_ratio=1.0)
    base.update(kw)
    return base


def pt(pid, eids, **kw):
    base = dict(id=pid, text=f"point {pid}", eids=eids, kind="finding",
                issues_initial=[], rounds=0, issues=[])
    base.update(kw)
    return base


EVID = [
    ev("E1", "MED:1", ["Xi NM", "Li JJ"], 2021, quote="DoubletFinder has <b>best</b> accuracy"),
    ev("E2", "MED:1", ["Xi NM", "Li JJ"], 2021, quote="cxds is fastest"),
    ev("E3", "MED:2", ["Bais AS"], 2020, etype="new_method"),
    ev("E4", "MED:3", ["Zhang Z", "Melzer ME", "Csuth TC"], 2024),
    ev("E5", "MED:4", ["Zhang Z", "Kim Y", "Lee J"], 2024, match="fuzzy", match_ratio=0.93),
]
SYN = {"subquestions": {
    "S2": {"points": [pt("S2.1", ["E3"]), pt("S2.2", ["E1", "E2", "E99"])], "gaps": ["no F1 values"]},
    "S1": {"points": [pt("S1.1", ["E4", "E5"]),
                      pt("S1.2", ["E1"], issues_initial=["rests only on papers citing another study"], rounds=1),
                      pt("S1.3", ["E1"], issues=["number 50 not in cited evidence"])],
           "gaps": []}}}
SEL = {"docs": [
    {"key": "MED:1", "pub_year": 2021, "authors": ["Xi NM", "Li JJ"], "journal": "Cell systems",
     "cited_by": 174, "evidence_type": "independent_comparison", "sids": ["S2"], "score": 4.5,
     "title": "Benchmarking doublet detection", "doi": "10.1/x"},
    {"key": "MED:9", "pub_year": 2019, "authors": ["McGinnis CS"], "title": "Never cited",
     "evidence_type": "new_method"}]}


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lake = write_lake(Path(self.tmp.name) / "lake", evidence=EVID, synthesis=SYN, selected=SEL)
        self.m = render.build(self.lake)
        self.html = render.render(self.m)

    def tearDown(self):
        self.tmp.cleanup()

    def test_labels(self):
        self.assertEqual(render.surname("Xi NM"), "Xi")
        self.assertEqual(render.surname("van der Berg AB"), "van der Berg")
        self.assertEqual(render.surname("Tabula Sapiens Consortium"), "Tabula Sapiens Consortium")
        p = self.m["papers"]
        self.assertEqual(p["MED:1"]["label"], "Xi & Li 2021")
        self.assertEqual(p["MED:2"]["label"], "Bais 2020")
        # two different 'Zhang et al. 2024' papers get a/b in order of first citation
        self.assertEqual(p["MED:3"]["label"], "Zhang et al. 2024a")
        self.assertEqual(p["MED:4"]["label"], "Zhang et al. 2024b")

    def test_sections_follow_plan_order_and_refs_number_by_first_citation(self):
        self.assertEqual([s[0] for s in self.m["sections"]], ["S1", "S2"])
        self.assertEqual(self.m["order"], ["MED:3", "MED:4", "MED:1", "MED:2"])
        self.assertLess(self.html.index('id="sec-S1"'), self.html.index('id="sec-S2"'))

    def test_every_cited_quote_is_shown_and_escaped(self):
        self.assertIn("DoubletFinder has &lt;b&gt;best&lt;/b&gt; accuracy", self.html)
        self.assertNotIn("<b>best</b>", self.html)
        self.assertIn("cxds is fastest", self.html)
        self.assertIn("fuzzy match (93%)", self.html)
        self.assertIn("verbatim match", self.html)

    def test_point_cites_each_paper_once(self):
        s22 = self.html.split('id="S2.2"')[1].split("</li>")[0]
        self.assertIn("(Xi &amp; Li 2021)", s22)
        self.assertIn("2 quotes", s22)

    def test_missing_evidence_is_flagged_not_dropped(self):
        s22 = self.html.split('id="S2.2"')[1].split('</details></li>')[0]
        self.assertIn("evidence missing", s22)
        self.assertIn("E99", s22)

    def test_check_history_visible(self):
        s12 = self.html.split('id="S1.2"')[1].split('</details></li>')[0]
        self.assertIn("rewritten after check", s12)
        self.assertIn("rests only on papers citing another study", s12)
        self.assertIn("now passes", s12)
        s13 = self.html.split('id="S1.3"')[1].split('</details></li>')[0]
        self.assertIn("unresolved check", s13)
        self.assertIn("number 50 not in cited evidence", s13)

    def test_evidence_type_shown_on_citation(self):
        s21 = self.html.split('id="S2.1"')[1].split('</details></li>')[0]
        self.assertIn("method paper (authors&#x27; own tool)", s21)

    def test_reference_list_and_uncited(self):
        self.assertIn('id="ref-1"', self.html)
        self.assertIn("Cell systems", self.html)
        self.assertIn("Selected but not cited (1)", self.html)
        self.assertIn("Never cited", self.html)
        self.assertIn("https://europepmc.org/article/MED/1", self.html)

    def test_gaps_rendered(self):
        self.assertIn("no F1 values", self.html)

    def test_summary_rendered_first_when_present(self):
        syn = dict(SYN, summary=[pt("X1", ["E1"])])
        lake = write_lake(Path(self.tmp.name) / "lake2", evidence=EVID, synthesis=syn)
        m = render.build(lake)
        self.assertEqual(m["sections"][0][0], "summary")

    def test_cli_on_lake_and_on_eval_run(self):
        self.assertEqual(render.main([str(self.lake)]), 0)
        self.assertTrue((self.lake / "report.html").exists())
        run = Path(self.tmp.name) / "run"
        write_lake(run / "caseA", evidence=EVID, synthesis=SYN)
        write_lake(run / "caseB", evidence=EVID, synthesis=SYN)
        (run / "scores.md").write_text("x")
        self.assertEqual(render.main([str(run)]), 0)
        self.assertTrue((run / "caseA" / "report.html").exists())
        self.assertTrue((run / "caseB" / "report.html").exists())

    def test_support_counts(self):
        # S1 cites MED:3, MED:4, MED:1 across 3 points; all independent comparisons
        self.assertEqual(render.support(self.m["sections"][0][2], self.m)[:2], (3, 3))
        self.assertIn("3 points from 3 papers (3 independent comparison)", self.html)
        # S2: 2 points from 2 papers, one of them the authors' own method paper
        self.assertIn("2 points from 2 papers (1 independent comparison, 1 method paper", self.html)
        # whole report: 5 points, 4 papers
        self.assertIn("5 points from 4 papers", self.html)

    def test_no_premises_file_no_banner(self):
        self.assertNotIn('class="banner"', self.html)
        self.assertNotIn("Premise check", self.html)

    def test_missing_premise_banner_and_section_warnings(self):
        prem = [{"pid": "P1", "name": "DoubletBench", "query": "DoubletBench AND Hargreaves", "hits": 0, "top": []}]
        lake = write_lake(Path(self.tmp.name) / "trap", evidence=EVID, synthesis=SYN, premises=prem)
        h = render.render(render.build(lake))
        self.assertIn('class="banner"', h)
        self.assertIn("“DoubletBench”", h)
        self.assertIn("search: DoubletBench AND Hargreaves", h)
        self.assertIn("not evidence about it", h)
        self.assertEqual(h.count('class="secwarn"'), 2)       # S1 and S2 both have points
        self.assertIn('<details class="premises" open>', h)  # open when something is missing
        self.assertLess(h.index('class="banner"'), h.index('id="sec-S1"'))

    def test_empty_section_gets_no_warning(self):
        prem = [{"pid": "P1", "name": "X", "query": "X", "hits": 0, "top": []}]
        syn = {"subquestions": {"S1": {"points": [], "gaps": ["No verified evidence was found"]},
                                "S2": {"points": [pt("S2.1", ["E1"])], "gaps": []}}}
        lake = write_lake(Path(self.tmp.name) / "trap2", evidence=EVID, synthesis=syn, premises=prem)
        h = render.render(render.build(lake))
        self.assertEqual(h.count('class="secwarn"'), 1)

    def test_found_premise_no_banner(self):
        prem = [{"pid": "P1", "name": "scDblFinder", "query": "scDblFinder", "hits": 3,
                 "top": [{"title": "Doublet identification with scDblFinder"}, "MED:123"]}]
        lake = write_lake(Path(self.tmp.name) / "ok", evidence=EVID, synthesis=SYN, premises=prem)
        h = render.render(render.build(lake))
        self.assertNotIn('class="banner"', h)
        self.assertNotIn('class="secwarn"', h)
        self.assertIn("found · 3 hits", h)
        self.assertIn("Doublet identification with scDblFinder", h)
        self.assertIn('<details class="premises">', h)       # collapsed when all found

    def test_out_of_scope_tag(self):
        sel = dict(SEL, docs=[dict(SEL["docs"][0], in_scope=False)] + SEL["docs"][1:])
        lake = write_lake(Path(self.tmp.name) / "oos", evidence=EVID, synthesis=SYN, selected=sel)
        h = render.render(render.build(lake))
        self.assertIn('class="tag t-oos"', h)
        self.assertNotIn('class="tag t-oos"', self.html)

    def test_stability_marks(self):
        lake = write_lake(Path(self.tmp.name) / "stab", evidence=EVID, synthesis=SYN)
        (lake / "stability.json").write_text(json.dumps({"S1.1": {"in": 5, "of": 5}, "S2.1": {"in": 1, "of": 5}}))
        h = render.render(render.build(lake))
        self.assertIn('class="flag ok" title="present in 5 of 5 sampled syntheses">in 5/5 samples', h)
        self.assertIn('class="flag warn" title="present in 1 of 5 sampled syntheses">in 1/5 samples', h)
        self.assertNotIn("samples</span>", self.html)

    def test_does_not_touch_stage_files(self):
        before = {p.name: p.read_bytes() for p in self.lake.iterdir()}
        render.render_lake(self.lake)
        for name, data in before.items():
            self.assertEqual((self.lake / name).read_bytes(), data, name)


if __name__ == "__main__":
    unittest.main()
