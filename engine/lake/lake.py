#!/usr/bin/env python3
"""Evidence lake: a fixed research protocol where code runs the steps and the model fills narrow slots.

  lake.py run "<question>" [--until STAGE]   create a lake and run the stages in order
  lake.py <stage> <lake_dir> [--force]        run one stage on an existing lake
  lake.py status <lake_dir>                   counts per stage

Stages: plan → collect → screen → select → fetch → extract → synthesize → check → report. Every stage reads and writes files in the lake folder,
skips work already done (so a crash resumes), and records what it did in manifest.json.
Lakes live under $LAKE_ROOT (default <repo>/data/lakes, or $HARNESS_DATA/lakes; see paths.py).
"""
import argparse, hashlib, html, json, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402
from store import PROTOCOL, read_json, write_json, read_jsonl, append_jsonl, manifest_update  # noqa: E402
import evidence  # noqa: E402
import synth  # noqa: E402

HERE = Path(__file__).parent
ROOT = Path(os.environ.get("LAKE_ROOT") or Path(os.environ.get("HARNESS_DATA") or Path(__file__).resolve().parents[2] / "data") / "lakes").expanduser()  # see paths.py
COLLECT_MAX = int(os.environ.get("LAKE_COLLECT_MAX", "100"))   # records stored per query
SCREEN_TOP = int(os.environ.get("LAKE_SCREEN_TOP", "100"))      # top-ranked records per query sent to screening
SCREEN_BATCH = int(os.environ.get("LAKE_SCREEN_BATCH", "8"))
ABSTRACT_CHARS = 1500
# `profile` is not a pipeline stage: nothing downstream reads it (PRINCIPLES D5), and inside STAGES
# every full run paid for its calls. Run it on demand: lake.py profile <lake>.
STAGES = ["plan", "collect", "screen", "chain", "select", "fetch", "extract", "synthesize", "check", "report"]


def slug_for(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())[:6]
    return "-".join(words) + "-" + hashlib.sha1(question.encode()).hexdigest()[:6]


def new_lake(question: str) -> Path:
    slug = slug_for(question)
    lake = ROOT / slug
    lake.mkdir(parents=True, exist_ok=True)
    if not (lake / "manifest.json").exists():
        write_json(lake / "manifest.json", {
            "question": question, "slug": slug, "protocol": PROTOCOL,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": llm.MODEL, "options": llm.OPTIONS,
            "settings": {"collect_max": COLLECT_MAX, "screen_top": SCREEN_TOP, "screen_batch": SCREEN_BATCH},
            "scripts": {f: hashlib.sha256((HERE / f).read_bytes()).hexdigest()[:12]
                        for f in ("epmc_search.R", "epmc_fetch.R", "lake.py", "llm.py", "evidence.py", "verify.py", "store.py", "synth.py")},
            "stages": {}})
    return lake


# ── plan ────────────────────────────────────────────────────────────────────
PLAN_SCHEMA = {
    "type": "object", "required": ["scope", "presupposes", "subquestions"],
    "properties": {"scope": {"type": "string"},
                   "presupposes": {"type": "array", "maxItems": 5, "items": {"type": "object", "required": ["name", "query"],
                                   "properties": {"name": {"type": "string"}, "query": {"type": "string"}}}},
                   "subquestions": {"type": "array", "minItems": 3, "maxItems": 5, "items": {
        "type": "object", "required": ["id", "text", "why", "queries", "counter_query", "asks_about_outcomes"],
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "why": {"type": "string"},
                       "asks_about_outcomes": {"type": "boolean"},
                       "queries": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "string"}},
                       "counter_query": {"type": "string"}}}}}}


