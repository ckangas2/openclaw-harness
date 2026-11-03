"""Offline test for review.py: builds a synthetic lake with every edge case the page must show
(a key paper lost at selection, an UNVERIFIED point, a repaired claim, a rejected quote, a relevant
but unselected paper, HTML in a title) and checks the rendered page contains each one.
run: python3 -m unittest engine/tests/test_review.py"""
import json, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "lake"
sys.path.insert(0, str(HERE))
import review  # noqa: E402


def build(root: Path) -> Path:
    d = root / "how-x-y-abc123"
    (d / "queries").mkdir(parents=True)

    def wj(p, o):
        (d / p).write_text(json.dumps(o))

    def wl(p, rows):
        (d / p).write_text("".join(json.dumps(r) + "\n" for r in rows))

    wj("manifest.json", {"slug": "how-x-y-abc123", "protocol": "lake-0.8", "model": "qwen-q8:latest",
                         "stages": {"collect": {"failed": [{"qid": "S1-q1", "error": "boom"}], "relaxed": ["S1-q2"]}}})
    wj("plan.json", {"question": "Does X improve Y?", "scope": "droplet scRNA-seq",
                     "subquestions": [{"id": "S1", "text": "What do benchmarks report?", "why": "w",
                                       "queries": [{"qid": "S1-q1", "query": "a AND b", "kind": "main"}]}]})
    wj("premises.json", [{"pid": "P1", "name": "FakeBench", "query": '"FakeBench"', "hits": 0, "top": []}])
    wj("selected.json", {"rule": {"excluded_out_of_scope": ["MED:9"]},
                         "docs": [{"key": "MED:1", "pmcid": "PMC1", "title": "Benchmarking doublet methods",
                                   "pub_year": 2021, "cited_by": 903, "evidence_type": "independent_comparison",
                                   "sids": ["S1"], "text_source": "fulltext", "is_preprint": False,
                                   "authors": ["Xi NM", "Li JJ"], "doi": "10.1/a"}]})
    wl("records.jsonl", [{"key": "MED:1", "title": "Benchmarking doublet methods", "cited_by": 903},
                         {"key": "MED:2", "title": "A method paper about <i>things</i> & stuff", "cited_by": 3},
                         {"key": "MED:9", "title": "Out of scope paper", "cited_by": 1}])
    wl("screen.jsonl", [{"sid": "S1", "key": "MED:1", "relevant": True, "in_scope": True,
                         "evidence_type": "independent_comparison", "reason": "benchmarks nine methods", "by": "llm"},
                        {"sid": "S1", "key": "MED:2", "relevant": True, "in_scope": True,
                         "evidence_type": "new_method", "reason": "its own tool", "by": "llm"},
                        {"sid": "S1", "key": "MED:9", "relevant": True, "in_scope": False,
                         "evidence_type": "other", "reason": "different data", "by": "llm"}])
    wl("evidence.jsonl", [
        {"eid": "E1", "sid": "S1", "key": "MED:1", "claim": "DoubletFinder had the best accuracy",
         "quote": "DoubletFinder had the best detection accuracy across 16 datasets.", "kind": "finding",
         "source": "this_paper", "evidence_type": "independent_comparison",
         "title": "Benchmarking doublet methods", "section": "Results", "match": "exact"},
        {"eid": "E2", "sid": "S1", "key": "MED:1", "claim": "scDblFinder reached AUPRC 0.91 in 16 datasets",
         "quote": "On the 16 real datasets, scDblFinder achieves the highest mean AUPRC.", "kind": "finding",
         "source": "cited_work", "evidence_type": "independent_comparison",
         "title": "Benchmarking doublet methods", "section": "Results", "match": "fuzzy",
         "claim_repaired": True, "claim_original": "Their new method scDblFinder reached AUPRC 0.91"}])
    wl("evidence_rejected.jsonl", [{"sid": "S1", "key": "MED:2", "claim": "made up",
                                    "quote": "Scrublet failed everywhere.", "why": "missing", "pmcid": "PMC2"}])
    wj("synthesis.json", {"checked": "x", "subquestions": {"S1": {"points": [
        {"id": "S1.1", "text": "An independent benchmark found DoubletFinder best.", "eids": ["E1"],
         "issues_initial": [], "issues": [], "rounds": 0},
        {"id": "S1.2", "text": "AUPRC was 0.91.", "eids": ["E2"],
         "issues_initial": ["numbers not found in the cited evidence: 0.91"],
         "issues": ["numbers not found in the cited evidence: 0.91"], "rounds": 2},
        {"id": "S1.3", "text": "dropped one", "eids": [], "dropped": True,
         "issues_initial": ["cites no evidence"], "issues": [], "rounds": 1}],
        "gaps": ["No data on CITE-seq."], "coverage": {"docs_cited": 1, "independent_docs_cited": 1}}},
        "summary": [{"id": "Sum.1", "text": "DoubletFinder was best in one benchmark.", "eids": ["E1"],
                     "issues_initial": [], "issues": [], "rounds": 0}]})
    wj("metrics.json", {"evidence_kept": 2, "quotes_rejected": 1, "points": 4, "points_fixed": 0,
                        "points_unverified": 1, "references": 1, "independent_references": 1, "llm_calls": 42})
    wj("queries/index.json", [{"qid": "S1-q1", "base": "S1-q1", "sid": "S1", "query": "a AND b",
                               "sort": "relevance", "n": 5, "hit_count": 500},
                              {"qid": "S1-q1-cited", "base": "S1-q1", "sid": "S1", "query": "a AND b",
                               "sort": "CITED desc", "n": 5, "hit_count": 500}])
    wj("chain.json", {"seeds": ["MED:1"], "candidates": 40, "new": 10, "picked": [], "added": ["MED:3"]})
    (d / "report.md").write_text(
        "# Does X improve Y?\n\n*Evidence lake, protocol lake-0.8.*\n\n## Summary\n\n"
        "- DoubletFinder was best in one benchmark. [1] <sub>E1</sub>\n"
        "- AUPRC was 0.91. [1] <sub>E2</sub> **UNVERIFIED** (numbers not found)\n\n"
        "## References\n\n[1] Xi NM, Li JJ (2021). Benchmarking doublet methods. https://doi.org/10.1/a\n\n"
        "## How this was built\n\n| | |\n|---|---|\n| points | 4 |\n| llm calls | 42 |\n")
    (root / "scores.json").write_text(json.dumps([{"case": "d01-x", "lake": str(d), "wall_secs": 659.6,
        "keys": [{"label": "Xi & Li 2021", "found": True, "selected": True, "cited": True},
                 {"label": "Zhang 2024", "found": True, "selected": False, "cited": False},
                 {"label": "Missing 2020", "found": False, "selected": False, "cited": False}]}]))
    (root / "scores.md").write_text("| case | key cited |\n|---|---|\n| d01-x | 1 |\n")
    return d


