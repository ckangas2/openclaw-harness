"""Stages synthesize → check → report: from verified evidence to a report whose every statement is checked.

synthesize  model, per sub-question: points (1-2 sentences each) that cite evidence ids, plus gaps
check       code: citations exist, numbers appear in the cited evidence, no independence claims resting on
            method papers' self-evaluation, nothing resting only on secondhand citations; flagged points go
            back to the model with the problem (at most 2 rounds), then are marked UNVERIFIED
report      code: markdown with numbered references, an evidence appendix (quotes), and metrics
"""
import json, re, sys, time
from pathlib import Path

import llm
from verify import numbers
from store import PROTOCOL, manifest_update, read_json, read_jsonl, write_json

MAX_ROUNDS = 2
TYPE_LABEL = {"independent_comparison": "independent comparison", "new_method": "method paper (own evaluation)",
              "review": "review", "primary_study": "primary study", "other": "other"}
POINTS_SCHEMA = {
    "type": "object", "required": ["points", "gaps"],
    "properties": {"points": {"type": "array", "minItems": 1, "maxItems": 7, "items": {
        "type": "object", "required": ["text", "eids", "kind"],
        "properties": {"text": {"type": "string"}, "eids": {"type": "array", "items": {"type": "string"}},
                       "kind": {"type": "string", "enum": ["finding", "disagreement", "limitation"]}}}},
        "gaps": {"type": "array", "items": {"type": "string"}}}}
REVISE_SCHEMA = {
    "type": "object", "required": ["drop", "text", "eids"],
    "properties": {"drop": {"type": "boolean"}, "text": {"type": "string"}, "eids": {"type": "array", "items": {"type": "string"}}}}


def card(e: dict, quote_chars=300) -> str:
    who = TYPE_LABEL.get(e["evidence_type"], "other") + (", citing another study" if e.get("source") == "cited_work" else "")
    first = (e.get("authors") or ["?"])[0].split()[0] if e.get("authors") else "?"
    out = f"[{e['eid']}] ({who}; {first} {e.get('year')}; {e.get('kind')}) {e['claim']}\n"
    out += f"    quote: \"{e['quote'][:quote_chars]}\""
    # The row a table cell came from. Stored since patch U and printed in the report, but the
    # synthesis never saw it: 27 Oct PD-1 run cited a KEYNOTE-002 cell (refractory patients,
    # chemotherapy comparator) as a KEYNOTE-006 result, and the row disproving it was printed
    # in the same report. A quote the model cannot place is a quote it will misattribute.
    if e.get("context"):
        out += f"\n    this quote is one cell of: {e['context'][:300]}"
    return out


# ── checks (pure code) ──────────────────────────────────────────────────────
# Claims of independent evidence, not the statistical sense ("cells treated as independent replicates").
INDEP_RE = re.compile(r"\b(independent(ly)?\s+(benchmark\w*|comparison\w*|evaluation\w*|assessment\w*|stud(y|ies)|analys[ie]s|test(s|ing)?|review\w*|group\w*)"
                      r"|benchmark(s|ing)?\s+(stud(y|ies)|show\w*|found|report\w*|indicate\w*|reveal\w*)|neutral\s+(benchmark|comparison)\w*|third[- ]party)\b", re.I)
SUPER_RE = re.compile(r"\b(best|outperform\w*|superior|most accurate|highest|top[- ]perform\w*)\b", re.I)
ATTRIB_RE = re.compile(r"\b(authors?|developers?|report\w*|claim\w*|according to|their own)\b", re.I)
# a point may mix independent and method-paper evidence if it names the method paper's claim as its authors'
METHOD_ATTRIB_RE = re.compile(r"\b(method (authors|developers|papers?)|(its|their) (own )?(authors|developers)|authors of (the )?[A-Za-z0-9-]+ (method|tool|paper)"
                              r"|developers of|[A-Z][a-z]+ et al\.? \(\d{4}\),? (the )?(authors|developers) of)\b", re.I)
SECONDHAND_RE = re.compile(r"\b(as reported (by|in)|reported by|cited (in|by)|citing|according to|as described (by|in)|referenc\w*)\b", re.I)
# Shared authors only matter when a sentence presents papers as separate studies or groups (incident: "another
# independent study" for the same group's protocol, d01). Contrasting conditions ("while at high dose ...") is fine.
SEPARATE_RE = re.compile(r"\b((another|separate|second|other|different)\s+(stud(y|ies)|groups?|benchmarks?|teams?|labs?|analys[ie]s|reports?)"
                         r"|two\s+(stud(y|ies)|groups|benchmarks|teams|labs)|independent(ly)?|disagree\w*|contradict\w*|in contrast)\b", re.I)
# Scope markers a summary sentence needs when its support is narrow (protocol 0.9).
PREPRINT_RE = re.compile(r"\b(pre-?prints?|not (yet )?peer[- ]reviewed|bio[Rr]xiv|med[Rr]xiv)\b", re.I)
ONE_SOURCE_RE = re.compile(r"\b(one|a single)\s+(stud(y|ies)|report|paper|preprint|trial|experiment|analysis|cohort|model)\b"
                           r"|\ba (study|report|paper|preprint|trial)\b|\bin (one|a single)\b"
                           r"|\b(reported|found|showed|observed|described)\s+(by|in)\b|\bet al\.?|\baccording to\b"
                           r"|\b(its|their|the|these|whose) (own )?(authors|investigators|researchers|developers)\b", re.I)