def stage_plan(lake: Path, force=False):
    if (lake / "plan.json").exists() and not force:
        return
    q = read_json(lake / "manifest.json")["question"]
    out, _ = llm.call(lake, "plan", "plan.md", {"question": q}, PLAN_SCHEMA)
    subs, seen = [], set()
    def fresh(x):
        x = " ".join(str(x or "").split())
        if x and x.lower() not in seen:
            seen.add(x.lower())
            return x
        return None

    for s in out.get("subquestions", [])[:5]:
        if not str(s.get("text", "")).strip():
            continue
        # Code assigns ids and query ids, so later stages never depend on the model's labels.
        sid = f"S{len(subs) + 1}"
        qs = [{"qid": f"{sid}-q{j}", "query": x, "kind": "main"}
              for j, x in enumerate(filter(None, map(fresh, s.get("queries", []))), 1)]
        # The field tag is code's job, not the model's. plan.md says why it is needed; the model
        # only answers whether this sub-question is about outcomes. It would not emit the tag itself
        # even once the schema allowed three queries (27 Oct), approximating it in free text, which
        # returns 1764 hits with the target trial at rank 775 where the tag returns 13 at rank 8.
        # Built from the FIRST AND group only: the full 3-concept query plus a tag over-restricts,
        # and PUB_TYPE is already doing the filtering.
        if s.get("asks_about_outcomes") and qs:
            core = split_and(qs[0]["query"])[0]
            qs.append({"qid": f"{sid}-t", "query": f'{core} AND PUB_TYPE:"Clinical Trial"', "kind": "trial"})
        c = fresh(s.get("counter_query"))
        if c:
            qs.append({"qid": f"{sid}-c", "query": c, "kind": "counter"})
        if qs:
            subs.append({"id": sid, "text": s["text"].strip(), "why": str(s.get("why", "")).strip(), "queries": qs})
    if len(subs) < 3:
        raise SystemExit(f"plan: only {len(subs)} usable sub-questions; see calls.jsonl")
    pres = [{"pid": f"P{i}", "name": str(p.get("name", "")).strip(), "query": " ".join(str(p.get("query", "")).split())}
            for i, p in enumerate([p for p in out.get("presupposes", []) if p.get("name") and p.get("query")][:5], 1)]
    write_json(lake / "plan.json", {"question": q, "scope": str(out.get("scope", "")).strip(), "presupposes": pres, "subquestions": subs})
    manifest_update(lake, "plan", {"subquestions": len(subs), "queries": sum(len(s["queries"]) for s in subs),
                                   "prompt_hash": llm.prompt_hash("plan.md")})


# ── collect ─────────────────────────────────────────────────────────────────
SORTS = {"": "", "-cited": "CITED desc"}   # each query runs by relevance and again by citation count


def run_search(query: str, out: Path, max_results: int, sort: str = "") -> tuple[bool, str]:
    r = subprocess.run(["Rscript", str(HERE / "epmc_search.R"), query, str(out), str(max_results), "100", sort],
                       capture_output=True, text=True, timeout=600)
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode == 0, lines[-1] if lines else ""


TAG_RE = re.compile(r"<[^>]+>")


def title_key(t):
    t = re.sub(r"[^a-z0-9]+", " ", html.unescape(TAG_RE.sub(" ", t or "")).lower()).strip()
    return t if len(t) > 30 else ""


def clean_abstract(a):
    return " ".join(html.unescape(TAG_RE.sub(" ", a)).split()) if a else ""


RELAX_MIN = int(os.environ.get("LAKE_RELAX_MIN", "30"))   # a query with fewer hits is relaxed
# plan.md appends PUB_TYPE:"Clinical Trial" as the LAST AND group of a tagged query, and that tag is
# the whole point of the query (relevance ranking buries individual trial reports under reviews of
# them). Relaxing by dropping the last group would strip exactly the tag, so drop the last UNtagged
# group instead. A query that is nothing but tags is left alone.
FIELD_TAG_RE = re.compile(r"\b[A-Z][A-Z_]{2,}:")


