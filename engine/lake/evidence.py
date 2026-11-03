"""Stages select → fetch → extract: from screened records to verified evidence.

select   code: rank relevant records per sub-question (evidence type first, then citations), keep the top ones
fetch    code: open-access full text for the selected records (epmc_fetch.R); abstract otherwise
extract  code picks the passages most related to each sub-question (bge-m3 via Ollama, keyword overlap if
         unavailable); the model returns claims with verbatim quotes; code keeps only quotes found in the text
"""
import hashlib, json, math, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

import llm
import verify
from store import HERE, append_jsonl, manifest_update, read_json, read_jsonl, write_json

K_PER_SUB = int(os.environ.get("LAKE_K_PER_SUB", "5"))       # records kept per sub-question (beyond comparisons/reviews)
MAX_DOCS = int(os.environ.get("LAKE_MAX_DOCS", "20"))
CITE_W = float(os.environ.get("LAKE_CITE_W", "0.3"))   # weight on log1p(cited_by) in the select score
MIN_PRIMARY = int(os.environ.get("LAKE_MIN_PRIMARY", "0"))   # floor: slots reserved in the MAX_DOCS cut for top primary studies
BREADTH_W = float(os.environ.get("LAKE_BREADTH_W", "0.0"))   # weight on (number of sub-questions that screened it relevant - 1)
PASSAGES_PER_CALL = int(os.environ.get("LAKE_PASSAGES", "8"))
# One section could take every slot. Streby 2019 / S4 (27 Oct): five of eight went to consecutive
# Discussion paragraphs and the Results paragraph reporting the negative day-7 biopsies was never
# offered. Similarity to a sub-question favours prose that discusses the topic over prose that
# reports the measurements, so the cap is on breadth, not on rank. 0 = no cap (pre-27-Oct).
PASSAGES_PER_SECTION = int(os.environ.get("LAKE_PASSAGES_PER_SECTION", "2"))
PASSAGE_CHARS = 2000
EMBED_MODEL = os.environ.get("LAKE_EMBED_MODEL", "bge-m3")
TYPE_WEIGHT = {"independent_comparison": 3.0, "review": 2.0, "primary_study": 1.5, "new_method": 1.0, "other": 0.5}
ALWAYS_KEEP = {"independent_comparison", "review"}
# ALWAYS_KEEP bypassed K_PER_SUB with no ceiling of its own. In a review-dense field that starves
# the MAX_DOCS cut: ohsv-iv-delivery (25 Oct eval) had 96 relevant reviews for 20 slots, so all 20
# went to reviews and no primary study was reachable -- Ikeda 1999 (254 cites, relevant on 3
# sub-questions) was dropped "below max_docs in the final sort". 0 = unlimited = old behaviour.
ALWAYS_KEEP_PER_SUB = int(os.environ.get("LAKE_ALWAYS_KEEP_PER_SUB", "3"))   # 0 = unlimited (pre-27-Oct)