AUTHOR_YEAR_RE = re.compile(r"\b([A-Z][A-Za-z'\u00C0-\u017F-]+)(?: et al\.?| (?:and|&) [A-Z][A-Za-z'\u00C0-\u017F-]+)?,? \(?((?:19|20)\d{2})\)?")
NOT_NAMES = {"In", "By", "Since", "From", "Until", "As", "On", "At", "During", "Before", "After", "Of", "Through", "For", "And", "The"}


def names_a_source(text: str) -> bool:
    if ONE_SOURCE_RE.search(text):
        return True
    return any(m.group(1) not in NOT_NAMES for m in AUTHOR_YEAR_RE.finditer(text))
ANIMAL_RE = re.compile(r"\b(mice|mouse|murine|rats?|rodents?|animals?|preclinical(ly)?|in vivo models?|xenografts?|zebrafish|primates?|porcine|canine|hamsters?|rabbits?|syngeneic|orthotopic)\b", re.I)


def surnames(e: dict) -> set:
    return {a.split()[0].lower() for a in (e.get("authors") or []) if a and a != "Unknown"}


def _au(name: str) -> str:
    """'Korthäuer U' -> 'korthäuer u' (surname + first initial)."""
    parts = (name or "").lower().replace(".", " ").split()
    if len(parts) > 1 and parts[-1].isalpha() and len(parts[-1]) <= 3:
        return " ".join(parts[:-1]) + " " + parts[-1][0]
    return " ".join(parts)


def same_group(a: list, b: list):
    """Same group = same last author, or >= 2 shared authors (the Truthinator's rule). -> shared names or None."""
    na = [_au(x) for x in a or [] if x and x != "Unknown"]
    nb = [_au(x) for x in b or [] if x and x != "Unknown"]
    if not na or not nb:
        return None
    if na[-1] == nb[-1]:
        return [na[-1]]
    shared = sorted(set(na) & set(nb))
    return shared if len(shared) >= 2 else None


def scope_issues(p: dict, cited: list, meta: dict, question: str = "") -> list:
    """A summary sentence may not state as general what rests on narrow support (protocol 0.9; incident: a
    preprint's single-model finding written as a general rule in the GM-CSF summary, 15 Oct)."""
    text, out = p["text"], []
    papers = {}
    for e in cited:
        papers.setdefault(e["key"], e)
    pre = [k for k in papers if (meta.get(k) or {}).get("is_preprint") or str(k).startswith("PPR:")]
    if pre and not PREPRINT_RE.search(text):
        ids = ", ".join(e["eid"] for e in cited if e["key"] in pre)
        out.append(f"rests partly on a preprint ({ids}); say so in the sentence (e.g. 'a preprint reports ...') "
                   "or drop those evidence ids from this sentence")
    firsthand = {k: e for k, e in papers.items() if e.get("evidence_type") != "review" and e.get("source") != "cited_work"}
    if len(papers) == 1 and firsthand and not names_a_source(text):
        out.append("rests on a single study; name it or say 'one study' (e.g. 'X et al. reported ...') rather than stating it as general")
    # all() meant one clinical citation in an otherwise preclinical point disabled this check, which
    # is how the 27 Oct summary listed cyclophosphamide and HDAC inhibitors as strategies with no
    # preclinical label while citing Currier 2008 among the support. Any animal item now counts.
    animal = [e for e in cited if ANIMAL_RE.search(e.get("quote", "") + " " + e.get("claim", ""))]
    if animal and not ANIMAL_RE.search(text) and not ANIMAL_RE.search(question):
        if len(animal) == len(cited):
            out.append("every cited item is from animal or preclinical models; say so (e.g. 'in mouse models ...')")
        else:
            out.append(f"some cited items are from animal or preclinical models "
                       f"({', '.join(e['eid'] for e in animal)}) but the sentence states the finding without "
                       "qualification; say which part is preclinical (e.g. 'in mouse models ...'), or drop "
                       "those evidence ids from this sentence")
    return out




# ── referent: what a cited item is ABOUT ────────────────────────────────────
# evidence_profile.jsonl records subject_words copied from each item's claim or quote, with
# check_profile resetting the label to unclear when the words are not there. Nothing read it.
# 27 Oct, ACE2 run: the summary said anti-ACE2 antibodies "blocked SARS-CoV-2 spike protein
# binding, syncytia formation, and viral replication" citing E62/E65/E67 - Li 2003, which is
# SARS-CoV. Every quote was verbatim and the point passed on the first pass.
PROFILE = {}