def drop_group(groups: list) -> list:
    """Remove the last AND group that carries no field tag; [] if there is nothing safe to drop."""
    for i in range(len(groups) - 1, -1, -1):
        if not FIELD_TAG_RE.search(groups[i]):
            return groups[:i] + groups[i + 1:]
    return []


def split_and(query: str) -> list:
    """Top-level AND groups of a search string (respects quotes and parentheses)."""
    groups, depth, quoted, cur, i = [], 0, False, "", 0
    while i < len(query):
        ch = query[i]
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        if not quoted and depth == 0 and query[i:i + 5].upper() == " AND ":
            groups.append(cur.strip())
            cur, i = "", i + 5
            continue
        cur += ch
        i += 1
    groups.append(cur.strip())
    return [g for g in groups if g]


def _search(qdir: Path, qid: str, query: str, sort: str, force: bool, failed: list) -> dict | None:
    out = qdir / f"{qid}.json"
    if not out.exists() or force:
        ok, msg = run_search(query, out, COLLECT_MAX, sort)
        if not ok or not out.exists():
            failed.append({"qid": qid, "error": msg})
            print(f"collect: {qid} FAILED: {msg}", file=sys.stderr)
            return None
        d = read_json(out)
        print(f"collect: {qid} {d['n']} of {d.get('hit_count')} hits", file=sys.stderr)
    return read_json(out)


def stage_collect(lake: Path, force=False):
    plan = read_json(lake / "plan.json")
    qdir = lake / "queries"
    qdir.mkdir(exist_ok=True)
    failed, index = [], []
    for s in plan["subquestions"]:
        for q in s["queries"]:
            variants = [(q["qid"], q["query"])]
            first = _search(qdir, q["qid"], q["query"], "", force, failed)
            # Too few hits usually means too many required terms: drop the last AND group until the
            # query finds enough, keeping at least two groups. Decided in code and logged.
            groups = split_and(q["query"])
            hits = (first or {}).get("hit_count") or 0
            level = 0
            while first is not None and hits < RELAX_MIN and len(groups) > 2:
                nxt = drop_group(groups)
                if len(nxt) == len(groups) or len(nxt) < 2:
                    break            # nothing droppable but the field tag: leave the query as it is
                groups = nxt
                level += 1
                rq = " AND ".join(groups)
                rid = f"{q['qid']}-r{level}"
                d = _search(qdir, rid, rq, "", force, failed)
                hits = (d or {}).get("hit_count") or 0
                variants.append((rid, rq))
            for vid, vq in variants:
                for suffix, sort in SORTS.items():
                    qid = vid + suffix
                    # relevance-sorted variants were already run above; read them, do not search again
                    d = read_json(qdir / f"{qid}.json") if not suffix else _search(qdir, qid, vq, sort, force, failed)
                    if d:
                        index.append({"qid": qid, "base": q["qid"], "sid": s["id"], "query": vq,
                                      "sort": sort or "relevance", "n": d["n"], "hit_count": d.get("hit_count")})
    write_json(qdir / "index.json", index)
    # Premise check (code only): does what the question takes for granted exist in Europe PMC?
    premises = []
    for p in plan.get("presupposes", []):
        out = qdir / f"{p['pid']}.json"
        if not out.exists() or force:
            ok, msg = run_search(p["query"], out, 50)   # 50, not 20: the identity test reads them all
            if not ok:
                failed.append({"qid": p["pid"], "error": msg})
        data = read_json(out, {}) or {}
        recs_p = data.get("records") or []
        # hits > 0 only said the search returned something. Ask whether any record IS this thing.
        import verify as _v
        premises.append({**p, "hits": data.get("hit_count", len(recs_p)) if data else None,
                         "examined": len(recs_p), **_v.premise_verdict(p.get("name", ""), recs_p),
                         "top": [{"title": r.get("title"), "year": r.get("pub_year"), "doi": r.get("doi")} for r in recs_p[:3]]})
    write_json(lake / "premises.json", premises)
    # Merge into one deduplicated record table; each record remembers which queries found it, at what rank.
    # The same paper can appear as several records (preprint and journal version): merge on DOI or
    # normalized title and keep the peer-reviewed version's metadata.
    recs, alias, merged = {}, {}, 0
    for v in index:
        data = read_json(qdir / f"{v['qid']}.json")
        for r in (data or {}).get("records", []):
            key = f"{r.get('source')}:{r.get('id')}"
            ids = [i for i in (f"doi:{(r.get('doi') or '').lower()}" if r.get("doi") else None,
                               f"title:{title_key(r.get('title'))}" if title_key(r.get("title")) else None, key) if i]
            canon = next((alias[i] for i in ids if i in alias), None)
            if canon is None:
                canon = key
                recs[key] = {"key": key, **{k: val for k, val in r.items() if k != "rank"},
                             "abstract": clean_abstract(r.get("abstract")), "hits": [], "also": []}
            elif key != canon and key not in recs[canon]["also"]:
                merged += 1
                recs[canon]["also"].append(key)
                if recs[canon].get("is_preprint") and not r.get("is_preprint"):
                    keep = {k: recs[canon][k] for k in ("key", "hits", "also")}
                    recs[canon] = {**{k: val for k, val in r.items() if k != "rank"},
                                   "abstract": clean_abstract(r.get("abstract")) or recs[canon]["abstract"], **keep}
            for i in ids:
                alias.setdefault(i, canon)
            recs[canon]["hits"].append({"qid": v["qid"], "sid": v["sid"], "rank": r["rank"]})
    # Chain-added records come from no query, so rebuilding this file from index.json alone deleted
    # them on every resumed run -- and stage_chain skips itself while chain.json exists, so they did
    # not come back. (27 Oct: records 928 -> 888, and selected.json was left naming MED:10426310,
    # which extract then had no text for and skipped.) Carry them forward.
    p = lake / "records.jsonl"
    kept_chained = [r for r in read_jsonl(p) if r.get("chained_from") and r.get("key") not in recs]
    p.write_text("")
    append_jsonl(p, list(recs.values()) + kept_chained)
    manifest_update(lake, "collect", {
        "per_query": {v["qid"]: [v["n"], v["hit_count"]] for v in index}, "failed": failed, "unique_records": len(recs),
        "chained_kept": len(kept_chained),
        "duplicates_merged": merged, "sorts": list(SORTS.values()), "relax_min": RELAX_MIN,
        "relaxed": sorted({v["base"] for v in index if "-r" in v["qid"]})})


