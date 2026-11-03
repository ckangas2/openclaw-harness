"""Frozen judge for evidence-lake points. Pinned at JUDGE_VERSION, model and prompt included.

Cross-family by design: the lake writes with Qwen, so the judge is not Qwen. A same-family judge
shares the writer's blind spots, and under tuning the pipeline learns whatever the judge accepts.
Four categorical questions per point, never a scale (E3). Every "yes" must copy the words of the
point it objects to; code checks they are there, and a "yes" whose words are not in the point is
counted "unclear". Not trusted for tuning until its agreement with hand labels is measured.

  python3 engine/lake/judge.py run   <lake_or_root> [--limit N]
  python3 engine/lake/judge.py sheet <lake_or_root> --out labels.jsonl
"""
import argparse, hashlib, json, os, re, sys, time, unicodedata, urllib.request
from pathlib import Path

JUDGE_VERSION = "judge-3"
MODEL = "gemma4:31b"
OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 16384}
QUESTIONS = ["unsupported", "misattributed", "unscoped", "overstated"]

SYSTEM = """You are checking one sentence from an evidence report against the evidence it cites. You see only the sentence and the cited items. Judge the sentence against these items only, not against what you know about the topic.

Answer four questions. Answer "yes" if there is a problem, "no" if there is none, and "unclear" if the items do not let you tell.

1. unsupported: Does the sentence state something that none of the cited quotes say? Paraphrase is fine. New facts, numbers, or conclusions are not.
2. misattributed: Does the sentence attach a finding to the wrong study, trial, population, comparator, or dose? Use the paper details, each paper's abstract, and the table rows shown with each quote.
3. unscoped: Is the evidence limited (animal or cell studies only, a single study, a post-hoc or secondary analysis, or an interim result) without the sentence saying so? Naming the source counts as marking a relayed finding: if the sentence says "as reported by X", "a review citing X", or "X noted that", the relay is marked and this is not a problem. Judge only the limitations of the study itself.
4. overstated: Does the sentence claim more than the cited study tested? Examples: superiority that the study says it was not powered to test, a cause drawn from an association, a general rule drawn from one model or cohort.

For each question, first write one sentence in "why" saying what you checked and what you found. Then fill "words": for a problem, the exact words of the sentence that have it; otherwise leave it empty. Give the answer last."""

ANSWER = {"type": "object", "properties": {
    "why": {"type": "string"}, "words": {"type": "string"},
    "answer": {"type": "string", "enum": ["yes", "no", "unclear"]}},
    "required": ["why", "words", "answer"]}
SCHEMA = {"type": "object", "properties": {q: ANSWER for q in QUESTIONS}, "required": QUESTIONS}
TYPE = {"independent_comparison": "independent comparison", "new_method": "method paper (own evaluation)",
        "primary_study": "primary study", "review": "review"}
# No-think by default. 28 Oct, judge-3 on 63 points: think cost 30-42s per point against 6, and
# flagged FEWER of the hand-labelled problems (PD-1 2 of 16 against 4; labels say 5). A 50-trial
# study would be ~31h with thinking against ~6h without. JUDGE_THINK=1 to re-test.
THINK = os.environ.get("JUDGE_THINK", "0") != "0"
OPTIONS["seed"] = int(os.environ.get("JUDGE_SEED", OPTIONS["seed"]))
PROMPT_HASH = hashlib.sha1((MODEL + f"think={THINK}" + json.dumps(OPTIONS, sort_keys=True) + SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:10]


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\u2018\u2019]", "'", s); s = re.sub(r"[\u201c\u201d]", '"', s)
    s = re.sub(r"[\u2010-\u2015]", "-", s)
    return re.sub(r"\s+", " ", s).strip(" .;,\"'")