# ── select ──────────────────────────────────────────────────────────────────
def stage_select(lake: Path, force=False):
    if (lake / "selected.json").exists() and not force:
        return
    plan = read_json(lake / "plan.json")
    recs = {r["key"]: r for r in read_jsonl(lake / "records.jsonl")}
    allrows = read_jsonl(lake / "screen.jsonl")
    screen = [r for r in allrows if r.get("relevant") and r.get("in_scope", True)]

    # A primary study that several sub-questions independently screened as relevant is protected
    # the way reviews are. Otherwise it competes on citation count under K_PER_SUB and loses to
    # reviews. Incident (23-24 Oct, ohsv-iv-delivery): Ikeda 1999, 254 citations, screened
    # relevant on 3 sub-questions, dropped at select; selected set was 17 reviews of 20.
    _sq = {}
    for _r in screen:
        if _r.get("evidence_type") == "primary_study":   # NOT new_method: a tool paper's
            # self-comparison is its authors' claim, and protecting it let heavily-cited
            # tool papers bypass K_PER_SUB and win the MAX_DOCS cut (d02, 24 Oct: 14 of 19
            # selected docs were new_method; 2 key primary studies displaced)
            _sq.setdefault(_r["key"], set()).add(_r["sid"])
    multi_primary = {k for k, sids in _sq.items() if len(sids) >= 2}
    out_of_scope = sorted({r["key"] for r in allrows if r.get("relevant") and r.get("in_scope") is False})
    chosen = {}
    dropped = []
    # evidence_type is a property of the DOCUMENT, but screen.md asks for it per sub-question,
    # so the same paper can come back typed three ways. ALWAYS_KEEP fires on any row, which made
    # one inconsistent judgement a free pass: PPR594969 (2 citations, typed independent_comparison
    # / new_method / primary_study) entered unconditionally while Zimmerman 2021 (254 citations)
    # lost the max_docs cut. Resolve once per record: modal type, ties to the lower weight.
    import collections as _c
    _votes = _c.defaultdict(_c.Counter)
    for _r in screen:
        if _r.get("evidence_type"):
            _votes[_r["key"]][_r["evidence_type"]] += 1
    resolved_type = {}
    for _k, _cnt in _votes.items():
        _top = max(_cnt.values())
        resolved_type[_k] = min((t for t, n in _cnt.items() if n == _top),
                                key=lambda t: TYPE_WEIGHT.get(t, 0.5))
    for s in plan["subquestions"]:
        rows = [r for r in screen if r["sid"] == s["id"] and r["key"] in recs]
        scored = []
        for r in rows:
            rec = recs[r["key"]]
            et = resolved_type.get(r["key"], r.get("evidence_type", "other"))
            score = TYPE_WEIGHT.get(et, 0.5) + CITE_W * math.log1p(rec.get("cited_by") or 0)
            scored.append((round(score, 3), et, r, rec))
        scored.sort(key=lambda x: (-x[0], x[3]["key"]))
        n_other = n_always = 0
        for score, et, r, rec in scored:
            always = et in ALWAYS_KEEP
            if always and ALWAYS_KEEP_PER_SUB and n_always >= ALWAYS_KEEP_PER_SUB:
                dropped.append({"key": r["key"], "sid": s["id"], "evidence_type": et,
                                "score": round(score, 3), "cited_by": rec.get("cited_by"),
                                "why": f"always_keep quota reached ({ALWAYS_KEEP_PER_SUB}) for {s['id']}"})
                continue
            keep = always or r["key"] in multi_primary or n_other < K_PER_SUB
            if not keep:
                # A stage that discards records logs why it discarded each one. Screening does;
                # select did not, which cost an hour of guessing (24 Oct, d02).
                dropped.append({"key": r["key"], "sid": s["id"], "evidence_type": et,
                                "score": round(score, 3), "cited_by": rec.get("cited_by"),
                                "why": f"k_per_sub reached ({K_PER_SUB}) for {s['id']}"})
                continue
            if always:
                n_always += 1
            elif r["key"] not in multi_primary:
                n_other += 1
            c = chosen.setdefault(rec["key"], {"key": rec["key"], "pmcid": rec.get("pmcid"), "title": rec.get("title"),
                                               "pub_year": rec.get("pub_year"), "doi": rec.get("doi"), "url": rec.get("url"),
                                               "journal": rec.get("journal"), "authors": (rec.get("authors") or [])[:3],
                                               "cited_by": rec.get("cited_by"), "is_preprint": rec.get("is_preprint"),
                                               "is_retracted": rec.get("is_retracted"), "is_open_access": rec.get("is_open_access"),
                                               "evidence_type": et, "sids": [], "score": 0})
            c["sids"].append(s["id"])
            c["score"] = max(c["score"], score)
            if et in ALWAYS_KEEP:
                c["evidence_type"] = et
    # Breadth. The score is otherwise a pure property of the record (type + citations), identical
    # on every row, so the final sort carried no sub-question information and "sids" was recorded
    # but never read. A record three sub-questions independently called relevant is more central to
    # the question than one a single sub-question wanted. Default 0 leaves behaviour unchanged.
    if BREADTH_W:
        for c in chosen.values():
            c["score"] = round(c["score"] + BREADTH_W * (len(set(c["sids"])) - 1), 3)
    ranked = sorted(chosen.values(), key=lambda c: (-c["score"], c["key"]))
    docs = ranked[:MAX_DOCS]
    # Floor. multi_primary protection only covers K_PER_SUB; the binding cut for Ikeda 1999 was
    # the final MAX_DOCS sort, which it missed by 0.217 -- less than the 0.5 gap between review
    # (2.0) and primary_study (1.5) in TYPE_WEIGHT. Default 0 leaves behaviour unchanged.
    promoted = []
    if MIN_PRIMARY:
        have = sum(1 for c in docs if c["evidence_type"] == "primary_study")
        add = [c for c in ranked[MAX_DOCS:] if c["evidence_type"] == "primary_study"][:max(0, MIN_PRIMARY - have)]
        if add:
            docs = sorted(docs[:MAX_DOCS - len(add)] + add, key=lambda c: (-c["score"], c["key"]))
            promoted = [c["key"] for c in add]
    kept = {c["key"] for c in docs}
    for c in ranked:
        if c["key"] in kept:
            continue
        dropped.append({"key": c["key"], "sid": ",".join(c["sids"]), "evidence_type": c["evidence_type"],
                        "score": c["score"], "cited_by": c.get("cited_by"),
                        "why": f"below max_docs ({MAX_DOCS}) in the final sort"})
    write_json(lake / "selected.json", {"rule": {"type_weight": TYPE_WEIGHT, "always_keep": sorted(ALWAYS_KEEP),
                                                 "k_per_sub": K_PER_SUB, "max_docs": MAX_DOCS, "excluded_out_of_scope": out_of_scope,
                                                 "always_keep_per_sub": ALWAYS_KEEP_PER_SUB,
                                                 "protected_multi_primary": sorted(multi_primary),
                                                 "min_primary": MIN_PRIMARY, "breadth_w": BREADTH_W,
                                                 "promoted_primary": promoted,
                                                 "retyped": {k: v for k, v in resolved_type.items() if len(_votes[k]) > 1},
                                                 "dropped": dropped,
                                                 "score": f"type_weight + {CITE_W}*log1p(cited_by)" + (f" + {BREADTH_W}*(n_sids-1)" if BREADTH_W else "")}, "docs": docs})
    manifest_update(lake, "select", {"docs": len(docs), "by_type": {t: sum(1 for d in docs if d["evidence_type"] == t) for t in TYPE_WEIGHT}})