# ── screen ──────────────────────────────────────────────────────────────────
EVIDENCE_TYPES = ["independent_comparison", "new_method", "review", "primary_study", "other"]
SCREEN_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["n", "evidence_type", "in_scope", "relevant", "reason"],
        "properties": {"n": {"type": "integer"}, "evidence_type": {"type": "string", "enum": EVIDENCE_TYPES},
                       "in_scope": {"type": "boolean"},
                       "relevant": {"type": "boolean"}, "reason": {"type": "string"}}}}}}


def record_card(n: int, r: dict) -> str:
    meta = "; ".join(str(x) for x in [r.get("pub_year"), ", ".join(r.get("pub_types") or [])[:60],
                                       r.get("journal")] if x)
    return f"[{n}] {r.get('title') or '(no title)'} ({meta})\nAbstract: {r['abstract'][:ABSTRACT_CHARS]}"


def candidates(recs: list, sid: str) -> list:
    """Records found by this sub-question's queries within the top SCREEN_TOP ranks, best rank first."""
    out = []
    for r in recs:
        ranks = [h["rank"] for h in r["hits"] if h["sid"] == sid]
        if ranks and min(ranks) <= SCREEN_TOP:
            out.append((min(ranks), r))
    return [r for _, r in sorted(out, key=lambda x: (x[0], x[1]["key"]))]