def read_jsonl(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def lakes(target):
    t = Path(target)
    if (t / "synthesis.json").exists():
        return [t]
    return sorted(d for d in t.iterdir() if d.is_dir() and (d / "synthesis.json").exists())


def points(lake):
    syn = json.loads((lake / "synthesis.json").read_text())
    out = []
    summ = syn.get("summary")
    if isinstance(summ, dict):
        for i, p in enumerate(summ.get("points", [])):
            if not p.get("dropped"):
                out.append((f"Sum.{i+1}", p))
    for sid, b in syn.get("subquestions", {}).items():
        for i, p in enumerate(b.get("points", [])):
            if not p.get("dropped"):
                out.append((f"{sid}.{i+1}", p))
    return out


def cards(lake, p):
    ev = {e["eid"]: e for e in read_jsonl(lake / "evidence.jsonl")}
    sel = {d.get("key"): d for d in (json.loads((lake / "selected.json").read_text()) or {}).get("docs", [])}
    # Each cited paper's abstract goes on the card. judge-1 marked "In the CheckMate 067 trial"
    # unsupported because the quote and title never name the trial; the abstract does (28 Oct).
    # The judge reads the name itself - no list of trial names, so it works in any field.
    recs = {r.get("key"): r for r in read_jsonl(lake / "records.jsonl")}
    out = []
    for x in p.get("eids", []):
        e = ev.get(x)
        if not e:
            continue
        d = sel.get(e.get("key"), {})
        first = (e.get("authors") or d.get("authors") or ["?"])[0]
        first = first.split()[0] if isinstance(first, str) and first else "?"
        kind = TYPE.get(e.get("evidence_type"), "other")
        if e.get("source") == "cited_work":
            kind += ", relaying another study's finding"
        s = f'[{x}] {first} {d.get("pub_year", "")}, {kind}: "{(e.get("title") or d.get("title") or "")[:140]}"\n'
        ab = (recs.get(e.get("key"), {}) or {}).get("abstract")
        if ab:
            s += f"    abstract: {ab[:1500]}\n"
        s += f'    section: {e.get("section", "")}\n    quote: "{e.get("quote", "")}"'
        if e.get("context"):
            s += f"\n    table row: {e['context']}"
        out.append(s)
    return "\n".join(out)


def ask(user, cache, cache_f):
    k = hashlib.sha1(f"{PROMPT_HASH}|{user}".encode()).hexdigest()
    if k in cache:
        return cache[k], True
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    host = host if host.startswith("http") else "http://" + host
    body = {"model": MODEL, "stream": False, "format": SCHEMA, "options": OPTIONS, "keep_alive": "10m", "think": THINK,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    last = None
    for _ in range(2):
        try:
            req = urllib.request.Request(host + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=600).read())
            ans = json.loads(r["message"]["content"])
            # Ollama truncates an oversized prompt silently (it drops the start - the instructions),
            # so every call records its real size.
            ans["_meta"] = {"prompt_tokens": r.get("prompt_eval_count"), "out_tokens": r.get("eval_count"),
                            "secs": round((r.get("total_duration") or 0) / 1e9, 1)}
            if all(q in ans for q in QUESTIONS):
                cache[k] = ans
                with open(cache_f, "a") as f:
                    f.write(json.dumps({"k": k, "a": ans}) + "\n")
                return ans, False
        except Exception as ex:
            last = ex
    return {"error": str(last or "bad json")}, False


def verify(ans, text):
    """A 'yes' must quote the point. If its words are not in the point, it is unverifiable."""
    t = norm(text)
    out = {}
    for q in QUESTIONS:
        a = dict(ans.get(q) or {})
        if a.get("answer") == "yes":
            w = norm(a.get("words"))
            if not w or w not in t:
                a["answer"], a["downgraded"] = "unclear", True
        out[q] = a
    return out


def run(target, limit=None):
    cache_d = Path(os.environ.get("JUDGE_CACHE", "data/judge-cache")); cache_d.mkdir(parents=True, exist_ok=True)
    cache_f = cache_d / f"{JUDGE_VERSION}-{PROMPT_HASH}.jsonl"
    cache = {r["k"]: r["a"] for r in read_jsonl(cache_f)}
    for lake in lakes(target):
        plan = json.loads((lake / "plan.json").read_text())
        rows, t0, hits = [], time.time(), 0
        for pid, p in points(lake)[:limit]:
            user = (f"Question the report answers: {plan.get('question', '')}\n\nSentence:\n{p['text']}\n\n"
                    f"Cited items:\n{cards(lake, p) or '(none)'}")
            ans, cached = ask(user, cache, cache_f)
            hits += cached
            rows.append({"pid": pid, "text": p["text"], "eids": p.get("eids", []), "meta": ans.get("_meta"),
                         "judge": verify(ans, p["text"]) if "error" not in ans else ans})
        out = lake / f"{JUDGE_VERSION}-{'think' if THINK else 'nothink'}-s{OPTIONS['seed']}.jsonl"
        out.write_text("".join(json.dumps(r) + "\n" for r in rows))
        ok = [r for r in rows if "error" not in r["judge"]]
        yes = {q: sum(1 for r in ok if r["judge"][q].get("answer") == "yes") for q in QUESTIONS}
        unc = {q: sum(1 for r in ok if r["judge"][q].get("answer") == "unclear") for q in QUESTIONS}
        down = sum(1 for r in ok for q in QUESTIONS if r["judge"][q].get("downgraded"))
        bad = sum(1 for r in ok if any(r["judge"][q].get("answer") == "yes" for q in QUESTIONS))
        print(f"{lake.name[:40]:40} points {len(rows)}  errors {len(rows)-len(ok)}  cached {hits}  "
              f"{time.time()-t0:.0f}s")
        print(f"{'':40} yes {yes}  unclear {unc}  downgraded {down}  "
              f"points with any problem {bad}/{len(ok)}")
        m = [r["meta"] for r in rows if r.get("meta")]
        if m:
            pt = max(x.get("prompt_tokens") or 0 for x in m)
            warn = "  WARNING: near the limit, may be truncated" if pt >= 0.9 * OPTIONS["num_ctx"] else ""
            print(f"{'':40} max prompt tokens {pt} of {OPTIONS['num_ctx']}{warn}  "
                  f"mean output tokens {sum(x.get('out_tokens') or 0 for x in m)/len(m):.0f}  "
                  f"mean secs {sum(x.get('secs') or 0 for x in m)/len(m):.1f}")
        print(f"{'':40} -> {out}")


def sheet(target, out):
    rows = []
    for lake in lakes(target):
        plan = json.loads((lake / "plan.json").read_text())
        for pid, p in points(lake):
            rows.append({"lake": lake.name, "pid": pid, "question": plan.get("question", ""),
                         "text": p["text"], "cited": cards(lake, p),
                         "label": {q: None for q in QUESTIONS}, "note": ""})
    Path(out).write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} points -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "sheet"])
    ap.add_argument("target")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default="labels.jsonl")
    a = ap.parse_args()
    run(a.target, a.limit) if a.cmd == "run" else sheet(a.target, a.out)