class ReviewTest(unittest.TestCase):
    def test_renders_everything_a_reviewer_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "20261014-2049"
            root.mkdir()
            build(root)
            review.main([str(root)])
            h = (root / "review.html").read_text()
            for probe in ["Needs your eye", "Key papers", "found, not selected", "Premise check", "NOT FOUND",
                          "not in quote", "still:", "caught:", "Claim repairs", "Their new method",
                          "screened relevant but not selected", "Searches and chaining", "out of scope",
                          "No data on CITE-seq", "dropped", "0.91", "secondhand", "fuzzy"]:
                self.assertIn(probe, h, f"missing from page: {probe}")
            self.assertIn("&lt;i&gt;things&lt;/i&gt; &amp; stuff", h)     # titles escaped, not injected
            self.assertNotIn("<i>things</i>", h)
            self.assertEqual(h.count("<details"), h.count("</details>"))
            self.assertEqual(h.count("<section"), h.count("</section>"))
            self.assertIn('<table><thead><tr><th></th><th></th>', h.replace("\n", ""))  # markdown metrics table
            self.assertIn('href="#c1"', h)
            self.assertGreater(len(h), 6000)

    def test_single_lake_without_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = build(Path(tmp) / "x")
            out = Path(tmp) / "one.html"
            review.main([str(d), "-o", str(out)])
            h = out.read_text()
            self.assertIn("Does X improve Y?", h)
            self.assertNotIn("Key papers", h)      # no scores file, so no key-paper table


if __name__ == "__main__":
    unittest.main()