SCOPE_SCHEMA = {"type": "object", "required": ["scope"], "properties": {"scope": {"type": "string"}}}


def ensure_scope(lake: Path, plan: dict) -> str:
    """Lakes planned before scope existed get it from one small call, stored in plan.json."""
    if not plan.get("scope"):
        out, _ = llm.call(lake, "scope", "scope.md", {"question": plan["question"]}, SCOPE_SCHEMA)
        plan["scope"] = str(out.get("scope", "")).strip() or "(no restriction stated)"
        write_json(lake / "plan.json", plan)
    return plan["scope"]


def screen_batch(lake: Path, sub: dict, batch: list, scope: str = "", question: str = "") -> list:
    cards = "\n\n".join(record_card(i, r) for i, r in enumerate(batch, 1))
    out, decisions = llm.call(lake, f"screen:{sub['id']}", "screen.md",
                              {"question": question, "subquestion": sub["text"], "scope": scope or "(no restriction)", "records": cards},
                              SCREEN_SCHEMA, prob_fields=("evidence_type", "in_scope", "relevant"))
    items = {}
    for it in out.get("items", []):
        if isinstance(it.get("n"), int) and 1 <= it["n"] <= len(batch) and it["n"] not in items:
            items[it["n"]] = it
    n_items = len(out.get("items", []))
    p_rel = [d for d in decisions if d["field"] == "relevant"]
    p_typ = [d for d in decisions if d["field"] == "evidence_type"]
    p_rel = p_rel if len(p_rel) == n_items else []
    p_typ = p_typ if len(p_typ) == n_items else []
    rows = []
    for j, it in enumerate(out.get("items", [])):
        n = it.get("n")
        if items.get(n) is not it:
            continue
        rows.append({"sid": sub["id"], "key": batch[n - 1]["key"], "relevant": bool(it["relevant"]),
                     "evidence_type": it.get("evidence_type") if it.get("evidence_type") in EVIDENCE_TYPES else "other",
                     "in_scope": bool(it.get("in_scope", True)),
                     "reason": it.get("reason", ""), "by": "llm",
                     "p": p_rel[j]["p"] if p_rel else None, "p_type": p_typ[j]["p"] if p_typ else None})
    return rows


def stage_screen(lake: Path, force=False):
    plan = read_json(lake / "plan.json")
    scope = ensure_scope(lake, plan)
    recs = read_jsonl(lake / "records.jsonl")
    # A collect stage whose searches failed must stop the lake, not feed it nothing. 28 Oct: Europe
    # PMC refused every query with HTTP 503 and 9 lakes still ran to report.md with 0 papers, which
    # looked complete to anything that only checked for a report.
    import os as _os
    _m = read_json(lake / "manifest.json") or {}
    _failed = ((_m.get("stages", _m) or {}).get("collect") or {}).get("failed") or []
    if not recs or (_failed and _os.environ.get("LAKE_ALLOW_PARTIAL") != "1"):
        raise RuntimeError(f"collect incomplete: {len(recs)} records, {len(_failed)} failed searches. "
                           "Not screening a partial lake; LAKE_ALLOW_PARTIAL=1 to override.")
    sp = lake / "screen.jsonl"
    if force and sp.exists():
        sp.unlink()
    done = {(r["sid"], r["key"]) for r in read_jsonl(sp)}
    for sub in plan["subquestions"]:
        todo, rule_rows = [], []
        for r in candidates(recs, sub["id"]):
            if (sub["id"], r["key"]) in done:
                continue
            # Rules decided in code, logged like any other decision.
            if r.get("is_retracted"):
                rule_rows.append({"sid": sub["id"], "key": r["key"], "relevant": False, "reason": "retracted", "by": "rule"})
            elif len(r["abstract"]) < 100:
                rule_rows.append({"sid": sub["id"], "key": r["key"], "relevant": None, "reason": "no abstract", "by": "rule"})
            else:
                todo.append(r)
        append_jsonl(sp, rule_rows)
        missing = []
        for i in range(0, len(todo), SCREEN_BATCH):
            batch = todo[i:i + SCREEN_BATCH]
            rows = screen_batch(lake, sub, batch, scope, plan["question"])
            append_jsonl(sp, rows)
            got = {r["key"] for r in rows}
            missing += [r for r in batch if r["key"] not in got]
            print(f"screen: {sub['id']} {min(i + SCREEN_BATCH, len(todo))}/{len(todo)}", file=sys.stderr)
        for r in missing:  # one retry, one record at a time
            append_jsonl(sp, screen_batch(lake, sub, [r], scope, plan["question"]) or
                         [{"sid": sub["id"], "key": r["key"], "relevant": None, "reason": "unscreened (no valid reply)", "by": "rule"}])
    rows = read_jsonl(sp)
    per = {s["id"]: {"screened": sum(1 for r in rows if r["sid"] == s["id"]),
                     "relevant": sum(1 for r in rows if r["sid"] == s["id"] and r["relevant"])} for s in plan["subquestions"]}
    manifest_update(lake, "screen", {"per_subquestion": per, "prompt_hash": llm.prompt_hash("screen.md")})


