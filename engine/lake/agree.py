"""Measure a judge against hand labels. Reports per question and for 'any problem'.

  python3 engine/lake/agree.py data/labels-1.jsonl <lake_root> --judge judge-2-nothink-s42
  python3 engine/lake/agree.py data/labels-1.jsonl <lake_root> --judge A --judge B   (compare arms)

Positives are few, so a CI is printed with every rate and the known-error list is printed in full:
a judge that scores well on average but misses the errors we built the cases for is not usable.
"""
import argparse, hashlib, json, math
from pathlib import Path

Q = ["unsupported", "misattributed", "unscoped", "overstated"]
# A labelled question -> the judge questions that may answer it. judge-4 split misattributed.
MAP = {"misattributed": ["misattributed", "wrong_study", "wrong_population", "wrong_comparator"]}


def read_jsonl(p):
    return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]


def split(lake, pid):
    """Deterministic half, so dev/test never shifts between runs."""
    h = hashlib.sha1(f"{lake}|{pid}".encode()).hexdigest()
    return "dev" if int(h[:8], 16) % 2 == 0 else "test"


def wilson(k, n):
    if not n:
        return None
    p = k / n
    z = 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    hw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - hw), min(1.0, c + hw)


def fmt(k, n):
    w = wilson(k, n)
    return "    n=0    " if not w else f"{k}/{n} = {w[0]:.2f} [{w[1]:.2f},{w[2]:.2f}]"


def load_judge(root, tag):
    out = {}
    for f in sorted(Path(root).glob(f"*/{tag}.jsonl")):
        for r in read_jsonl(f):
            if "error" in r.get("judge", {}):
                continue
            out[(f.parent.name, r["pid"])] = r["judge"]
    return out


def report(labels, j, tag, arm="all"):
    rows = [(k, v) for k, v in labels.items() if k in j and (arm == "all" or split(*k) == arm)]
    print(f"\n=== {tag}   {arm}   {len(rows)} points judged of {len(labels)} labelled")
    if not rows:
        return
    print(f"{'question':14} {'TPR (caught)':28} {'TNR (left alone)':28} judge-yes  unclear")
    for q in Q + ["ANY"]:
        tp = fn = tn = fp = unc = jy = 0
        for k, lab in rows:
            want = (any(v == "yes" for v in lab.values()) if q == "ANY" else lab[q] == "yes")
            if q == "ANY":
                got = any((v or {}).get("answer") == "yes"
                          for v in j[k].values() if isinstance(v, dict))
                u = False
            else:
                qs = [x for x in MAP.get(q, [q]) if x in j[k]]
                answers = [(j[k][x] or {}).get("answer") for x in qs]
                got = "yes" in answers
                u = not got and "unclear" in answers
            jy += got
            unc += u
            if want and got: tp += 1
            elif want and not got: fn += 1
            elif not want and got: fp += 1
            else: tn += 1
        print(f"{q:14} {fmt(tp, tp+fn):28} {fmt(tn, tn+fp):28} {jy:^9} {unc:^8}")
    print("  known problems, one line each:")
    for k, lab in sorted(rows):
        if not any(v == "yes" for v in lab.values()):
            continue
        mine = [q for q in Q if lab[q] == "yes"]
        theirs = [q for q, v in j[k].items() if isinstance(v, dict) and v.get("answer") == "yes"]
        mark = "CAUGHT " if theirs else "MISSED "
        print(f"    {mark}{k[0][:14]:14} {k[1]:6} mine={','.join(mine):28} judge={','.join(theirs) or '-'}")
    def yeses(k):
        return [q for q, v in j[k].items() if isinstance(v, dict) and v.get("answer") == "yes"]
    fp_rows = [(k, yeses(k)) for k, lab in sorted(rows)
               if not any(v == "yes" for v in lab.values()) and yeses(k)]
    print(f"  false alarms: {len(fp_rows)}")
    for k, qs in fp_rows[:12]:
        print(f"    {k[0][:14]:14} {k[1]:6} judge={','.join(qs)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("labels")
    ap.add_argument("root")
    ap.add_argument("--judge", action="append", required=True)
    ap.add_argument("--arm", default="all", choices=["all", "dev", "test"])
    a = ap.parse_args()
    labels = {(r["lake"], r["pid"]): r["label"] for r in read_jsonl(a.labels)}
    for tag in a.judge:
        j = load_judge(a.root, tag)
        if not j:
            print(f"\n=== {tag}: no files matched {a.root}/*/{tag}.jsonl")
            continue
        report(labels, j, tag, a.arm)