def _nsub(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def names_term(text, phrase):
    """Whole-term occurrence. Boundary-aware so 'sars-cov' does not match in 'sars-cov-2'."""
    p = _nsub(phrase)
    return len(p) >= 3 and re.search(r"(?<![\w-])" + re.escape(p) + r"(?![\w-])", _nsub(text)) is not None


def subject_words_of(eid, profiles=None):
    w = ((profiles if profiles is not None else PROFILE).get(eid) or {}).get("subject_words") or []
    if isinstance(w, str):
        w = [w]
    return [x for x in (_nsub(y) for y in w) if len(x) >= 3]


def subject_issues(text, cited, profiles=None) -> list:
    """Cited items whose subject is absent from a sentence that names other items' subjects.

    Fires only on disagreement. If no cited subject appears the point is paraphrasing, and if
    all appear there is nothing to say - both stay silent, so this is quiet on ordinary prose.
    """
    present, absent = [], []
    for e in cited:
        subs = subject_words_of(e["eid"], profiles)
        if subs:
            (present if any(names_term(text, s) for s in subs) else absent).append((e["eid"], subs[0]))
    if present and absent:
        return ["cites evidence about a different subject than the sentence names ("
                + ", ".join(f"{eid} is about {s}" for eid, s in absent)
                + "); name that subject in the sentence, attribute it, or drop those evidence ids"]
    return []


def check_point(p: dict, ev: dict, missing_premises=(), question: str = "", meta=None, scope=False) -> list:
    issues = []
    eids = [x for x in p.get("eids", []) if isinstance(x, str)]
    cited = [ev[x] for x in eids if x in ev]
    unknown = [x for x in eids if x not in ev]
    if unknown:
        issues.append(f"cites evidence ids that do not exist: {', '.join(unknown)}")
    if not eids and any(n.lower() in p["text"].lower() for n in missing_premises) and not numbers(p["text"]):
        return issues  # stating that a premise was not found needs no evidence item
    if not cited:
        issues.append("cites no evidence")
        return issues
    # Context (the table row) is part of what the model was shown, so it is part of what the
    # citation supports. Without it a point naming KEYNOTE-006 fails on "006" - the trial name
    # is in the row, not the cell (27 Oct, PD-1 resynth).
    pool = " ".join(e["claim"] + " " + e["quote"] + " " + (e.get("context") or "") for e in cited)
    # Trial names are checked as names, not quantities. As quantities, "067" in CheckMate 067 failed
    # whenever the cited quote did not repeat the name, and the revise stripped correct trial names
    # (the largest share of number-check hits in the PD-1 call log, 28 Oct). As names they still catch the KEYNOTE-006-for-a-002-cell error.
    named = trial_ids(p["text"])
    known = trial_ids(pool) | {t for e in cited for t in e.get("trial_ids", ())}
    wrong = sorted(named - known)
    if wrong:
        issues.append(f"names a study that none of the cited items belong to: {', '.join(wrong)}")
    missing = sorted(numbers(mask_ids(p["text"])) - numbers(mask_ids(pool)) - numbers(question))  # restating the question is fine
    if missing:
        issues.append(f"numbers not found in the cited evidence: {', '.join(missing)}")
    types = {e["evidence_type"] for e in cited}
    secondhand = all(e.get("source") == "cited_work" for e in cited) and SECONDHAND_RE.search(p["text"])
    if types == {"new_method"} and not secondhand:  # an attributed report of someone else's study is not self-evaluation
        if INDEP_RE.search(p["text"]):
            issues.append("calls this independent/benchmark evidence, but every cited item is a method paper evaluating its own method")
        elif SUPER_RE.search(p["text"]) and not ATTRIB_RE.search(p["text"]):
            issues.append("states a method's superiority as fact, but it rests only on its authors' own evaluation; attribute it to them")
    elif INDEP_RE.search(p["text"]) and not types <= {"independent_comparison", "review"} and not METHOD_ATTRIB_RE.search(p["text"]) and not secondhand:
        others = sorted({f"{e['eid']} ({TYPE_LABEL.get(e['evidence_type'], 'other')})" for e in cited
                         if e["evidence_type"] not in ("independent_comparison", "review")})
        issues.append("calls this independent, but also cites items that are not independent comparisons: "
                      f"{', '.join(others)}; cite only independent comparisons here, or split the point and attribute the others")
    # all() meant one firsthand citation disabled this rule, and points cite 5-12 items. Summary
    # point 4 (27 Oct) cited five relayed review items plus two the reviews asserted themselves, so
    # nothing fired and the sentence carried no secondhand marker at all. Any relayed item counts.
    relayed = [e for e in cited if e.get("source") == "cited_work"]
    if relayed and not SECONDHAND_RE.search(p["text"]):
        if len(relayed) == len(cited):
            issues.append("rests only on papers citing another study; say it is secondhand (e.g. 'as reported by X, cited in Y')")
        else:
            issues.append(f"some cited items relay another study's finding ({', '.join(e['eid'] for e in relayed)}) "
                          "but the sentence does not say so; mark that part secondhand (e.g. 'as reported by X, "
                          "cited in Y') or drop those evidence ids from this sentence")
    docs = {}
    for e in cited:
        docs.setdefault(e["key"], e)
    if len(docs) > 1 and SEPARATE_RE.search(p["text"]):
        ds = list(docs.values())
        shared = set()
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                shared |= set(same_group(a.get("authors"), b.get("authors")) or [])
        if shared:
            issues.append(f"presents papers that share authors ({', '.join(sorted(shared))}) as separate studies; "
                          "present them as one group's work (e.g. an update of their own benchmark)")
    if scope:
        issues += scope_issues(p, cited, meta or {}, question)
    return issues


# ── synthesize ──────────────────────────────────────────────────────────────
def stage_synthesize(lake: Path, force=False):
    if (lake / "synthesis.json").exists() and not force:
        return
    plan = read_json(lake / "plan.json")
    evs = read_jsonl(lake / "evidence.jsonl")
    pos = {e["eid"]: i for i, e in enumerate(evs)}   # eids are content hashes, not counters
    order = {"independent_comparison": 0, "review": 1, "primary_study": 2, "new_method": 3, "other": 4}
    out = {"subquestions": {}}
    for s in plan["subquestions"]:
        mine = sorted([e for e in evs if e["sid"] == s["id"]],
                      key=lambda e: (order.get(e["evidence_type"], 9), pos.get(e["eid"], 1 << 30)))
        if not mine:
            out["subquestions"][s["id"]] = {"points": [], "gaps": ["No verified evidence was found for this sub-question."]}
            continue
        res, _ = llm.call(lake, f"synthesize:{s['id']}", "synthesize.md",
                          {"question": plan["question"], "subquestion": s["text"], "evidence": "\n".join(card(e) for e in mine)},
                          POINTS_SCHEMA)
        pts = [{"id": f"{s['id']}.{i}", "text": p["text"].strip(), "eids": p.get("eids", []), "kind": p.get("kind", "finding")}
               for i, p in enumerate(res.get("points", []), 1) if p.get("text", "").strip()]
        out["subquestions"][s["id"]] = {"points": pts, "gaps": [g for g in res.get("gaps", []) if g.strip()]}
        print(f"synthesize: {s['id']} {len(pts)} points", file=sys.stderr)
    write_json(lake / "synthesis.json", out)
    write_json(lake / "synthesis_raw.json", out)  # what the model wrote, before checks and revisions
    manifest_update(lake, "synthesize", {"points": sum(len(v["points"]) for v in out["subquestions"].values()),
                                         "prompt_hash": llm.prompt_hash("synthesize.md")})


# ── check + revise ──────────────────────────────────────────────────────────
def number_hints(p: dict, avail: list) -> list:
    """For numbers missing from the cited evidence, name the available items that do contain them (code, not model)."""
    cited = " ".join(e["claim"] + " " + e["quote"] for e in avail if e["eid"] in p.get("eids", []))
    out = []
    for n in sorted(numbers(p["text"]) - numbers(cited)):
        has = [e["eid"] for e in avail if n in numbers(e["claim"] + " " + e["quote"])]
        out.append(f"hint: {n} appears in {', '.join(has)}" if has else f"hint: {n} appears in none of the available evidence; remove it")
    return out


def revise_points(lake: Path, pts: list, ev: dict, pool_eids: list, label: str, missing_premises=(), question: str = "",
                  meta=None, scope=False) -> dict:
    stats = {"ok_first": 0, "fixed": 0, "dropped": 0, "unverified": 0}
    for p in pts:
        issues = check_point(p, ev, missing_premises, question, meta, scope)
        p["issues_initial"] = issues
        rounds = 0
        while issues and rounds < MAX_ROUNDS:
            rounds += 1
            avail = [ev[x] for x in dict.fromkeys(list(p.get("eids", [])) + pool_eids) if x in ev]
            res, _ = llm.call(lake, f"revise:{label}:{p['id']}", "revise.md",
                              {"point": p["text"], "cited": ", ".join(p.get("eids", [])) or "none",
                               "problems": "\n".join(f"- {i}" for i in issues + number_hints(p, avail)),
                               "evidence": "\n".join(card(e) for e in avail)},
                              REVISE_SCHEMA)
            if res.get("drop"):
                p["dropped"] = True
                issues = []
                break
            p["text"], p["eids"] = res.get("text", p["text"]).strip(), res.get("eids", p["eids"])
            issues = check_point(p, ev, missing_premises, question, meta, scope)
        p["rounds"] = rounds
        p["issues"] = issues
        if p.get("dropped"):
            stats["dropped"] += 1
        elif issues:
            stats["unverified"] += 1
        elif rounds:
            stats["fixed"] += 1
        else:
            stats["ok_first"] += 1
    return stats


SUMMARY_SCHEMA = {
    "type": "object", "required": ["points"],
    "properties": {"points": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
        "type": "object", "required": ["text", "eids"],
        "properties": {"text": {"type": "string"}, "eids": {"type": "array", "items": {"type": "string"}}}}}}}


