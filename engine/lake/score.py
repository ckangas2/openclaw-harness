"""Frozen scorer for the evidence lake. Pinned at SCORE_VERSION; do not edit during a tuning run.

Deliberately imports nothing from the pipeline. check_point and verify are things we tune, so a
score computed with them moves when they move: the doublet lake's "first-pass 23 -> 13" on 28 Oct
was a new checker rule, not a worse draft. Everything this needs is re-implemented below and
frozen. Changing any of it means a new SCORE_VERSION and re-running every baseline.

  python3 engine/lake/score.py <lake_dir> --case <id> --cases <cases.json>
  python3 engine/lake/score.py <root_with_case_dirs> --cases <cases.json>   (scores what it matches)
"""
import argparse, difflib, json, re, sys, unicodedata
from pathlib import Path

SCORE_VERSION = "score-3"

# Registry formats only; study NAMES come from the lake's studies.json (model-named, code-verified).
# score-1 carried a list of 14 oncology trial brands, which made it blind outside oncology.
REG_RE = re.compile(r"\b(NCT\d{8}|ISRCTN\d{8}|ACTRN\d{14}|DRKS\d{8}|ChiCTR[-\w]{6,20}|"
                    r"EudraCT[\s:]*\d{4}-\d{6}-\d{2}|CTRI/\d{4}/\d{2,3}/\d{6})\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[,\u00a0\u2009\u202f ]\d{3})+(?![\d.])|\d+(?:\.\d+)?)(\s?%)?")


_QUOTES = {8216: "'", 8217: "'", 8220: '"', 8221: '"', 8211: '-', 8212: '-', 8722: '-', 160: ' ', 8201: ' ', 8239: ' '}  # copied from verify.py 28 Oct; frozen


def norm(s: str) -> str:
    """Lowercase, unify quotes/dashes/spaces, drop citation brackets, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES).lower()
    s = re.sub(r"\[\d+(?:[,–-]\s*\d+)*\]", " ", s)   # [12], [3-5], [1,2]
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .;,")


def numbers(text, names=()):
    text = re.sub(r"(?<=\d)\N{MIDDLE DOT}(?=\d)", ".", text or "")          # Lancet middle-dot decimals
    text = REG_RE.sub(" ", text)                                     # registry ids are names
    for n in sorted(names, key=len, reverse=True):                   # so are the lake's study names
        parts = [re.escape(x) for x in n.split()]
        text = re.sub(r"(?<![A-Za-z0-9])" + r"[\s\-_/:]*".join(parts) + r"(?![A-Za-z0-9])", " ", text, flags=re.I)
    out = set()
    for m in NUM_RE.finditer(text):
        v = re.sub(r"[,\N{NO-BREAK SPACE}\N{THIN SPACE}\N{NARROW NO-BREAK SPACE} ]", "", m.group(1))
        if re.fullmatch(r"(19|20)\d\d", v):
            continue
        out.add(v.rstrip("0").rstrip(".") if "." in v else v)
    return out


def study_info(lake):
    """(all verified study names, key -> study id) from the lake's studies.json. A lake built before
    studies.json existed gets no names, and every paper counts as its own study."""
    p = lake / "studies.json"
    info = json.loads(p.read_text()) if p.exists() else {}
    ids = {k: set(c.get("registry", [])) | {"name:" + n for n in c.get("names", [])} for k, c in info.items()}
    parent = {k: k for k in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by = {}
    for k, ss in ids.items():
        for t in ss:
            by.setdefault(t, []).append(k)
    for ks in by.values():
        for k in ks[1:]:
            parent[find(k)] = find(ks[0])
    names = {n for c in info.values() for n in c.get("names", [])}
    return names, {k: find(k) for k in parent}


def found_in(quote, hay):
    """Exact after normalisation, else the best-aligned window at >= 0.92 - the pipeline's own rule.
    score-2 slid its window in steps of a quarter of the quote, so a quote starting between grid
    points was misaligned and reported missing (E96, d01, 28 Oct: pipeline ratio 0.96). The window
    is now anchored on the longest exact run shared with each third of the quote."""
    q, h = norm(quote), hay
    if not q or not h:
        return False
    if q in h:
        return True
    n = len(q)
    if n < 40 or len(h) < n:
        return False
    best = 0.0
    for a, b in ((0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)):
        sm = difflib.SequenceMatcher(None, h, q[a:b], autojunk=False)
        m = sm.find_longest_match(0, len(h), 0, b - a)
        if m.size < 8:
            continue
        start = max(0, m.a - (a + m.b))
        for pad in (0, 10, 25):
            w = h[max(0, start - pad): start + n + pad]
            best = max(best, difflib.SequenceMatcher(None, q, w, autojunk=False).ratio())
    return best >= 0.92


def strings(o, out):
    if isinstance(o, str):
        out.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            strings(v, out)
    elif isinstance(o, list):
        for v in o:
            strings(v, out)
    return out


def read_jsonl(p):
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def load_sources(lake):
    """key -> normalised text of THAT paper only: its record title and abstract, plus its fulltext
    file when one was fetched. No union fallback: a union lets a quote attributed to the wrong paper
    pass, which is what this scorer exists to catch (28 Oct: ACE2 keys never matched file stems, and
    a union had been hiding it)."""
    ft = {}
    for f in sorted((lake / "sources").glob("*.json")):
        try:
            ft[f.stem.upper()] = norm(" ".join(strings(json.loads(f.read_text()), [])))
        except Exception:
            continue
    by = {}
    for r in read_jsonl(lake / "records.jsonl"):
        k = r.get("key")
        if k:
            by[k] = norm(" ".join(x for x in (r.get("title"), r.get("abstract")) if x))
    sel = (json.loads((lake / "selected.json").read_text()) or {}).get("docs", [])
    pmc = {d.get("key"): d.get("pmcid") for d in sel}
    for e in read_jsonl(lake / "evidence.jsonl"):
        pmc.setdefault(e.get("key"), e.get("pmcid"))
    for k, pm in pmc.items():
        if not pm:
            continue
        pm = str(pm).upper()
        pm = pm if pm.startswith("PMC") else "PMC" + pm
        if pm in ft:
            by[k] = (by.get(k, "") + " " + ft[pm]).strip()
    return by, ""


def key_group_recall(case, cited_keys, sel):
    """Fraction of key-paper GROUPS reached. A group is satisfied by any of its reports.
    DOI decisive, then PMID; title last - letters and errata carry the original's title."""
    meta = {}
    for d in sel:
        k = d.get("key")
        meta[k] = (norm(d.get("doi")), str(d.get("pmid") or ""), norm(d.get("title")))
    groups, hit = {}, set()
    for kp in case.get("key_papers", []):
        g = kp.get("group") or kp.get("label")
        groups.setdefault(g, []).append(kp)
    for g, papers in groups.items():
        for kp in papers:
            doi, pmid, title = norm(kp.get("doi")), str(kp.get("pmid") or ""), norm(kp.get("title"))
            for k in cited_keys:
                mdoi, mpmid, mtitle = meta.get(k, ("", "", ""))
                if (doi and mdoi and doi == mdoi) or (pmid and mpmid and pmid == mpmid) \
                   or (not doi and not pmid and title and mtitle and title in mtitle):
                    hit.add(g)
                    break
    return len(hit), len(groups), sorted(set(groups) - hit)


