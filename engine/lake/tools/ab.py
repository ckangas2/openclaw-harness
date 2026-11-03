#!/usr/bin/env python3
"""Honest A/B of two ollama models under identical conditions. Read-only.

    python3 ab.py MODEL_A MODEL_B [--levels 1,2,4,6] [--predict 300] [--reps 2]

Controls the confounds that make a naive sweep lie:
  * both models are loaded and warmed BEFORE any timing, so neither pays a cold start
  * measurements alternate A,B at each level, so thermal or background drift hits both
  * the order flips on the second repetition
  * generated-token counts are printed, so an early stop cannot masquerade as speed
Nothing is written; the models are left loaded.
"""
import argparse, json, statistics, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = "Count from 1 to 300. Output one number per line and nothing else."


def call(url, model, predict, timeout=900):
    body = json.dumps({"model": model, "prompt": PROMPT, "stream": False, "think": False,
                       "options": {"temperature": 0, "seed": 42, "num_predict": predict}}).encode()
    req = urllib.request.Request(f"{url}/api/generate", body, {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return time.time() - t, (d.get("eval_count") or 0)


def level(url, model, n, predict):
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as ex:
        rs = list(ex.map(lambda _: call(url, model, predict), range(n)))
    wall = time.time() - t0
    tok = sum(x[1] for x in rs)
    return {"wall": wall, "tok": tok, "thr": tok / wall if wall else 0,
            "each": tok // n, "med": statistics.median(x[0] for x in rs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a"); ap.add_argument("b")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--levels", default="1,2,4,6")
    ap.add_argument("--predict", type=int, default=300)
    ap.add_argument("--reps", type=int, default=2)
    o = ap.parse_args()
    models = [o.a, o.b]
    levels = [int(x) for x in o.levels.split(",")]

    print("warming both models (not timed)")
    for m in models:
        for _ in range(2):
            secs, tok = call(o.url, m, 32)
        print(f"  {m:28} warm ({tok} tokens, {secs:.2f}s)")

    res = {m: {n: [] for n in levels} for m in models}
    for rep in range(o.reps):
        order = models if rep % 2 == 0 else models[::-1]
        print(f"\nrepetition {rep + 1}, order: {' then '.join(order)}")
        for n in levels:
            for m in order:
                r = level(o.url, m, n, o.predict)
                res[m][n].append(r)
                flag = "  EARLY STOP" if r["each"] < o.predict * 0.8 else ""
                print(f"  N={n:<2} {m:28} {r['wall']:>7.2f}s  {r['thr']:>7.1f} tok/s  "
                      f"{r['each']:>4} tok each  med {r['med']:>6.2f}s{flag}")

    print(f"\n{'':4} {'model':28} {'tok/s (median of reps)':>24}   scaling")
    best = {}
    for m in models:
        base = statistics.median(x["thr"] for x in res[m][levels[0]])
        row = []
        for n in levels:
            thr = statistics.median(x["thr"] for x in res[m][n])
            row.append(thr)
            best[m] = max(best.get(m, 0), thr)
        print(f"     {m:28} " + "  ".join(f"N={n}:{t:>7.1f}" for n, t in zip(levels, row)))
        print(f"     {'':28} " + "  ".join(f"  {t/base:>6.2f}x" for t in row))
    wa, wb = best[models[0]], best[models[1]]
    win, lose = (models[0], models[1]) if wa >= wb else (models[1], models[0])
    print(f"\nbest aggregate throughput: {models[0]} {wa:.1f} vs {models[1]} {wb:.1f} tok/s")
    print(f"-> {win} is {max(wa, wb) / max(min(wa, wb), 1e-9):.2f}x {lose}")


if __name__ == "__main__":
    main()