# ── studies, not papers ─────────────────────────────────────────────────────
# One trial is reported many times. 27 Oct, PD-1 melanoma run: of 20 selected papers, five
# were CheckMate 067 and three were KEYNOTE-006 - 8 papers, 2 trials - and the report named
# the trial for neither of its two most prominent references. Every count that treats a paper
# as a source therefore overstates breadth.
# Measured over three eval runs (112 selected papers): grouping on registry ids and named
# trials found in a paper's OWN title and abstract collapses 8, every one verified correct.
# Restricted to primary studies on purpose: reviews and indirect comparisons cite every trial
# they discuss, and including them chained 13 unrelated papers into one group.
# Study identity. Registry ids have formats fixed by standards bodies, so a pattern for them works in
# any field. Trial and cohort NAMES do not: until 28 Oct this file carried a list of 14 oncology trial
# brands (CheckMate, KEYNOTE, ...), which made study grouping a silent no-op for any other question.
# Names now come from the model, one call per selected primary study, and code keeps a name only if
# it is verbatim in that paper's own title or abstract, is not part of the question, and is not so
# common across the lake that it names the topic rather than a study.
REG_RE = re.compile(r"\b(NCT\d{8}|ISRCTN\d{8}|ACTRN\d{14}|DRKS\d{8}|ChiCTR[-\w]{6,20}|"
                    r"EudraCT[\s:]*\d{4}-\d{6}-\d{2}|CTRI/\d{4}/\d{2,3}/\d{6})\b", re.I)
