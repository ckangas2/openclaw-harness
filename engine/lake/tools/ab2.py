#!/usr/bin/env python3
"""A/B two ollama models, each with the GPU to itself. Read-only.

    python3 ab2.py MODEL_A MODEL_B [--levels 1,2,4,6] [--predict 300] [--reps 2]

Only one model is resident at a time: everything is unloaded, the model under test is
loaded and warmed, its levels are measured, then it is unloaded again. The pair runs
twice in opposite order so drift hits both. Per-block it reports the model's own
reported generation rate, so a change in speculative decoding is visible rather than
inferred. Nothing is written; all models are left unloaded at the end.
"""
import argparse, json, statistics, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = "Count from 1 to 300. Output one number per line and nothing else."


def post(url, path, payload, timeout=900):
    req = urllib.request.Request(f"{url}{path}", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def loaded(url):
    try:
        with urllib.request.urlopen(f"{url}/api/ps", timeout=30) as r:
            return [m["name"] for m in (json.load(r).get("models") or [])]
    except Exception:
        return []


def unload_all(url):
    for m in loaded(url):
        try:
            post(url, "/api/generate", {"model": m, "keep_alive": 0}, timeout=120)
        except Exception as e:
            print(f"  (could not unload {m}: {type(e).__name__})")
    time.sleep(2)


def call(url, model, predict):
    t = time.time()
    d = post(url, "/api/generate",
             {"model": model, "prompt": PROMPT, "stream": False, "think": False,
              "options": {"temperature": 0, "seed": 42, "num_predict": predict}})
    wall = time.time() - t
    ed, ec = d.get("eval_duration") or 0, d.get("eval_count") or 0
    return wall, ec, (ec / (ed / 1e9) if ed else 0.0)      # ollama's own gen rate


def block(url, model, levels, predict):
    unload_all(url)
    for _ in range(2):
        w, ec, rate = call(url, model, 32)
    print(f"  loaded+warm {model:28} ollama-reported gen rate {rate:6.1f} tok/s   "
          f"resident: {', '.join(loaded(url)) or 'none'}")
    out = {}
    for n in levels:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            rs = list(ex.map(lambda _: call(url, model, predict), range(n)))
        wall = time.time() - t0
        tok = sum(x[1] for x in rs)
        out[n] = {"thr": tok / wall, "each": tok // n, "wall": wall,
                  "gen": statistics.median(x[2] for x in rs),
                  "med": statistics.median(x[0] for x in rs)}
        f = "  EARLY STOP" if out[n]["each"] < predict * 0.8 else ""
        print(f"    N={n:<2} {wall:>7.2f}s  {out[n]['thr']:>7.1f} tok/s aggregate  "
              f"gen {out[n]['gen']:>6.1f} tok/s  {out[n]['each']:>4} tok each{f}")
    unload_all(url)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a"); ap.add_argument("b")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--levels", default="1,2,4,6")
    ap.add_argument("--predict", type=int, default=300)
    ap.add_argument("--reps", type=int, default=2)
    o = ap.parse_args()
    models, levels = [o.a, o.b], [int(x) for x in o.levels.split(",")]
    res = {m: {n: [] for n in levels} for m in models}
    for rep in range(o.reps):
        for m in (models if rep % 2 == 0 else models[::-1]):
            print(f"\nrepetition {rep + 1}: {m} (alone on the GPU)")
            for n, r in block(o.url, m, levels, o.predict).items():
                res[m][n].append(r)
    print(f"\n{'model':28} " + "  ".join(f"{'N=' + str(n):>12}" for n in levels))
    best = {}
    for m in models:
        agg = [statistics.median(x["thr"] for x in res[m][n]) for n in levels]
        gen = [statistics.median(x["gen"] for x in res[m][n]) for n in levels]
        best[m] = max(agg)
        print(f"{m:28} " + "  ".join(f"{a:>8.1f} agg" for a in agg))
        print(f"{'':28} " + "  ".join(f"{g:>8.1f} gen" for g in gen))
        print(f"{'':28} " + "  ".join(f"{a / agg[0]:>9.2f}x" for a in agg))
    wa, wb = best[models[0]], best[models[1]]
    hi, lo = (models[0], models[1]) if wa >= wb else (models[1], models[0])
    print(f"\nbest aggregate: {models[0]} {wa:.1f} vs {models[1]} {wb:.1f} tok/s"
          f"  ->  {hi} is {max(wa, wb) / max(min(wa, wb), 1e-9):.2f}x {lo}")
    print("'gen' is ollama's own eval_count/eval_duration: if it collapses between runs,")
    print("speculative decoding stopped working and the comparison is not about batching.")


if __name__ == "__main__":
    main()