# ── fetch ───────────────────────────────────────────────────────────────────
FETCH_TRIES = int(os.environ.get("LAKE_FETCH_TRIES", "3"))   # 1 = the old single attempt
# Errors worth a second go. Anything else (not open access, no such record) is permanent and the
# paper is read as an abstract, which is what the old code did for every failure including these.
TRANSIENT_RE = re.compile(r"(\b5\d\d\b|timeout|timed out|connection|reset|temporarily|unavailable)", re.I)


def run_fetch(pmcid: str, out: Path) -> tuple[bool, str]:
    r = subprocess.run(["Rscript", str(HERE / "epmc_fetch.R"), pmcid, str(out)], capture_output=True, text=True, timeout=300)
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode == 0, lines[-1] if lines else ""


def fetch_with_retry(pmcid: str, out: Path, force: bool) -> tuple[dict, int]:
    """(stored data, attempts used). A cached success is never refetched; a cached FAILURE is,
    because the old code wrote the failure to disk and then skipped the path forever."""
    cached = read_json(out, {}) if out.exists() else {}
    if cached.get("status") == "success" and not force:
        return cached, 0
    data, last = cached, ""
    for attempt in range(1, FETCH_TRIES + 1):
        ok, msg = run_fetch(pmcid, out)
        data = read_json(out, {}) or {}
        if data.get("status") == "success":
            if attempt > 1:
                print(f"fetch: {pmcid} ok on attempt {attempt}", file=sys.stderr)
            return data, attempt
        last = str(data.get("error") or msg or "")
        if attempt < FETCH_TRIES and TRANSIENT_RE.search(last):
            time.sleep(2 ** attempt)
            continue
        break
    print(f"fetch: {pmcid} FAILED after {attempt} attempt(s): {last}", file=sys.stderr)
    return data, attempt