# ── chain ───────────────────────────────────────────────────────────────────
# Citation chaining (code only): the references and citing papers of the screened independent
# comparisons and reviews. Papers linked to by several of them are central to the topic whether or
# not the plan's queries used their vocabulary. They are added to the records and screened as usual.
CHAIN_MAX = int(os.environ.get("LAKE_CHAIN_MAX", "40"))
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def fetch_links(source: str, pid: str, kind: str) -> list:
    """kind: 'references' (what the paper cites) or 'citations' (what cites it) -> [{source, id, title}]."""
    import urllib.request, urllib.error
    url = f"{EPMC}/{source}/{pid}/{kind}?format=json&pageSize=1000"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                d = json.load(r)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                print(f"chain: {source}:{pid} {kind} FAILED: {e}", file=sys.stderr)
                return []
            time.sleep(2 ** attempt)
    items = (d.get("referenceList") or {}).get("reference") or (d.get("citationList") or {}).get("citation") or []
    return [{"source": x.get("source"), "id": x.get("id"), "title": x.get("title")} for x in items if x.get("id") and x.get("source")]


def stage_chain(lake: Path, force=False):
    if (lake / "chain.json").exists() and not force:
        # Do not skip when the records this stage added are gone: a collect run before patch I
        # rebuilt records.jsonl without them. Re-running repairs the lake.
        gone = [k for k in (read_json(lake / "chain.json", {}) or {}).get("added", [])
                if k not in {r["key"] for r in read_jsonl(lake / "records.jsonl")}]
        if not gone:
            return
        print(f"chain: re-running; {len(gone)} previously added records are missing from records.jsonl",
              file=sys.stderr)
    recs = {r["key"]: r for r in read_jsonl(lake / "records.jsonl")}
    seeds = {}
    for r in read_jsonl(lake / "screen.jsonl"):
        if r.get("relevant") and r.get("in_scope", True) and r.get("evidence_type") in ("independent_comparison", "review"):
            seeds.setdefault(r["key"], set()).add(r["sid"])
    links = {}   # candidate key -> {"seeds": set, "sids": set}
    for key, sids in seeds.items():
        src, pid = key.split(":", 1)
        for kind in ("references", "citations"):
            for x in fetch_links(src, pid, kind):
                k = f"{x['source']}:{x['id']}"
                if k == key:
                    continue
                c = links.setdefault(k, {"seeds": set(), "sids": set()})
                c["seeds"].add(key)
                c["sids"] |= sids
    # new papers only, most-connected first; ties by key for determinism
    new = sorted(((k, v) for k, v in links.items() if k not in recs), key=lambda kv: (-len(kv[1]["seeds"]), kv[0]))
    pick = [(k, v) for k, v in new if len(v["seeds"]) >= 2][:CHAIN_MAX] or new[:min(CHAIN_MAX, 15)]
    added = []
    qdir = lake / "queries"
    for i in range(0, len(pick), 25):
        batch = pick[i:i + 25]
        q = " OR ".join(f"(EXT_ID:{k.split(':', 1)[1]} AND SRC:{k.split(':', 1)[0]})" for k, _ in batch)
        out = qdir / f"chain-{i // 25}.json"
        ok, msg = run_search(q, out, 100)
        data = read_json(out, {}) if ok else {}
        for r in (data or {}).get("records", []):
            k = f"{r.get('source')}:{r.get('id')}"
            v = dict(batch).get(k)
            if not v or k in recs:
                continue
            recs[k] = {"key": k, **{kk: vv for kk, vv in r.items() if kk != "rank"}, "abstract": clean_abstract(r.get("abstract")),
                       "hits": [{"qid": "chain", "sid": sid, "rank": 1} for sid in sorted(v["sids"])], "also": [],
                       "chained_from": sorted(v["seeds"])}
            added.append(k)
    p = lake / "records.jsonl"
    p.write_text("")
    append_jsonl(p, recs.values())
    write_json(lake / "chain.json", {"seeds": sorted(seeds), "candidates": len(links), "new": len(new),
                                     "picked": [{"key": k, "linked_seeds": len(v["seeds"])} for k, v in pick], "added": added})
    manifest_update(lake, "chain", {"seeds": len(seeds), "candidates": len(links), "picked": len(pick), "added": len(added)})
    print(f"chain: {len(seeds)} seeds, {len(links)} linked papers, {len(added)} added; screening them", file=sys.stderr)
    stage_screen(lake)   # incremental: only the added records are screened