def score_lake(lake: Path, case: dict):
    lake = Path(lake)
    syn = json.loads((lake / "synthesis.json").read_text())
    ev = {e["eid"]: e for e in read_jsonl(lake / "evidence.jsonl")}
    sel = (json.loads((lake / "selected.json").read_text()) or {}).get("docs", [])
    by_key, _ = load_sources(lake)
    stype = {d.get("key"): d.get("evidence_type") for d in sel}
    names, group = study_info(lake)

    points = [p for b in syn.get("subquestions", {}).values()
              for p in b.get("points", []) if not p.get("dropped")]
    cited_keys = {ev[x]["key"] for p in points for x in p.get("eids", []) if x in ev}

    # 1. every cited quote is in its own paper's stored text (abstract, plus fulltext if fetched)
    used = {x for p in points for x in p.get("eids", []) if x in ev}
    faith_ok = sum(1 for x in used if found_in(ev[x]["quote"], by_key.get(ev[x].get("key"), "")))

    # 2. every number in a point is in the evidence it cites; study names and registry ids are masked
    num_ok = num_tot = 0
    for p in points:
        c = [ev[x] for x in p.get("eids", []) if x in ev]
        if not c:
            continue
        num_tot += 1
        pool = " ".join(e.get("claim", "") + " " + e.get("quote", "") + " " + (e.get("context") or "") for e in c)
        extra = numbers(p["text"], names) - numbers(pool, names) - numbers(case.get("question", ""), names)
        num_ok += not extra

    # 3. distinct primary studies cited (gate, not score): further reports of one study count once
    prim = {k for k in cited_keys if stype.get(k) == "primary_study"}
    studies = len({group.get(k, k) for k in prim})
    floor = (case.get("expect") or {}).get("primary_studies_min")

    kg_hit, kg_tot, kg_missed = key_group_recall(case, cited_keys, sel)
    c = {"key_groups": [kg_hit, kg_tot], "key_groups_missed": kg_missed,
         "quotes_found": [faith_ok, len(used)], "numbers_supported": [num_ok, num_tot],
         "primary_studies": studies, "primary_studies_floor": floor,
         "study_names_known": len(names)}

    def r(k):
        a, b = c[k]
        return a / b if b else None
    parts = {k: r(k) for k in ("key_groups", "quotes_found", "numbers_supported")}
    live = {k: v for k, v in parts.items() if v is not None}
    gate = None if not floor else (studies >= floor)
    return {"version": SCORE_VERSION, "case": case.get("id"), "lake": str(lake),
            "corroboration_gate": gate,
            "score": round(sum(live.values()) / len(live), 4) if live else None,
            "parts": {k: round(v, 4) for k, v in live.items()}, "counts": c, "points": len(points)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--case")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cases = json.loads(Path(a.cases).read_text())["cases"]
    t = Path(a.target)
    jobs = []
    if (t / "synthesis.json").exists():
        c = next((x for x in cases if x["id"] == a.case), None)
        if not c:
            sys.exit(f"--case required and must be one of: {', '.join(x['id'] for x in cases)}")
        jobs.append((t, c))
    else:
        for d in sorted(x for x in t.iterdir() if x.is_dir()):
            if not (d / "synthesis.json").exists():
                continue
            c = next((x for x in cases if norm(x["question"])[:40] in norm(
                json.loads((d / "plan.json").read_text()).get("question", ""))), None)
            if c:
                jobs.append((d, c))
            else:
                print(f"no case matches {d.name}", file=sys.stderr)
    out = [score_lake(d, c) for d, c in jobs]
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        for r in out:
            g = r.get("corroboration_gate")
            print(f"{r['case']:24} score {r['score']}  gate {'-' if g is None else ('pass' if g else 'FAIL')}  " +
                  "  ".join(f"{k} {v}" for k, v in r["parts"].items()))
            print(f"{'':24} {r['counts']}")
        if len(out) > 1:
            vals = [r["score"] for r in out if r["score"] is not None]
            print(f"{'MEAN':24} {round(sum(vals)/len(vals), 4)}  over {len(vals)} cases")


if __name__ == "__main__":
    main()