STUDY_SCHEMA = {"type": "object", "properties": {
    "names": {"type": "array", "items": {"type": "string"}, "maxItems": 4}}, "required": ["names"]}
_LAKE_NAMES = set()   # verified study names of the lake being checked; filled by stage_check


def name_key(s) -> str:
    """CheckMate 067, CheckMate-067 and CHECKMATE067-style spellings to one key: 'checkmate 067'."""
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# Study-design words, the same in every field. Used only to REJECT a "name" made of nothing else
# ("the phase 3 trial"), and to key "Phase III KEYNOTE-006 Trial" and "KEYNOTE-006" as one study.
# This is vocabulary about study design, not a list of studies, so it does not tie the lake to a field.
GENERIC = set("""the a an of and in for with on to phase i ii iii iv 1 2 3 4 trial trials study studies
cohort cohorts randomised randomized controlled clinical open label double single blind blinded
multicentre multicenter multicentric international pilot prospective retrospective observational
cross sectional case control extension follow up long term part arm arms registry""".split())


def study_key(name) -> str:
    """A study's identity key: its name without study-design words. '' if nothing else is left."""
    return " ".join(t for t in name_key(name).split() if t not in GENERIC)


def registry_ids(text) -> set:
    return {re.sub(r"[\s:]+", "", m.group(0)).upper() for m in REG_RE.finditer(text or "")}


