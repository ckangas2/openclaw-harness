"""Offline tests for the evidence lake: the LLM and the Europe PMC search are replaced by fakes.
run: python3 -m unittest engine/tests/test_lake.py"""
import json, os, re, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lake"))
import llm, lake, evidence, verify, synth, eval as lake_eval  # noqa: E402


def chat_reply(obj, bools=()):
    content = json.dumps(obj) if not isinstance(obj, str) else obj
    toks = re.findall(r'"[^"]*"|\w+|[^\s\w]|\s+', content)  # crude tokenizer, keeps spacing tokens
    lp = [{"token": t, "logprob": -0.1, "top_logprobs": [{"token": t, "logprob": -0.1}]} for t in toks]
    return {"message": {"content": content}, "logprobs": lp}


PLAN = {"scope": "droplet scRNA-seq", "subquestions": [
    {"id": "x", "text": "What do benchmarks report?", "why": "w", "queries": ["a AND b", "c"], "counter_query": "a AND limitation"},
    {"id": "y", "text": "Which defaults?", "why": "w", "queries": ["c", "d"], "counter_query": "d fail"},  # "c" duplicates
    {"id": "z", "text": "Any failures?", "why": "w", "queries": ["e", "f"], "counter_query": ""},
]}


def fake_search(query, out, max_results, sort=""):
    if "FakeBench" in query:
        Path(out).write_text(json.dumps({"query": query, "n": 0, "hit_count": 0, "records": []}))
        return True, "ok"
    recs = [{"rank": i, "id": f"{query[:1]}{i}", "source": "MED", "title": f"T {query} {i}",
             "abstract": "<p>Benchmark of methods &amp; results</p> " + "x" * 120, "pub_types": ["Journal Article"],
             "is_retracted": i == 3} for i in range(1, 5)]
    recs.append({"rank": 5, "id": "shared", "source": "MED", "title": "shared", "abstract": "y" * 150, "pub_types": []})
    # the same paper as a preprint record (different id, same DOI): must be merged
    recs.append({"rank": 6, "id": "PPR1", "source": "PPR", "doi": "10.1/x", "title": "A preprint later published in a journal",
                 "abstract": "z" * 150, "pub_types": ["Preprint"], "is_preprint": True})
    recs.append({"rank": 7, "id": "J1", "source": "MED", "doi": "10.1/X", "title": "A preprint later published in a journal",
                 "abstract": "z" * 150, "pub_types": ["Journal Article"], "is_preprint": False})
    hits = 5 if query.count(" AND ") >= 2 else 500   # long AND chains find little
    Path(out).write_text(json.dumps({"query": query, "n": len(recs), "hit_count": hits, "records": recs}))
    return True, "ok"


class LakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lake.ROOT = Path(self.tmp.name)
        lake.run_search = fake_search
        self.replies = []
        llm._post = lambda body, timeout: self.replies.pop(0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_collect_screen(self):
        d = lake.new_lake("Which doublet detection method performs best?")
        self.replies.append(chat_reply(PLAN))
        lake.stage_plan(d)
        plan = lake.read_json(d / "plan.json")
        self.assertEqual([s["id"] for s in plan["subquestions"]], ["S1", "S2", "S3"])
        s2 = [q["query"] for q in plan["subquestions"][1]["queries"]]
        self.assertEqual(s2, ["d", "d fail"])                      # duplicate "c" dropped
        self.assertEqual(plan["subquestions"][1]["queries"][-1]["qid"], "S2-c")
        self.assertNotIn("S3-c", [q["qid"] for q in plan["subquestions"][2]["queries"]])  # empty counter dropped

        lake.stage_collect(d)
        recs = lake.read_jsonl(d / "records.jsonl")
        shared = [r for r in recs if r["key"] == "MED:shared"][0]
        self.assertGreater(len(shared["hits"]), 3)                 # deduplicated across queries
        self.assertTrue(recs[0]["abstract"].startswith("Benchmark of methods & results"))
        dup = [r for r in recs if (r.get("doi") or "").lower() == "10.1/x"]
        self.assertEqual(len(dup), 1)                               # preprint + journal version merged
        self.assertFalse(dup[0]["is_preprint"])                     # journal version's metadata kept
        self.assertTrue(any(h["qid"].endswith("-cited") for h in shared["hits"]))  # citation-sorted pass ran

        # screening: answer every batch, the first one with a missing item to exercise the retry
        def responder(body, timeout):
            prompt = body["messages"][0]["content"]
            n = sum(1 for line in prompt.splitlines() if line.startswith("[") and "] " in line)
            items = [{"n": i, "evidence_type": "new_method", "in_scope": True, "relevant": i % 2 == 1, "reason": "true story"} for i in range(1, n + 1)]
            if not getattr(responder, "dropped", False) and n > 1:
                responder.dropped = True
                items = items[:-1]
            return chat_reply({"items": items}, [it["relevant"] for it in items])
        llm._post = responder
        lake.stage_screen(d)
        rows = lake.read_jsonl(d / "screen.jsonl")
        keys = [(r["sid"], r["key"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))                # each record screened once per sub-question
        self.assertTrue(any(r["reason"] == "retracted" and r["by"] == "rule" for r in rows))
        self.assertTrue(all(r["p"] is not None and r["p_type"] is not None for r in rows if r["by"] == "llm"))
        self.assertTrue(all(r["evidence_type"] == "new_method" for r in rows if r["by"] == "llm"))
        calls = lake.read_jsonl(d / "calls.jsonl")
        self.assertTrue(all(c["prompt_hash"] and c["model"] for c in calls))
        m = lake.read_json(d / "manifest.json")
        self.assertEqual(set(m["stages"]), {"plan", "collect", "screen"})

        # resume: a second run screens nothing new
        before = len(calls)
        lake.stage_screen(d)
        self.assertEqual(len(lake.read_jsonl(d / "calls.jsonl")), before)

    def test_find_quote(self):
        ps = {"s0p0": "Scrublet had the highest precision [12], whereas DoubletFinder \u2014 at default settings \u2014 had the best AUPRC.",
              "s0p1": "We used 16 benchmark datasets with experimentally annotated doublets."}
        self.assertEqual(verify.find_quote("whereas DoubletFinder - at default settings - had the best AUPRC", ps)[:2], ("s0p0", "exact"))
        self.assertEqual(verify.find_quote("We used 16 benchmark datasets with experimentally annotated doublets", ps, prefer="s0p0")[:2], ("s0p1", "exact"))
        self.assertEqual(verify.find_quote("We used sixteen benchmark datasets with experimentally annotated doublets", ps)[1], "fuzzy")
        self.assertEqual(verify.find_quote("DoubletFinder was the clear overall winner across every dataset", ps)[1], "missing")
        self.assertEqual(verify.find_quote("too short", ps)[1], "too_short")

    def test_select_fetch_extract(self):
        d = lake.new_lake("Which doublet detection method performs best?")
        self.replies.append(chat_reply(PLAN))
        lake.stage_plan(d)
        lake.stage_collect(d)
        llm._post = lambda body, timeout: chat_reply({"items": [
            {"n": i, "evidence_type": "independent_comparison" if i == 1 else "new_method", "relevant": True, "reason": "r"}
            for i in range(1, 9)]})
        lake.stage_screen(d)
        recs = lake.read_jsonl(d / "records.jsonl")
        for r in recs:
            r["pmcid"] = "PMC1" if r["key"] == "MED:shared" else None
        (d / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        evidence.stage_select(d)
        sel = lake.read_json(d / "selected.json")
        self.assertLessEqual(len(sel["docs"]), evidence.MAX_DOCS)
        def fake_fetch(pmcid, out):
            Path(out).write_text(json.dumps({"pmcid": pmcid, "status": "success", "sections": [
                {"title": "Abstract", "paragraphs": ["We benchmarked nine doublet detection methods on sixteen datasets."]},
                {"title": "Results", "paragraphs": ["DoubletFinder had the best detection accuracy across the datasets we tested here.",
                                                    "Computational efficiency varied widely between the methods we compared."]}],
                "tables": [], "figures": []}))
            return True, "ok"
        evidence.run_fetch = fake_fetch
        evidence.embed = lambda texts: (_ for _ in ()).throw(RuntimeError("no ollama"))  # forces keyword fallback
        evidence.stage_fetch(d)
        sel = lake.read_json(d / "selected.json")
        self.assertIn("fulltext", {x["text_source"] for x in sel["docs"]})
        def extractor(body, timeout):
            prompt = body["messages"][0]["content"]
            if "DoubletFinder had the best" in prompt:
                return chat_reply({"claims": [
                    {"claim": "DF best", "quote": "DoubletFinder had the best detection accuracy across the datasets we tested here.", "passage": "s1p0", "kind": "finding"},
                    {"claim": "made up", "quote": "Scrublet failed on every dataset that contained heterotypic doublets.", "passage": "s1p1", "kind": "finding"}]})
            return chat_reply({"claims": []})
        llm._post = extractor
        evidence.stage_extract(d)
        ev = lake.read_jsonl(d / "evidence.jsonl")
        rj = lake.read_jsonl(d / "evidence_rejected.jsonl")
        self.assertTrue(ev and all(e["match"] == "exact" and e["eid"].startswith("E") for e in ev))
        self.assertTrue(rj and all(r["why"] == "missing" for r in rj))
        n_calls = len(lake.read_jsonl(d / "calls.jsonl"))
        evidence.stage_extract(d)  # resume: nothing redone
        self.assertEqual(len(lake.read_jsonl(d / "calls.jsonl")), n_calls)

        # synthesize → check → report: one point invents a number, the revise call fixes it
        good = ev[0]["eid"]
        def writer(body, timeout):
            prompt = body["messages"][0]["content"]
            if "failed an automatic check" in prompt:
                return chat_reply({"drop": False, "text": "Its authors report DoubletFinder had the best detection accuracy.", "eids": [good]})
            if "Write the summary" in prompt:
                return chat_reply({"points": [{"text": "Its authors report DoubletFinder had the best detection accuracy.", "eids": [good]}]})
            return chat_reply({"points": [{"text": "DoubletFinder had the best accuracy on 42 datasets.", "eids": [good], "kind": "finding"}],
                               "gaps": ["No data on CITE-seq."]})
        llm._post = writer
        synth.stage_synthesize(d)
        synth.stage_check(d)
        syn = lake.read_json(d / "synthesis.json")
        pts = [p for b in syn["subquestions"].values() for p in b["points"]]
        self.assertTrue(pts and all(p["issues_initial"] and not p["issues"] and p["rounds"] == 1 for p in pts))
        synth.stage_report(d)
        rep = (d / "report.md").read_text()
        self.assertIn("## References", rep)
        self.assertIn(good, rep)
        self.assertNotIn("42 datasets", rep)
        self.assertEqual(lake.read_json(d / "metrics.json")["points_unverified"], 0)

    def test_check_point(self):
        ev = {"E1": {"eid": "E1", "claim": "DoubletFinder had the best AUPRC (0.80) in 16 datasets", "quote": "x" * 30,
                     "evidence_type": "independent_comparison", "source": "this_paper", "key": "a"},
              "E2": {"eid": "E2", "claim": "The authors report Chord outperforms others", "quote": "y" * 30,
                     "evidence_type": "new_method", "source": "this_paper", "key": "b"},
              "E3": {"eid": "E3", "claim": "Xi and Li found DoubletFinder best", "quote": "z" * 30,
                     "evidence_type": "review", "source": "cited_work", "key": "c"}}
        ok = synth.check_point({"text": "An independent benchmark of 16 datasets found DoubletFinder best (AUPRC 0.8) in 2021.", "eids": ["E1"]}, ev)
        self.assertEqual(ok, [])
        self.assertTrue(any("numbers" in i for i in synth.check_point({"text": "AUPRC was 0.91.", "eids": ["E1"]}, ev)))
        self.assertTrue(any("independent" in i for i in synth.check_point({"text": "Benchmarks show Chord is best.", "eids": ["E2"]}, ev)))
        self.assertTrue(any("not independent comparisons" in i for i in synth.check_point(
            {"text": "Independent benchmarks found DoubletFinder best and Chord strong.", "eids": ["E1", "E2"]}, ev)))
        self.assertTrue(any("superiority" in i for i in synth.check_point({"text": "Chord is the most accurate tool.", "eids": ["E2"]}, ev)))
        self.assertEqual(synth.check_point({"text": "Its authors report Chord outperforms others.", "eids": ["E2"]}, ev), [])
        self.assertTrue(any("secondhand" in i for i in synth.check_point({"text": "DoubletFinder was best.", "eids": ["E3"]}, ev)))
        self.assertTrue(any("do not exist" in i for i in synth.check_point({"text": "x", "eids": ["E9"]}, ev)))
        self.assertEqual(synth.check_point({"text": "As reported by Ttoouli, Xi and Li found DoubletFinder best.", "eids": ["E3"]}, ev), [])
        ev["E4"] = {"eid": "E4", "claim": "scDblFinder had the highest AUPRC", "quote": "w" * 30, "evidence_type": "other",
                    "source": "this_paper", "key": "d", "authors": ["Xi NM", "Li JJ"]}
        ev["E1"]["authors"] = ["Xi NM", "Li JJ"]
        self.assertTrue(any("independent comparison" in i for i in synth.check_point({"text": "An independent study found scDblFinder had the highest AUPRC.", "eids": ["E4"]}, ev)))
        self.assertEqual(synth.check_point({"text": "Method authors argue Chord is strong, while independent benchmarks found DoubletFinder best.",
                                            "eids": ["E1", "E2"]}, ev), [])
        self.assertTrue(any("share authors" in i for i in synth.check_point(
            {"text": "DoubletFinder was best, while another study found scDblFinder had the highest AUPRC.", "eids": ["E1", "E4"]}, ev)))

    def test_premise_and_eval(self):
        d = lake.new_lake("What did FakeBench conclude?")
        plan = dict(PLAN, presupposes=[{"name": "FakeBench", "query": "\"FakeBench\""}])
        self.replies.append(chat_reply(plan))
        lake.stage_plan(d)
        lake.stage_collect(d)
        prem = lake.read_json(d / "premises.json")
        self.assertEqual(prem[0]["hits"], 0)
        self.assertIn("NOT FOUND", synth.premise_text(d))
        self.assertEqual(synth.check_point({"text": "No study called FakeBench was found in Europe PMC.", "eids": []}, {}, ["FakeBench"]), [])
        self.assertTrue(synth.check_point({"text": "FakeBench found 12 methods.", "eids": []}, {}, ["FakeBench"]))
        case = {"id": "t", "question": "What did FakeBench conclude?", "key_papers": [{"label": "x", "title": "t a and b 1"}],
                "forbidden": ["FakeBench"], "expect_premise_missing": ["FakeBench"]}
        r = lake_eval.score_case(case, d)
        self.assertEqual((r["key_found"], r["key_total"]), (1, 1))
        self.assertTrue(r["premise_flagged_missing"])
        c = lake_eval.compare(d, d)
        self.assertTrue(c["plan_identical"])
        self.assertEqual(c["records_jaccard"], 1.0)

    def test_force_clears_later_stages(self):
        d = lake.new_lake("q force")
        for n in ["plan.json", "records.jsonl", "screen.jsonl", "selected.json", "evidence.jsonl", "report.md"]:
            (d / n).write_text("{}")
        (d / "queries").mkdir()
        lake.invalidate(d, "screen")
        self.assertTrue((d / "plan.json").exists() and (d / "records.jsonl").exists() and (d / "queries").exists())
        self.assertFalse(any((d / n).exists() for n in ["screen.jsonl", "selected.json", "evidence.jsonl", "report.md"]))
        self.assertEqual(synth.check_point({"text": "Cells were treated as independent replicates, which inflates FDR.",
                                            "eids": ["E2"]}, {"E2": {"eid": "E2", "claim": "c", "quote": "q" * 30, "evidence_type": "new_method", "source": "this_paper", "key": "b"}}), [])
        avail = [{"eid": "E5", "claim": "FDR exceeding 50%", "quote": "x" * 30}, {"eid": "E6", "claim": "c", "quote": "y" * 30}]
        self.assertEqual(synth.number_hints({"text": "FDR above 50%.", "eids": ["E6"]}, avail), ["hint: 50 appears in E5"])

    def test_split_and_relax(self):
        self.assertEqual(lake.split_and('"single-cell" AND (pseudobulk OR "pseudo-bulk") AND "type I error"'),
                         ['"single-cell"', '(pseudobulk OR "pseudo-bulk")', '"type I error"'])
        self.assertEqual(lake.split_and('"a AND b" AND (c AND d)'), ['"a AND b"', '(c AND d)'])
        d = lake.new_lake("relax?")
        plan = {"scope": "x", "subquestions": [
            {"id": "x", "text": "t1", "why": "w", "queries": ["alpha AND beta AND gamma AND delta", "zeta"], "counter_query": "eta"},
            {"id": "y", "text": "t2", "why": "w", "queries": ["b1", "b2"], "counter_query": "b3"},
            {"id": "z", "text": "t3", "why": "w", "queries": ["c1", "c2"], "counter_query": "c3"}]}
        self.replies.append(chat_reply(plan))
        lake.stage_plan(d)
        lake.stage_collect(d)
        idx = lake.read_json(d / "queries" / "index.json")
        rel = [v for v in idx if v["base"] == "S1-q1"]
        self.assertIn("alpha AND beta", [v["query"] for v in rel])          # relaxed down to two groups
        self.assertTrue(any(v["qid"] == "S1-q1-r2-cited" for v in rel))     # relaxed variants also run by citations
        self.assertIn("S1-q1", lake.read_json(d / "manifest.json")["stages"]["collect"]["relaxed"])
        self.assertTrue(verify.numbers("17 000 cells") == verify.numbers("17,000 cells") == {"17000"})

    def test_chain(self):
        d = lake.new_lake("chain?")
        self.replies.append(chat_reply(PLAN))
        lake.stage_plan(d)
        lake.stage_collect(d)
        recs = lake.read_jsonl(d / "records.jsonl")
        seedkeys = [recs[0]["key"], recs[1]["key"]]
        lake.append_jsonl(d / "screen.jsonl", [{"sid": "S1", "key": k, "relevant": True, "in_scope": True,
                                                "evidence_type": "independent_comparison", "reason": "r", "by": "llm"} for k in seedkeys])
        lake.fetch_links = lambda src, pid, kind: [{"source": "MED", "id": "999", "title": "Zimmerman"}] + \
            ([{"source": "MED", "id": f"x{pid}", "title": "one-off"}] if kind == "references" else [])
        def chain_search(query, out, max_results, sort=""):
            ids = re.findall(r"EXT_ID:(\w+)", query)
            Path(out).write_text(json.dumps({"n": len(ids), "records": [{"rank": 1, "id": i, "source": "MED", "title": f"paper {i}",
                                                                         "abstract": "q" * 150, "pub_types": []} for i in ids]}))
            return True, "ok"
        lake.run_search = chain_search
        llm._post = lambda body, timeout: chat_reply({"items": [{"n": 1, "evidence_type": "independent_comparison", "in_scope": True,
                                                                 "relevant": True, "reason": "r"}]})
        lake.stage_chain(d)
        ch = lake.read_json(d / "chain.json")
        self.assertEqual(ch["added"], ["MED:999"])                     # linked to 2 seeds; one-offs not picked
        self.assertTrue(any(r["key"] == "MED:999" for r in lake.read_jsonl(d / "screen.jsonl")))
        self.assertEqual(synth.check_point({"text": "As reported by She et al., a benchmark found DoubletFinder best.", "eids": ["E9"]},
                                           {"E9": {"eid": "E9", "claim": "c", "quote": "q" * 30, "evidence_type": "new_method",
                                                   "source": "cited_work", "key": "z"}}), [])

    def test_claim_issues(self):
        q = "Similarly, cells identified as doublets in the ileum dataset co-expressed TPSAB1/TPSB2 and CD79A/MZB1."
        self.assertTrue(verify.claim_issues("These doublets were missed by DoubletFinder.", q))
        self.assertEqual(verify.claim_issues("Doublets in the ileum co-expressed mast-cell and plasma-cell markers.", q), [])
        q2 = "On the 16 real RNA-seq datasets, scDblFinder achieves the highest mean AUPRC."
        self.assertTrue(verify.claim_issues("Their new method, scDblFinder, had the highest AUPRC on 16 datasets.", q2, "", "independent_comparison"))
        self.assertEqual(verify.claim_issues("scDblFinder had the highest mean AUPRC on 16 real datasets.", q2, "", "independent_comparison"), [])
        self.assertTrue(any("numbers" in i for i in verify.claim_issues("scDblFinder had AUPRC 0.9 on 16 datasets.", q2)))
        self.assertEqual(synth.check_point({"text": "No figure for recovering 8,000 cells was found.", "eids": ["E1"]},
                                           {"E1": {"eid": "E1", "claim": "c", "quote": "x" * 30, "evidence_type": "independent_comparison", "source": "this_paper", "key": "a"}},
                                           (), "how many cells to load to recover about 8,000 cells?"), [])

    def test_field_probs_split_tokens(self):
        lp = [{"token": t, "logprob": -0.2231, "top_logprobs": []} for t in
              ['{"', 'items', '":', ' [{"', 'relevant', '":', ' true', ',', ' "reason":', ' "', 'false', ' alarm', '"}]}']]
        got = llm.field_probs(lp, ("relevant",))
        self.assertEqual([(g["token"], g["p"]) for g in got], [("true", 0.8)])

    def test_bad_json_retries_then_fails(self):
        d = lake.new_lake("q")
        self.replies += [chat_reply("not json"), chat_reply("still not")]
        with self.assertRaises(ValueError):
            llm.call(d, "plan", "plan.md", {"question": "q"}, lake.PLAN_SCHEMA)
        self.assertEqual(len(lake.read_jsonl(d / "calls.jsonl")), 2)


if __name__ == "__main__":
    unittest.main()
