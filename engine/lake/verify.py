"""Deterministic checks. No model calls here: everything is string matching and counting."""
import difflib, re, unicodedata

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         "−": "-", " ": " ", " ": " ", " ": " "})


def norm(s: str) -> str:
    """Lowercase, unify quotes/dashes/spaces, drop citation brackets, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES).lower()
    s = re.sub(r"\[\d+(?:[,–-]\s*\d+)*\]", " ", s)   # [12], [3-5], [1,2]
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .;,")


def find_quote(quote: str, passages: dict, prefer: str | None = None, fuzzy_min: float = 0.92):
    """Locate a quote in the stored passages.
    Returns (passage_id, "exact" | "fuzzy", ratio) or (None, "missing", best_ratio)."""
    q = norm(quote)
    if len(q) < 20:
        return None, "too_short", 0.0
    order = ([prefer] if prefer in passages else []) + [p for p in passages if p != prefer]
    for pid in order:
        if q in norm(passages[pid]):
            return pid, "exact", 1.0
    best = (None, 0.0)
    for pid in order:
        text = norm(passages[pid])
        if not text:
            continue
        sm = difflib.SequenceMatcher(None, q, text, autojunk=False)
        # best-matching window of the quote's length around the longest common block
        m = sm.find_longest_match(0, len(q), 0, len(text))
        start = max(0, m.b - m.a)
        window = text[start:start + len(q) + 10]
        r = difflib.SequenceMatcher(None, q, window, autojunk=False).ratio()
        if r > best[1]:
            best = (pid, r)
    if best[1] >= fuzzy_min:
        return best[0], "fuzzy", round(best[1], 3)
    return None, "missing", round(best[1], 3)


# ── claim vs quote (does the paraphrase add anything the quote does not say?) ──
# thousands may be grouped with commas, spaces or thin spaces ("17,000", "17 000", "69\u2009516")
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[,\u00a0\u2009\u202f ]\d{3})+(?![\d.])|\d+(?:\.\d+)?)(\s?%)?")
NAME_RE = re.compile(r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b")   # scDblFinder, DoubletFinder, SoupX
OWN_RE = re.compile(r"\b(their|its|the authors'?)\s+(own|new)\s+(method|tool|algorithm|approach|model|software|package)", re.I)


def numbers(text: str) -> set:
    out = set()
    # Lancet journals write decimals with a middle dot (0·72). Read as "0" and "72" it made 9 of
    # 17 number-check hits in the PD-1 lake false (28 Oct).
    text = re.sub(r"(?<=\d)\u00b7(?=\d)", ".", text or "")
    for m in NUM_RE.finditer(text or ""):
        v = re.sub(r"[,\u00a0\u2009\u202f ]", "", m.group(1))
        if re.fullmatch(r"(19|20)\d\d", v):   # years are not measurements
            continue
        out.add(v.rstrip("0").rstrip(".") if "." in v else v)
    return out


def claim_issues(claim: str, quote: str, title: str = "", evidence_type: str = "") -> list:
    issues = []
    ctx = (quote + " " + (title or "")).lower()
    names = sorted({n for n in NAME_RE.findall(claim) if n.lower() not in ctx})
    if names:
        issues.append(f"names something the quote does not mention: {', '.join(names)}")
    extra = sorted(numbers(claim) - numbers(quote + " " + (title or "")))
    if extra:
        issues.append(f"has numbers the quote does not contain: {', '.join(extra)}")
    if evidence_type != "new_method" and OWN_RE.search(claim):
        issues.append("calls a method the authors' own, but this paper is not presenting its own method")
    return issues


# ── premise identity: does a returned record actually BE the named thing? ────
# "hits > 0" answered only whether the search returned something, which it almost always does.
PREMISE_STOP = {"trial", "trials", "study", "studies", "benchmark", "benchmarks", "dataset",
                "datasets", "method", "methods", "tool", "tools", "analysis", "paper", "papers",
                "data", "cohort", "consortium", "project", "database", "atlas", "pipeline",
                "software", "package", "version", "the", "of", "and", "for", "in", "on", "et", "al"}
# OptiMEL, T-VEC, G207, scDblFinder, OPTiM: short but unmistakable, so keep them whatever the length
STRONG_RE = re.compile(r"[A-Za-z][a-z]*[A-Z0-9]|[A-Z]{2,}|\d")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")


def premise_tokens(name: str) -> list:
    """The parts of a premise name worth testing for, lowercased, in order, deduplicated."""
    out = []
    for t in WORD_RE.findall(name or ""):
        if re.fullmatch(r"(19|20)\d\d", t) or t.lower() in PREMISE_STOP:
            continue
        if len(t) >= 4 or STRONG_RE.search(t):
            out.append(t.lower())
    return list(dict.fromkeys(out))


def premise_author_year(name: str):
    """('surname', 'YYYY') for names shaped like 'Squair 2021' or 'Xi & Li 2021', else None."""
    yr = re.search(r"\b((?:19|20)\d\d)\b", name or "")
    if not yr:
        return None
    sur = [t for t in WORD_RE.findall(name) if t[:1].isupper() and len(t) >= 3
           and t.lower() not in PREMISE_STOP and not re.fullmatch(r"(19|20)\d\d", t)]
    return (sur[0].lower(), yr.group(1)) if sur else None


def premise_verdict(name: str, records: list, near: float = 0.82) -> dict:
    """-> {"verdict": found|near_miss|not_found|unclear, "why": str, "matched": {...} (optional)}.

    Only "not_found" should drive the report's missing-premise statement: a near_miss is a naming
    problem, not an absent thing, and "unclear" means this check could not adjudicate at all."""
    toks, ay = premise_tokens(name), premise_author_year(name)
    if not records:
        return {"verdict": "not_found", "why": "the search returned no records"}
    if not toks and not ay:
        return {"verdict": "unclear", "why": f"'{name}' carries no distinctive name to test for"}

    def _m(r):
        return {"title": r.get("title"), "year": r.get("pub_year"), "doi": r.get("doi")}

    best_partial = []
    for r in records:
        title = norm(r.get("title"))
        carried = [t for t in toks if t in title]
        # One distinctive name is testable: carrying it IS the match. A multi-word description
        # is not a name, and carrying some of its words proves nothing. 27 Oct, ACE2 run:
        # 'TMPRSS2 role in SARS-CoV-2 entry' returned FOUND because a title carried
        # 'sars-cov-2' and 'entry' - TMPRSS2, the term under test, was never matched.
        if carried and (len(toks) == 1 or len(carried) == len(toks)):
            return {"verdict": "found", "why": f"a returned title carries {', '.join(carried)}", "matched": _m(r)}
        if len(carried) > len(best_partial):
            best_partial = carried
        if ay:
            authors = " ".join(r.get("authors") or []).lower()
            if ay[0] in authors and str(r.get("pub_year") or "") == ay[1]:
                return {"verdict": "found", "why": f"author {ay[0]} and year {ay[1]} match", "matched": _m(r)}
    if len(toks) > 1:
        missed = [t for t in toks if t not in best_partial]
        return {"verdict": "unclear",
                "why": f"'{name}' is a description, not a name this check can test: no returned title "
                       f"carries all of {', '.join(toks)}"
                       + (f" (closest carried {', '.join(best_partial)}; never {', '.join(missed)})"
                          if best_partial else "")}
    # Nothing carried the name. Is something close enough that the name is probably misspelled?
    best = (0.0, None, None, None)
    for r in records:
        for w in set(re.findall(r"[a-z0-9][a-z0-9'-]{2,}", norm(r.get("title")))):
            for t in toks:
                ratio = difflib.SequenceMatcher(None, t, w).ratio()
                if ratio > best[0]:
                    best = (ratio, t, w, r)
    if best[0] >= near:
        return {"verdict": "near_miss", "matched": _m(best[3]),
                "why": f"no record carries '{best[1]}'; the closest word in a returned title is "
                       f"'{best[2]}' (similarity {best[0]:.2f}) -- the premise may be misnamed"}
    return {"verdict": "not_found",
            "why": f"none of the {len(records)} records examined carries {', '.join(toks) or 'the name'}"}