def stage_fetch(lake: Path, force=False):
    sel = read_json(lake / "selected.json")
    sdir = lake / "sources"
    sdir.mkdir(exist_ok=True)
    got, failed, not_oa = 0, [], []
    for d in sel["docs"]:
        pm = d.get("pmcid")
        if not pm:
            d["text_source"], d["fulltext_status"] = "abstract", "no_pmcid"
            not_oa.append({"pmcid": None, "key": d["key"], "why": "no PMC identifier"})
            continue
        # EPMC's fullTextXML answers 500 (not 404) for records outside the OA subset, so asking
        # costs three requests and two sleeps to learn what is_open_access already said. Not being
        # open access is not a failure: the paper stays selected and is read as an abstract.
        if d.get("is_open_access") is False:
            d["text_source"], d["fulltext_status"] = "abstract", "not_open_access"
            not_oa.append({"pmcid": pm, "key": d["key"], "why": "not in the EPMC open-access subset"})
            continue
        out = sdir / f"{pm}.json"
        data, tries = fetch_with_retry(pm, out, force)
        if data.get("status") == "success":
            d["text_source"], d["fulltext_status"] = "fulltext", "success"
            got += 1
        else:
            d["text_source"], d["fulltext_status"] = "abstract", "fetch_failed"
            failed.append({"pmcid": pm, "error": data.get("error", "no file"), "attempts": tries})
    write_json(lake / "selected.json", sel)
    manifest_update(lake, "fetch", {"fulltext": got, "abstract_only": len(sel["docs"]) - got,
                                    "failed": failed, "not_open_access": not_oa, "tries": FETCH_TRIES})


# ── passages ────────────────────────────────────────────────────────────────
def passages_for(lake: Path, doc: dict, rec: dict) -> list[dict]:
    """[{pid, section, text}] for a document: full text if fetched, else the abstract."""
    out = []
    data = read_json(lake / "sources" / f"{doc.get('pmcid')}.json", {}) if doc.get("text_source") == "fulltext" else {}
    if data.get("status") == "success":
        for i, s in enumerate(data.get("sections") or []):
            for j, p in enumerate(s.get("paragraphs") or []):
                if p and len(p) > 40:
                    out.append({"pid": f"s{i}p{j}", "section": s.get("title") or "", "text": p[:PASSAGE_CHARS]})
        for i, t in enumerate(data.get("tables") or []):
            txt = f"{t.get('label') or 'Table'}: {t.get('caption') or ''}\n{t.get('text') or ''}"
            out.append({"pid": f"t{i}", "section": "Table", "text": txt[:PASSAGE_CHARS]})
        for i, f in enumerate(data.get("figures") or []):
            if f.get("caption"):
                out.append({"pid": f"f{i}", "section": "Figure", "text": f"{f.get('label') or 'Figure'}: {f['caption']}"[:PASSAGE_CHARS]})
    if not out and rec.get("abstract"):
        out.append({"pid": "abs", "section": "Abstract", "text": rec["abstract"][:PASSAGE_CHARS * 2]})
    return out


def eid_for(key: str, pid: str, quote: str) -> str:
    """A content hash, so one sentence has one id in every run and across sub-questions.

    Sequential ids renumbered on every re-extract, which silently repointed any stored
    citation and made before/after comparison invalid; they also gave the same sentence a
    fresh id per sub-question, so it counted more than once wherever evidence was counted.
    """
    h = hashlib.sha1(f"{key}|{pid}|{verify.norm(quote)}".encode()).hexdigest()[:8]
    return f"E{h}"


def context_for(passage: dict, quote: str) -> str:
    """For a table quote, the heading line plus the row the quote sits in; else "".

    A table passage is "<label>: <caption>" then one line per row, cells joined with " | ".
    A quote may be a single cell, and the row label naming the trial, arm or condition is
    two columns away. Storing the row keeps the referent with the quote without touching
    the quote itself.
    """
    if not str(passage.get("pid") or "").startswith("t"):
        return ""
    lines = [l for l in (passage.get("text") or "").splitlines() if l.strip()]
    if not lines:
        return ""
    q = verify.norm(quote)
    row = next((l for l in lines[1:] if q and q in verify.norm(l)), "")
    return " / ".join(x for x in (lines[0], row) if x)[:400]


