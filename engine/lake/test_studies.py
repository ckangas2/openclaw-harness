"""Both-ways tests for model-named studies. Stubs llm.call, so it needs no model.
Run from the repo root: python3 engine/lake/test_studies.py"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "engine/lake")
import synth, llm

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS" if ok else "FAIL"), "|", name, "|", got if not ok else "")

# --- stub the model: answers keyed by title ---
ANS = {
 "CheckMate 067 4-year": ["CheckMate 067"],
 "Long-term outcomes nivolumab": ["CheckMate 067"],      # name only in the abstract
 "KEYNOTE-006 5-year": ["KEYNOTE-006"],
 "Framingham lipids": ["Framingham Heart Study"],
 "Framingham BP": ["Framingham Heart Study"],
 "SARS entry 1": ["SARS-CoV-2"],                          # a topic, must be rejected
 "SARS entry 2": ["SARS-CoV-2"],
 "SARS entry 3": ["SARS-CoV-2"],
 "SARS entry 4": ["SARS-CoV-2"],
 "Invented": ["ORION-9"],                                 # not in its own text, must be rejected
 "Lowercase": ["the phase 3 trial"],                      # not a proper name
 "Generic caps": ["Phase 3 Trial"],                       # capitalised, still only design words
 "Wordy 006": ["Phase III KEYNOTE-006 Trial"],            # must key to the same study as C
}
def fake_call(lake, step, prompt_file, inputs, schema, **kw):
    return {"names": ANS.get(inputs["title"], [])}, None
llm.call = fake_call
synth.llm = llm

docs = [
 ("A", "CheckMate 067 4-year", "Results of the phase 3 CheckMate 067 trial."),
 ("B", "Long-term outcomes nivolumab", "In the phase III CheckMate 067 trial, 6.5-year OS."),
 ("C", "KEYNOTE-006 5-year", "KEYNOTE-006 randomised pembrolizumab versus ipilimumab."),
 ("D", "Framingham lipids", "Participants of the Framingham Heart Study offspring cohort."),
 ("E", "Framingham BP", "Data from the Framingham Heart Study were analysed."),
 ("F", "SARS entry 1", "SARS-CoV-2 uses ACE2."), ("G", "SARS entry 2", "SARS-CoV-2 spike."),
 ("H", "SARS entry 3", "SARS-CoV-2 TMPRSS2."), ("I", "SARS entry 4", "SARS-CoV-2 entry."),
 ("J", "Invented", "A trial of drug X registered as NCT01234567."),
 ("K", "Lowercase", "the phase 3 trial showed benefit."),
 ("L", "A review", "We review CheckMate 067."),
 ("M", "Generic caps", "A Phase 3 Trial of drug Y."),
 ("N", "Wordy 006", "Results of the Phase III KEYNOTE-006 Trial at 5 years."),
]
with tempfile.TemporaryDirectory() as td:
    lake = Path(td)
    (lake / "plan.json").write_text(json.dumps({"question": "What do SARS-CoV-2 studies show?"}))
    (lake / "selected.json").write_text(json.dumps({"docs": [
        {"key": k, "title": t, "evidence_type": "review" if k == "L" else "primary_study"} for k, t, _ in docs]}))
    (lake / "records.jsonl").write_text("".join(json.dumps({"key": k, "title": t, "abstract": a}) + "\n" for k, t, a in docs))
    info = synth.study_names(lake)
    rej = {k: [w for _, w in c["rejected"]] for k, c in info.items()}
    check("name found only in the abstract is kept", info["B"]["names"], ["checkmate 067"])
    check("topic in the question is rejected", sorted(set(rej["F"] + rej["G"])), ["part of the question"])
    check("a name absent from the paper is rejected", rej["J"], ["not in this paper's title or abstract"])
    check("a non-proper name is rejected", rej["K"], ["not a proper name"])
    check("capitalised design words alone are rejected", rej["M"], ["not a proper name"])
    check("registry id is still read by pattern", info["J"]["registry"], ["NCT01234567"])
    check("reviews are not asked", "L" in info, False)
    smap = synth.study_map(lake)
    check("two CheckMate 067 reports are one study", smap["A"] == smap["B"], True)
    check("CheckMate 067 and KEYNOTE-006 stay separate", smap["A"] == smap["C"], False)
    check("two Framingham papers are one study (non-oncology)", smap["D"] == smap["E"], True)
    check("'Phase III KEYNOTE-006 Trial' and 'KEYNOTE-006' are one study", smap["C"] == smap["N"], True)
    check("SARS-CoV-2 papers are NOT merged into one study", len({smap[k] for k in "FGHI"}), 4)
    n_before = len(json.loads((lake / "studies.json").read_text()))
    calls = []
    llm.call = lambda *a, **k: (calls.append(1), ({"names": []}, None))[1]
    synth.study_names(lake)
    check("second call reuses the cache, no model calls", len(calls), 0)

# --- the topic rule on its own: a name in most papers, NOT in the question ---
llm.call = lambda lake, step, pf, inputs, schema, **kw: ({"names": ["COVID-19"]}, None)
with tempfile.TemporaryDirectory() as td:
    lake = Path(td)
    (lake / "plan.json").write_text(json.dumps({"question": "How does the virus enter cells?"}))
    ks = "PQRS"
    (lake / "selected.json").write_text(json.dumps({"docs": [
        {"key": k, "title": f"Paper {k}", "evidence_type": "primary_study"} for k in ks]}))
    (lake / "records.jsonl").write_text("".join(
        json.dumps({"key": k, "title": f"Paper {k}", "abstract": "During COVID-19 we measured entry."}) + "\n" for k in ks))
    info = synth.study_names(lake)
    check("a name in every paper but not the question is rejected as the topic",
          sorted({w for c in info.values() for _, w in c["rejected"]}), ["names the topic, not a study"])
    check("so those papers stay separate studies", len(set(synth.study_map(lake).values())), 4)

# --- point-level checks, with the lake's names in place ---
synth._LAKE_NAMES.clear(); synth._LAKE_NAMES.update({"checkmate 067", "keynote 006", "keynote 002", "framingham heart"})
def ev(eid, quote, ctx="", tids=()):
    return {"eid": eid, "key": eid, "claim": "", "quote": quote, "context": ctx,
            "evidence_type": "primary_study", "source": "own", "trial_ids": list(tids)}
E = {"W": ev("W", "Median OS was 72.1 months in the combination group", tids=["checkmate 067"]),
     "G": ev("G", "No superiority in OS at this interim analysis",
             ctx="Table 2 / KEYNOTE-002 (phase II)/pembrolizumab | Advanced melanoma"),
     "C": ev("C", "The pooled overall survival HR was 0\N{MIDDLE DOT}72 (95% CI 0\N{MIDDLE DOT}65-0\N{MIDDLE DOT}79)"),
     "J": ev("J", "lectins enhance viral entry"),
     "F": ev("F", "Incidence was 12% over ten years", tids=["framingham heart"]),
     "X": ev("X", "Incidence was 12% over ten years")}
def flags(text, eids):
    return bool(synth.check_point({"text": text, "eids": eids}, E))
check("trial name from the paper's study passes", flags("In the CheckMate 067 trial, median OS was 72.1 months.", ["W"]), False)
check("hyphenated spelling passes", flags("In CheckMate-067, median OS was 72.1 months.", ["W"]), False)
check("wrong trial for a 002 cell is caught", flags("In the KEYNOTE-006 trial there was no OS superiority.", ["G"]), True)
check("right trial for a 002 cell passes", flags("In the KEYNOTE-002 trial there was no OS superiority.", ["G"]), False)
check("Lancet decimal passes", flags("The pooled HR was 0.72 (95% CI 0.65-0.79).", ["C"]), False)
check("invented number is caught", flags("The pooled HR was 0.75 (95% CI 0.65-0.79).", ["C"]), True)
check("leaked reference numbers are caught", flags("Jackson (refs 115, 117) report lectins enhance entry.", ["J"]), True)
check("non-oncology study, right paper, passes", flags("In the Framingham Heart Study, incidence was 12%.", ["F"]), False)
check("non-oncology study, wrong paper, is caught", flags("In the Framingham Heart Study, incidence was 12%.", ["X"]), True)
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
