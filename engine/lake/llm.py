"""One narrow LLM call: prompt file + inputs + JSON schema -> parsed JSON.

Every call is logged to <lake>/calls.jsonl with the prompt file's hash, the inputs, the output,
the model settings, and (for yes/no-style tokens) the model's probabilities, so any result in a
lake can be traced to the exact prompt and settings that produced it.
"""
import hashlib, json, math, os, time, urllib.request
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("LAKE_MODEL", "qwen-q8:latest")
OPTIONS = {"temperature": 0, "seed": 42, "top_p": 1}
if os.environ.get("LAKE_NUM_CTX"):
    # Changing num_ctx reloads the model; leave unset to share the loaded model with OpenClaw.
    OPTIONS["num_ctx"] = int(os.environ["LAKE_NUM_CTX"])


def prompt_hash(name: str) -> str:
    return hashlib.sha256((PROMPTS / name).read_bytes()).hexdigest()[:12]


def render(name: str, inputs: dict) -> str:
    text = (PROMPTS / name).read_text()
    for k, v in inputs.items():
        text = text.replace("{{" + k + "}}", v if isinstance(v, str) else json.dumps(v, indent=1))
    return text


def _post(body: dict, timeout: int) -> dict:
    """Streamed chat call, reassembled into one reply.

    Ollama 0.33.3 returned one logprob per response regardless of streaming, which made prob_fields
    dead for every lake built before 25 Oct 2026 (n_logprobs == 1 in calls.jsonl). 0.40.11 returns
    per-token logprobs on /api/chat, streamed or not, so the streaming is no longer required for
    this - kept because it is harmless. Note /v1/chat/completions has no think:False and returns
    the thinking tokens' logprobs instead, so /api/chat is the right endpoint here."""
    req = urllib.request.Request(f"{OLLAMA}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    content, logprobs, last = [], [], {}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            content.append(chunk.get("message", {}).get("content", ""))
            logprobs += chunk.get("logprobs") or []
            last = chunk
    return {"message": {"content": "".join(content)}, "logprobs": logprobs,
            "eval_count": last.get("eval_count"), "prompt_eval_count": last.get("prompt_eval_count")}


def field_probs(logprobs, fields) -> list:
    """For each occurrence of a JSON field named in `fields`, the probability of the first token of its
    value, with the top alternatives. Works on the token stream, so it does not depend on how the
    tokenizer splits punctuation or on words like "true" appearing inside free text."""
    out, text, want = [], "", None
    for t in logprobs or []:
        tok = t.get("token", "")
        if want and tok.strip():
            out.append({"field": want, "token": tok.strip(), "p": round(math.exp(t["logprob"]), 4),
                        "alts": [(a["token"].strip(), round(math.exp(a["logprob"]), 4)) for a in t.get("top_logprobs", [])]})
            want = None
        text += tok
        tail = text[-60:].replace(" ", "")
        for f in fields:
            # String values open with a quote token, so matching both '"f":' and '"f":"' captured
            # two entries per string field (the quote, then the value). screen_batch's length guard
            # then discarded all of them, which is why p_type was always None. Match the quoted
            # form first and only once.
            if tail.endswith(f'"{f}":"') or (tail.endswith(f'"{f}":') and not tail.endswith('":"')):
                want = f
    return out


def call(lake: Path, step: str, prompt_file: str, inputs: dict, schema: dict, timeout: int = 900, post=None, prob_fields=()):
    """Run one structured call -> (parsed JSON, probabilities of the values of `prob_fields`).
    Raises ValueError if the reply is not valid JSON after one retry."""
    post = post or _post
    prompt = render(prompt_file, inputs)
    body = {"model": MODEL, "stream": True, "think": False, "format": schema, "options": OPTIONS,
            "logprobs": True, "top_logprobs": 3,
            "messages": [{"role": "user", "content": prompt}]}
    last_err = None
    for attempt in (1, 2):
        t0 = time.time()
        r = post(body, timeout)
        content = r.get("message", {}).get("content", "")
        try:
            out = json.loads(content)
            err = None
        except json.JSONDecodeError as e:
            out, err = None, f"bad JSON: {e}"
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step, "prompt": prompt_file,
               "prompt_hash": prompt_hash(prompt_file), "model": MODEL, "options": OPTIONS, "attempt": attempt,
               "secs": round(time.time() - t0, 1), "inputs": inputs, "output": out if out is not None else content,
               "error": err, "decisions": field_probs(r.get("logprobs"), prob_fields),
               "eval_count": r.get("eval_count"), "n_logprobs": len(r.get("logprobs") or []), "prompt_eval_count": r.get("prompt_eval_count")}
        with open(lake / "calls.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        if out is not None:
            return out, rec["decisions"]
        last_err = err
    raise ValueError(f"{step}: {last_err}")