def embed(texts: list[str]):
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(f"{llm.OLLAMA}/api/embed", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["embeddings"]


def _cos(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _lexical(q: str, t: str) -> float:
    qw = set(re.findall(r"[a-z0-9]{3,}", q.lower()))
    tw = re.findall(r"[a-z0-9]{3,}", t.lower())
    return sum(1 for w in tw if w in qw) / (1 + len(tw)) ** 0.5


def rank_passages(query: str, passages: list[dict], embedder=None) -> tuple[list[dict], str]:
    """Abstract paragraphs always, then the passages most similar to the query."""
    head = [p for p in passages if p["section"] == "Abstract"]
    rest = [p for p in passages if p["section"] != "Abstract"]
    if not rest:
        return head, "abstract"
    method = "embed"
    try:
        vecs = (embedder or embed)([query] + [p["text"] for p in rest])
        scores = [_cos(vecs[0], v) for v in vecs[1:]]
    except Exception as e:  # Ollama down or model missing: keyword overlap, logged as such
        print(f"extract: embeddings unavailable ({e}); using keyword overlap", file=sys.stderr)
        method = "lexical"
        scores = [_lexical(query, p["text"]) for p in rest]
    ranked = [p for _, p in sorted(zip(scores, rest), key=lambda x: -x[0])]
    if PASSAGES_PER_SECTION:
        per, top = {}, []
        for p in ranked:                       # best first, at most N per section
            sec = p["section"] or "?"
            if per.get(sec, 0) >= PASSAGES_PER_SECTION:
                continue
            per[sec] = per.get(sec, 0) + 1
            top.append(p)
            if len(top) >= PASSAGES_PER_CALL:
                break
        if len(top) < PASSAGES_PER_CALL:       # few sections: fill the rest by rank
            chosen = {p["pid"] for p in top}
            top += [p for p in ranked if p["pid"] not in chosen][:PASSAGES_PER_CALL - len(top)]
    else:
        top = ranked[:PASSAGES_PER_CALL]
    order = {p["pid"]: i for i, p in enumerate(passages)}
    return head + sorted(top, key=lambda p: order[p["pid"]]), method


# ── extract ─────────────────────────────────────────────────────────────────
KINDS = ["finding", "limitation", "method_detail", "context"]
EXTRACT_SCHEMA = {
    "type": "object", "required": ["claims"],
    "properties": {"claims": {"type": "array", "maxItems": 6, "items": {
        "type": "object", "required": ["claim", "quote", "passage", "kind", "source"],
        "properties": {"claim": {"type": "string"}, "quote": {"type": "string"}, "passage": {"type": "string"},
                       "kind": {"type": "string", "enum": KINDS}, "source": {"type": "string", "enum": ["this_paper", "cited_work"]}}}}}}
CLAIMFIX_SCHEMA = {"type": "object", "required": ["claim"], "properties": {"claim": {"type": "string"}}}
WHO = {"independent_comparison": "an independent comparison of existing methods",
       "new_method": "a paper presenting its own new method; its comparisons are the authors' evaluation of their own method",
       "review": "a review of other studies", "primary_study": "a primary study", "other": "other"}


def stage_extract(lake: Path, force=False):
    plan = read_json(lake / "plan.json")
    subs = {s["id"]: s for s in plan["subquestions"]}
    sel = read_json(lake / "selected.json")
    recs = {r["key"]: r for r in read_jsonl(lake / "records.jsonl")}
    ep, rp = lake / "evidence.jsonl", lake / "evidence_rejected.jsonl"
    if force:
        for p in (ep, rp):
            p.unlink(missing_ok=True)
    done = {(e["key"], e["sid"]) for e in read_jsonl(ep) + read_jsonl(rp)}
    done |= {(d["key"], d["sid"]) for d in read_jsonl(lake / "extract_done.jsonl")} if not force else set()
    if force:
        (lake / "extract_done.jsonl").unlink(missing_ok=True)
    n_e = len(read_jsonl(ep))
    methods = {}
    for d in sel["docs"]:
        rec = recs.get(d["key"], {})
        allp = passages_for(lake, d, rec)
        if not allp:
            continue
        by_id = {p["pid"]: p for p in allp}
        for sid in d["sids"]:
            if (d["key"], sid) in done:
                continue
            chosen, how = rank_passages(subs[sid]["text"], allp)
            methods[how] = methods.get(how, 0) + 1
            block = "\n\n".join(f"[{p['pid']}] ({p['section']})\n{p['text']}" for p in chosen)
            meta = f"{d.get('title')} ({d.get('pub_year')}; {d.get('journal')}). This is {WHO.get(d['evidence_type'], 'other')}."
            out, _ = llm.call(lake, f"extract:{sid}:{d['key']}", "extract.md",
                              {"subquestion": subs[sid]["text"], "paper": meta, "passages": block}, EXTRACT_SCHEMA)
            keep, reject = [], []
            for c in out.get("claims", [])[:6]:
                pid, how_found, ratio = verify.find_quote(c.get("quote", ""), {k: v["text"] for k, v in by_id.items()},
                                                          prefer=c.get("passage"))
                row = {"sid": sid, "key": d["key"], "pmcid": d.get("pmcid"), "claim": c.get("claim", "").strip(),
                       "quote": c.get("quote", "").strip(), "kind": c.get("kind"),
                       "source": c.get("source") if c.get("source") in ("this_paper", "cited_work") else "this_paper", "evidence_type": d["evidence_type"],
                       "title": d.get("title"), "year": d.get("pub_year"), "doi": d.get("doi"), "url": d.get("url"),
                       "authors": d.get("authors"), "text_source": d.get("text_source")}
                if pid:
                    # The quote exists; now check the paraphrase adds nothing (one repair, then reject).
                    issues = verify.claim_issues(row["claim"], row["quote"], d.get("title"), d["evidence_type"])
                    repaired = False
                    if issues:
                        fix, _ = llm.call(lake, f"claimfix:{sid}:{d['key']}", "claimfix.md",
                                          {"quote": row["quote"], "claim": row["claim"], "problems": "\n".join(f"- {i}" for i in issues),
                                           "paper": meta}, CLAIMFIX_SCHEMA)
                        new = str(fix.get("claim", "")).strip()
                        if new and not verify.claim_issues(new, row["quote"], d.get("title"), d["evidence_type"]):
                            row["claim_original"], row["claim"], repaired = row["claim"], new, True
                    if issues and not repaired:
                        reject.append({**row, "cited_passage": c.get("passage"), "why": "claim_adds", "issues": issues})
                        continue
                    n_e += 1
                    eid = eid_for(d["key"], pid, row["quote"])
                    if any(k["eid"] == eid for k in keep):
                        continue          # the model returned the same sentence twice
                    keep.append({"eid": eid, **row, "passage": pid, "section": by_id[pid]["section"],
                                 "context": context_for(by_id[pid], row["quote"]),
                                 "match": how_found, "match_ratio": ratio, "claim_repaired": repaired})
                else:
                    reject.append({**row, "cited_passage": c.get("passage"), "why": how_found, "best_ratio": ratio})
            append_jsonl(ep, keep)
            append_jsonl(rp, reject)
            append_jsonl(lake / "extract_done.jsonl", [{"key": d["key"], "sid": sid}])
            print(f"extract: {sid} {d.get('pmcid') or d['key']} kept {len(keep)} rejected {len(reject)}", file=sys.stderr)
    ev, rj = read_jsonl(ep), read_jsonl(rp)
    manifest_update(lake, "extract", {
        "evidence": len(ev), "rejected": len(rj), "fuzzy": sum(1 for e in ev if e["match"] == "fuzzy"),
        "claims_repaired": sum(1 for e in ev if e.get("claim_repaired")), "claims_rejected": sum(1 for r in rj if r.get("why") == "claim_adds"),
        "per_subquestion": {s: sum(1 for e in ev if e["sid"] == s) for s in subs},
        "passage_ranking": methods, "passages_per_call": PASSAGES_PER_CALL,
        "passages_per_section": PASSAGES_PER_SECTION, "prompt_hash": llm.prompt_hash("extract.md")})
