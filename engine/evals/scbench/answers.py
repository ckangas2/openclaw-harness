"""Find a case's benchmark answer in a run folder (no latch dependency, so it is testable)."""
import json
import re
from pathlib import Path

ANSWER_FILES = ("analysis_output.json", "eval_answer.json")
TAG_RE = re.compile(r"<EVAL_ANSWER>\s*(.*?)\s*</EVAL_ANSWER>", re.S)


def reply_texts(d: Path) -> list[str]:
    """Assistant messages from the transcript, then the final reply payloads (newest last)."""
    texts, transcript = [], []
    try:
        data = json.loads((d / "reply.json").read_text(errors="replace"))
        texts += [p["text"] for p in data.get("result", {}).get("payloads", []) or []
                  if isinstance(p, dict) and isinstance(p.get("text"), str)]
    except Exception:
        pass
    ev = d / "agent" / "events.jsonl"
    if ev.exists():
        for line in ev.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("source") != "transcript" or e.get("type") != "assistant.message":
                continue
            msg = e.get("data", {}).get("message", e.get("data", {}))
            c = msg.get("content")
            if isinstance(c, list):
                c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            if c:
                transcript.append(c)
    return transcript + texts


def parse_tag(text: str):
    for m in reversed(TAG_RE.findall(text or "")):
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", m.strip())
        try:
            v = json.loads(body)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


def find_answer(d: Path, eid: str):
    """(answer dict or None, where it came from)."""
    files = d / "files"
    if files.exists():
        for name in ANSWER_FILES:
            for f in sorted(files.rglob(name)):
                if f"/bench/{eid}/" in f.as_posix():
                    try:
                        v = json.loads(f.read_text())
                        if isinstance(v, dict):
                            return v, name
                    except json.JSONDecodeError:
                        pass
    for t in reversed(reply_texts(d)):
        v = parse_tag(t)
        if v is not None:
            return v, "reply <EVAL_ANSWER>"
    return None, "none"