# ── profile ─────────────────────────────────────────────────────────────────
# What system each piece of evidence was observed in, and by what method. One narrow call per batch
# of 8 items (prompts/evidence_profile.md); the model must copy the words that name the system and
# method, and check_profile resets the label to "unclear" when those words are not in the item's
# claim or quote. This belongs in the lake rather than in a reader because no reader can recover it
# from the quote: a review summarising mouse work writes it in species-neutral prose.
def stage_profile(lake: Path, force=False):
    import collections
    import truthinator                         # stdlib-only imports; profile_evidence logs to calls.jsonl
    p = lake / "evidence_profile.jsonl"
    if force:
        p.unlink(missing_ok=True)
    evs = read_jsonl(lake / "evidence.jsonl")
    done = {r["eid"] for r in read_jsonl(p)}
    todo = [e for e in evs if e["eid"] not in done]
    if todo:
        res = truthinator.profile_evidence(lake, todo, read_json(lake / "plan.json"))
        append_jsonl(p, [res[e["eid"]] for e in todo if e["eid"] in res])
    rows = read_jsonl(p)
    manifest_update(lake, "profile", {
        "items": len(rows), "newly_profiled": len(todo),
        "reset_by_word_check": sum(1 for r in rows if r.get("notes")),
        "by_system": dict(collections.Counter(r.get("system") for r in rows)),
        "by_method": dict(collections.Counter(r.get("method") for r in rows)),
        "by_directness": dict(collections.Counter(r.get("directness") for r in rows)),
        "prompt_hash": llm.prompt_hash("evidence_profile.md")})


