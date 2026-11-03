#!/usr/bin/env python3
"""Concurrency sweep against a local ollama. Read-only: generates text, writes nothing.

    python3 sweep.py [model] [--url http://localhost:11434] [--predict 200]

Fires N identical requests at once for N in 1,2,4,6,8 and reports aggregate throughput.
Generation is memory-bandwidth-bound at batch 1, so if the GPU has headroom the wall time
stays roughly flat as N rises - that flatness IS the speedup. If wall time climbs linearly
with N, the GPU is already saturated and threading the screening loop will not help.
"""
import argparse, json, statistics, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = ("List three barriers to intravenous delivery of oncolytic viruses, "
          "one per line, no preamble.")


def one(url, model, predict, timeout=600):
    body = json.dumps({
        "model": model, "prompt": PROMPT, "stream": False, "think": False,
        "options": {"temperature": 0, "seed": 42, "top_p": 1, "num_predict": predict},
    }).encode()
    req = urllib.request.Request(f"{url}/api/generate", body, {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return {"secs": time.time() - t, "out": d.get("eval_count") or 0,
            "prompt_tok": d.get("prompt_eval_count") or 0,
            "text": d.get("response") or "", "thinking": d.get("thinking") or ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default="qwen-q8-32k")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--predict", type=int, default=200)
    ap.add_argument("--levels", default="1,2,4,6,8")
    a = ap.parse_args()

    probe = one(a.url, a.model, 64)
    print(f"model {a.model}   warm-up: {probe['out']} output tokens, "
          f"response {len(probe['text'])} chars, thinking {len(probe['thinking'])} chars")
    if not probe["text"] and not probe["thinking"]:
        sys.exit("empty response and empty thinking - check the model name and that ollama is up")
    print(f"\n{'N':>3} {'wall s':>8} {'tok/s total':>12} {'tok/s each':>11} {'median req s':>13}  vs N=1")
    base = None
    for n in [int(x) for x in a.levels.split(",")]:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            rs = list(ex.map(lambda _: one(a.url, a.model, a.predict), range(n)))
        wall = time.time() - t0
        tok = sum(r["out"] for r in rs)
        thr = tok / wall if wall else 0
        base = base or thr
        print(f"{n:>3} {wall:>8.2f} {thr:>12.1f} {thr/n:>11.1f} "
              f"{statistics.median(r['secs'] for r in rs):>13.2f}  {thr/base:>5.2f}x")
    print("\nThe last column is aggregate throughput relative to one request at a time.")
    print("That multiple is what threading the screening loop would buy.")


if __name__ == "__main__":
    main()