def study_names(lake) -> dict:
    """key -> {"raw", "names", "rejected", "registry"} for every selected primary study, cached in
    studies.json. The model is asked once per paper; verification reruns on the cached answers every
    time, so tightening a rule never needs new calls."""
    import json as _json
    path = lake / "studies.json"
    cache = read_json(path, {}) or {}
    sel = (read_json(lake / "selected.json", {}) or {}).get("docs", [])
    recs = {r.get("key"): r for r in read_jsonl(lake / "records.jsonl")}
    plan = read_json(lake / "plan.json", {}) or {}
    text, title, abstract = {}, {}, {}
    for d in sel:
        r = recs.get(d.get("key"), {})
        title[d["key"]] = d.get("title") or r.get("title") or ""
        abstract[d["key"]] = r.get("abstract") or ""
        text[d["key"]] = f"{title[d['key']]} {abstract[d['key']]}"
    asked = 0
    for d in sel:
        k = d["key"]
        if d.get("evidence_type") != "primary_study" or k in cache:
            continue
        raw, err = [], None
        if text[k].strip():
            try:
                res, _ = llm.call(lake, f"study_name:{k}", "study_name.md",
                                  {"title": title[k], "abstract": abstract[k]}, STUDY_SCHEMA)
                raw = [str(x).strip() for x in (res or {}).get("names", []) if str(x).strip()]
            except Exception as ex:  # one bad reply must not stop the check stage
                err = str(ex)[:200]
            asked += 1
        cache[k] = {"raw": raw, "registry": sorted(registry_ids(text[k])), **({"error": err} if err else {})}
    q = f" {name_key(plan.get('question', ''))} "
    keyed = {k: f" {name_key(v)} " for k, v in text.items()}
    common = max(3, len(keyed) // 2)
    for k, c in cache.items():
        keep, rej = [], []
        for n in c.get("raw", []):
            nk = study_key(n)
            if not nk or not re.search(r"[a-z]", nk) or len(nk) > 60 or not re.search(r"[A-Z]", n):
                why = "not a proper name"
            elif f" {name_key(n)} " not in keyed.get(k, ""):
                why = "not in this paper's title or abstract"
            elif f" {nk} " in q:
                why = "part of the question"
            elif sum(1 for v in keyed.values() if f" {nk} " in v) > common:
                why = "names the topic, not a study"
            else:
                keep.append(nk)
                continue
            rej.append([n, why])
        c["names"], c["rejected"] = sorted(set(keep)), rej
    path.write_text(_json.dumps(cache, indent=1))
    if asked:
        print(f"studies: asked {asked}, named {sum(1 for c in cache.values() if c['names'])} of {len(cache)}",
              file=sys.stderr)
    return cache


def study_map(lake) -> dict:
    """key -> study id, for every selected paper. Two primary studies are one study when they share a
    registry id or a verified study name. Papers with neither are their own study."""
    info = study_names(lake)
    sel = (read_json(lake / "selected.json", {}) or {}).get("docs", [])
    ids = {k: set(c.get("registry", [])) | {"name:" + n for n in c.get("names", [])}
           for k, c in info.items() if c.get("registry") or c.get("names")}
    parent = {k: k for k in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by = {}
    for k, ss in ids.items():
        for i in ss:
            by.setdefault(i, []).append(k)
    for ks in by.values():
        for k in ks[1:]:
            parent[find(k)] = find(ks[0])
    return {d["key"]: (find(d["key"]) if d["key"] in parent else d["key"]) for d in sel}


def n_studies(keys, smap) -> int:
    return len({smap.get(k, k) for k in keys})


def trial_ids(text) -> set:
    """Registry ids in a text, plus any verified study name of the current lake that it mentions.
    Searching for the lake's own names, rather than a list of known brands, is what lets the
    wrong-study check work in any field."""
    out = registry_ids(text)
    k = f" {name_key(text)} "
    return out | {n for n in _LAKE_NAMES if f" {n} " in k}


def mask_ids(text) -> str:
    """Text with registry ids and the lake's study names removed, so their digits are not read as
    quantities ("067" in CheckMate 067)."""
    t = REG_RE.sub(" ", text or "")
    for n in sorted(_LAKE_NAMES, key=len, reverse=True):
        parts = [re.escape(x) for x in n.split()]
        t = re.sub(r"(?<![A-Za-z0-9])" + r"[\s\-_/:]*".join(parts) + r"(?![A-Za-z0-9])", " ", t, flags=re.I)
    return t


def study_ids(lake) -> dict:
    """key -> every registry id and verified name of the study that paper belongs to."""
    info, smap = study_names(lake), study_map(lake)
    by = {}
    for k, c in info.items():
        by.setdefault(smap.get(k, k), set()).update(set(c.get("registry", [])) | set(c.get("names", [])))
    return {k: by.get(smap.get(k, k), set()) for k in info}


def stage_check(lake: Path, force=False):
    syn = read_json(lake / "synthesis.json")
    if syn.get("checked") and not force:
        return
    syn = read_json(lake / "synthesis_raw.json") or syn  # a re-check starts from the model's own text
    plan = read_json(lake / "plan.json")
    ev = {e["eid"]: e for e in read_jsonl(lake / "evidence.jsonl")}
    PROFILE.clear()
    PROFILE.update({r["eid"]: r for r in read_jsonl(lake / "evidence_profile.jsonl")})
    smap = study_map(lake)
    tids = study_ids(lake)
    _LAKE_NAMES.clear()
    _LAKE_NAMES.update(n for c in study_names(lake).values() for n in c.get('names', []))
    for e in ev.values():
        e["trial_ids"] = sorted(tids.get(e["key"], ()))
    totals = {}
    for sid, block in syn["subquestions"].items():
        pool = [e for e, x in ev.items() if x["sid"] == sid]
        st = revise_points(lake, block["points"], ev, pool, sid, question=plan["question"])
        for k, v in st.items():
            totals[k] = totals.get(k, 0) + v
        # coverage, decided in code
        live = [p for p in block["points"] if not p.get("dropped")]
        docs = {ev[x]["key"] for p in live for x in p.get("eids", []) if x in ev}
        indep = {ev[x]["key"] for p in live for x in p.get("eids", []) if x in ev and ev[x]["evidence_type"] == "independent_comparison"}
        meth = {ev[x]["key"] for p in live for x in p.get("eids", []) if x in ev and ev[x]["evidence_type"] == "new_method"}
        prim = {ev[x]["key"] for p in live for x in p.get("eids", []) if x in ev and ev[x]["evidence_type"] == "primary_study"}
        # A review is a synthesis of other people's work whatever its `source` says, so a point
        # citing only reviews rests on no primary evidence. Counted and reported as a gap rather
        # than raised as a point issue: most points here cite only reviews, and turning that into a
        # revision demand would churn the whole summary without making it truer.
        review_only = sum(1 for p in live
                          if p.get("eids") and all(ev[x]["evidence_type"] == "review"
                                                   for x in p["eids"] if x in ev)
                          and any(x in ev for x in p["eids"]))
        block["coverage"] = {"docs_cited": len(docs), "studies_cited": n_studies(docs, smap),
                             "independent_docs_cited": len(indep), "method_docs_cited": len(meth), "primary_studies_cited": n_studies(prim, smap),
                             "points": len(live), "review_only_points": review_only}
        print(f"check: {sid} {st}", file=sys.stderr)
    # summary from the checked points only
    lines = [f"[{p['id']}] {p['text']} (evidence: {', '.join(p['eids'])})" + (" [UNVERIFIED]" if p.get("issues") else "")
             for b in syn["subquestions"].values() for p in b["points"] if not p.get("dropped")]
    res, _ = llm.call(lake, "summary", "summary.md", {"question": plan["question"], "points": "\n".join(lines),
                                                      "premises": premise_text(lake)}, SUMMARY_SCHEMA)
    summ = [{"id": f"Sum.{i}", "text": p["text"].strip(), "eids": p.get("eids", [])} for i, p in enumerate(res.get("points", []), 1)]
    used = list(dict.fromkeys(x for b in syn["subquestions"].values() for p in b["points"] if not p.get("dropped") for x in p["eids"]))
    # Only a genuine absence obliges summary.md to open by saying so. A misspelled name (near_miss)
    # or an untestable one (unclear) must not: on the 25 Oct T-VEC case that would have opened a
    # report about OPTiM by declaring OPTiM nonexistent.
    missing = [p["name"] for p in (read_json(lake / "premises.json", []) or [])
               if (p.get("verdict") or ("not_found" if not p.get("hits") else "found")) == "not_found"]
    sel = read_json(lake / "selected.json") or {}
    meta = {d["key"]: d for d in sel.get("docs", [])}
    st = revise_points(lake, summ, ev, used, "summary", missing, plan["question"], meta=meta, scope=True)
    syn["summary"] = summ
    # Same count L1b reports per section. The summary is built after that loop, so without this a
    # summary point resting entirely on reviews goes unremarked (Sum.4, 27 Oct: six review items).
    live_sum = [p for p in summ if not p.get("dropped")]
    syn["summary_coverage"] = {
        "points": len(live_sum),
        "review_only_points": sum(1 for p in live_sum
                                  if any(x in ev for x in p.get("eids", []))
                                  and all(ev[x]["evidence_type"] == "review"
                                          for x in p.get("eids", []) if x in ev))}
    syn["checked"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    syn["check_stats"] = {"subquestions": totals, "summary": st}
    write_json(lake / "synthesis.json", syn)
    manifest_update(lake, "check", {**syn["check_stats"], "prompt_hashes": {f: llm.prompt_hash(f) for f in ("revise.md", "summary.md")}})


def premise_text(lake: Path) -> str:
    ps = read_json(lake / "premises.json", []) or []
    if not ps:
        return "(the question names no specific paper, tool or dataset)"
    LABEL = {"found": "FOUND", "near_miss": "NAME MAY BE WRONG", "not_found": "NOT FOUND",
             "unclear": "NOT CHECKED"}
    out = []
    for p in ps:
        # lakes built before the identity check carry no verdict: fall back to the old rule
        v = p.get("verdict") or ("not_found" if not p.get("hits") else "found")
        line = f"- '{p['name']}': {LABEL.get(v, v)}"
        m = p.get("matched")
        if m and m.get("title"):
            line += f" — {m['title']} ({m.get('year')})"
        if p.get("why"):
            line += f"\n    {p['why']}"
        line += f"\n    search: {p['query']} — {p.get('hits')} hit(s)"
        if p.get("examined"):
            line += f", {p['examined']} examined"
        out.append(line)
    return "\n".join(out)


# ── report ──────────────────────────────────────────────────────────────────
def stage_report(lake: Path, force=False):
    m = read_json(lake / "manifest.json")
    plan = read_json(lake / "plan.json")
    syn = read_json(lake / "synthesis.json")
    ev = {e["eid"]: e for e in read_jsonl(lake / "evidence.jsonl")}
    epos = {k: i for i, k in enumerate(ev)}          # extraction order; eids are hashes
    sel = {d["key"]: d for d in read_json(lake / "selected.json")["docs"]}
    refs = {}

    def ref(key):
        if key not in refs:
            refs[key] = len(refs) + 1
        return refs[key]

    def render(p):
        if p.get("dropped"):
            return None
        nums = sorted({ref(ev[x]["key"]) for x in p.get("eids", []) if x in ev})
        cite = "".join(f"[{n}]" for n in nums)
        eids = ", ".join(x for x in p.get("eids", []) if x in ev)
        flag = f" **UNVERIFIED** ({'; '.join(p['issues'])})" if p.get("issues") else ""
        return f"- {p['text']} {cite} <sub>{eids}</sub>{flag}"

    L = [f"# {plan['question']}", "",
         f"*Evidence lake `{m['slug']}`, protocol {', '.join(sorted({v.get('protocol', m['protocol']) for v in m.get('stages', {}).values()} | {PROTOCOL}))}, model {m['model']} (temperature {m['options'].get('temperature')}), "
         f"built {m['created'][:10]}. Every statement cites evidence items whose quotes were found verbatim in the stored papers; "
         f"numbers were checked against the cited evidence.*", "", "## Summary", ""]
    L += [x for x in (render(p) for p in syn.get("summary", [])) if x]
    sc = syn.get("summary_coverage") or {}
    if sc.get("review_only_points"):
        L += ["", f"*{sc['review_only_points']} of {sc.get('points', '?')} summary points rest only on reviews, "
                  "so the underlying primary studies were not read directly.*"]
    if read_json(lake / "premises.json", []):
        L += ["", "## Premise check", "", "*What the question takes for granted, searched directly in Europe PMC (no model involved). "
              "FOUND means a returned title carries the name, or its authors and year match; it is not a "
              "judgement about the thing itself:*", "",
              premise_text(lake)]
    for s in plan["subquestions"]:
        b = syn["subquestions"].get(s["id"], {"points": [], "gaps": []})
        L += ["", f"## {s['id']}. {s['text']}", ""]
        L += [x for x in (render(p) for p in b["points"]) if x]
        cov = b.get("coverage", {})
        gaps = list(b.get("gaps", []))
        if cov.get("docs_cited", 0) < 2:
            gaps.append(f"These points rest on {cov.get('docs_cited', 0)} paper(s); no second independent source was found.")
        elif cov.get("studies_cited") and cov["studies_cited"] < cov.get("docs_cited", 0):
            gaps.append(f"These {cov['docs_cited']} papers report {cov['studies_cited']} distinct "
                        "study/studies; several are further reports of the same work, so they are "
                        "not independent confirmations of each other.")
        if cov.get("primary_studies_cited", 0) >= 2:
            L += ["", f"*Draws on {cov['primary_studies_cited']} distinct primary studies (further reports of one trial count once).*"]
        # "Independent comparison" is the methods-question sense: someone other than a tool's authors
        # evaluated it. It is a gap only where a method paper's own evaluation is cited. PD-1 S1 printed
        # it while citing two separate randomised trials (27 Oct).
        if cov.get("independent_docs_cited", 0) == 0 and cov.get("method_docs_cited", 0) > 0:
            gaps.append("No independent comparison is cited here; the method papers cited report their authors' own evaluations.")
        if cov.get("review_only_points"):
            gaps.append(f"{cov['review_only_points']} of {cov.get('points', '?')} points in this section rest only on "
                        "reviews, so the underlying primary studies were not read directly.")
        if gaps:
            L += ["", "*Gaps:*"] + [f"- {g}" for g in gaps]
    L += ["", "## References", ""]
    for key, n in sorted(refs.items(), key=lambda x: x[1]):
        d = sel.get(key, {})
        au = ", ".join(d.get("authors") or []) + (" et al." if len(d.get("authors") or []) >= 3 else "")
        link = f"https://doi.org/{d['doi']}" if d.get("doi") else d.get("url", "")
        L.append(f"[{n}] {au} ({d.get('pub_year')}). {d.get('title')} *{d.get('journal') or ''}*. {link} "
                 f"— {TYPE_LABEL.get(d.get('evidence_type'), 'other')}; {d.get('text_source')}"
                 + ("; no open-access full text" if d.get("fulltext_status") == "not_open_access" else "")
                 + ("; full text could not be fetched" if d.get("fulltext_status") == "fetch_failed" else "")
                 + ("; **preprint (not peer reviewed)**" if d.get("is_preprint") else "")
                 + ("; **RETRACTED**" if d.get("is_retracted") else ""))
    used = sorted({x for b in list(syn["subquestions"].values()) + [{"points": syn.get("summary", [])}] for p in b["points"]
                   if not p.get("dropped") for x in p.get("eids", []) if x in ev},
                  key=lambda x: epos.get(x, 1 << 30))
    L += ["", "## Evidence cited", ""]
    for x in used:
        e = ev[x]
        line = f"- **{x}** [{refs.get(e['key'], '?')}] {e['section']} ({e['match']}): \"{e['quote']}\""
        if e.get("context"):
            line += f"\n    - *in table row:* {e['context']}"
        L.append(line)
    # metrics, all computed from lake files
    st = m.get("stages", {})
    allp = [p for b in syn["subquestions"].values() for p in b["points"]] + syn.get("summary", [])
    metrics = {
        "queries": st.get("plan", {}).get("queries"), "unique_records": st.get("collect", {}).get("unique_records"),
        "screened_relevant": {k: v["relevant"] for k, v in st.get("screen", {}).get("per_subquestion", {}).items()},
        "docs_selected": len(sel), "fulltext": st.get("fetch", {}).get("fulltext"),
        "abstract_not_oa": len(st.get("fetch", {}).get("not_open_access", [])),
        "fetch_failed": len(st.get("fetch", {}).get("failed", [])),
        "evidence_kept": st.get("extract", {}).get("evidence"), "quotes_rejected": st.get("extract", {}).get("rejected"),
        "points": len(allp), "points_ok_first_pass": sum(1 for p in allp if not p.get("issues_initial")),
        "points_fixed": sum(1 for p in allp if p.get("issues_initial") and not p.get("issues") and not p.get("dropped")),
        "points_dropped": sum(1 for p in allp if p.get("dropped")), "points_unverified": sum(1 for p in allp if p.get("issues") and not p.get("dropped")),
        "references": len(refs), "independent_references": sum(1 for k in refs if sel.get(k, {}).get("evidence_type") == "independent_comparison"),
        "distinct_primary_studies": n_studies([k for k in refs if sel.get(k, {}).get("evidence_type") == "primary_study"], study_map(lake)),
        "llm_calls": len(read_jsonl(lake / "calls.jsonl")),
    }
    L += ["", "## How this was built", "", "| | |", "|---|---|"] + [f"| {k.replace('_', ' ')} | {v} |" for k, v in metrics.items()]
    (lake / "report.md").write_text("\n".join(L) + "\n")
    write_json(lake / "metrics.json", metrics)
    manifest_update(lake, "report", {"metrics": metrics})
    print(lake / "report.md")