# ── status / cli ────────────────────────────────────────────────────────────
def status(lake: Path):
    m = read_json(lake / "manifest.json", {})
    print(f"{lake}\n  question: {m.get('question')}\n  protocol {m.get('protocol')}  model {m.get('model')}")
    plan = read_json(lake / "plan.json")
    if plan:
        screen = read_jsonl(lake / "screen.jsonl")
        for s in plan["subquestions"]:
            rel = sum(1 for r in screen if r["sid"] == s["id"] and r["relevant"])
            n = sum(1 for r in screen if r["sid"] == s["id"])
            types = {}
            for r in screen:
                if r["sid"] == s["id"] and r["relevant"]:
                    types[r.get("evidence_type", "?")] = types.get(r.get("evidence_type", "?"), 0) + 1
            print(f"  {s['id']} {s['text'][:90]}\n     queries: " + " | ".join(q["query"][:60] for q in s["queries"]) +
                  f"\n     screened {n}, relevant {rel} " + str(types))
    sel = read_json(lake / "selected.json")
    if sel:
        src = {}
        for d in sel["docs"]:
            src[d.get("text_source", "?")] = src.get(d.get("text_source", "?"), 0) + 1
        print(f"  selected {len(sel['docs'])} docs {src}")
    ev, rj = read_jsonl(lake / "evidence.jsonl"), read_jsonl(lake / "evidence_rejected.jsonl")
    if ev or rj:
        print(f"  evidence {len(ev)} kept ({sum(1 for e in ev if e['match'] == 'fuzzy')} fuzzy), {len(rj)} rejected quotes")
    for k, v in m.get("stages", {}).items():
        print(f"  [{k}] done {v.get('done')}" + (f"  failed={v['failed']}" if v.get("failed") else ""))


FUNCS = {"plan": stage_plan, "collect": stage_collect, "screen": stage_screen, "chain": stage_chain,
         "select": evidence.stage_select, "fetch": evidence.stage_fetch, "extract": evidence.stage_extract,
         "profile": stage_profile,
         "synthesize": synth.stage_synthesize, "check": synth.stage_check, "report": synth.stage_report}


# What each stage writes. Forcing a stage removes its outputs and those of every later stage,
# so nothing downstream is left built on the old version. Fetched full texts (sources/) are a cache and stay.
OUTPUTS = {
    "plan": ["plan.json"], "collect": ["queries", "records.jsonl", "premises.json"], "screen": ["screen.jsonl"], "chain": ["chain.json"],
    "select": ["selected.json"], "fetch": [], "extract": ["evidence.jsonl", "evidence_rejected.jsonl", "extract_done.jsonl"],
    "profile": ["evidence_profile.jsonl"],
    "synthesize": ["synthesis.json", "synthesis_raw.json"], "check": [], "report": ["report.md", "metrics.json"],
}


def invalidate(lake: Path, stage: str):
    import shutil
    later = STAGES[STAGES.index(stage):]
    for st in later:
        for name in OUTPUTS[st]:
            p = lake / name
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
    m = read_json(lake / "manifest.json", {})
    for st in later:
        m.get("stages", {}).pop(st, None)
    write_json(lake / "manifest.json", m)
    print(f"cleared outputs of: {', '.join(later)}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "status", *STAGES, "profile"])
    ap.add_argument("target", help="question (run) or lake folder")
    ap.add_argument("--until", choices=STAGES, default=STAGES[-1])
    ap.add_argument("--force", action="store_true", help="redo this stage and clear every later stage's outputs")
    ap.add_argument("--fresh", action="store_true", help="run: move an existing lake for this question aside and start over")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        old = ROOT / slug_for(a.target)
        if a.fresh and old.exists():
            old.rename(old.with_name(old.name + time.strftime(".old-%Y%m%d-%H%M%S")))
        lake = new_lake(a.target)
        print(lake)
        for st in STAGES[:STAGES.index(a.until) + 1]:
            FUNCS[st](lake)
        status(lake)
    elif a.cmd == "status":
        status(Path(a.target).expanduser())
    else:
        d = Path(a.target).expanduser()
        if a.force:
            invalidate(d, a.cmd)
            if a.cmd == "check":  # check rewrites synthesis.json in place; restart it from the raw synthesis
                raw = read_json(d / "synthesis_raw.json")
                if raw:
                    write_json(d / "synthesis.json", raw)
        FUNCS[a.cmd](d, force=a.force)


if __name__ == "__main__":
    main()
