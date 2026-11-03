#!/usr/bin/env python3
"""Build runs/<RUN_ID>/summary.md: one readable page per case."""
import json
import re
import sys
from pathlib import Path

run = Path(sys.argv[1])
KEEP_G = re.compile(r"prompt:|tool sessions_send|finalize|agent_end|warning|code-check|tool web_|tool fetch|tool search")
KEEP_A = re.compile(r"prompt|finalize|agent_end|tool ")


def final_text(path: Path) -> str:
    """Best effort: the longest text field in the CLI's JSON output, else the raw tail."""
    raw = path.read_text(errors="replace") if path.exists() else ""
    try:
        data = json.loads(raw)
    except Exception:
        return raw[-6000:]
    texts = []

    def walk(x, key=""):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and key in ("text", "reply", "message", "content", "output"):
            texts.append(x)

    walk(data)
    return max(texts, key=len) if texts else raw[-6000:]


def last_session_answer(path: Path) -> str:
    """Last substantive assistant text in the exported session transcript."""
    if not path.exists():
        return ""
    last = ""
    for line in path.read_text(errors="replace").splitlines():
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
        c = (c or "").strip()
        if len(c) > 40 and c not in ("REPLY_SKIP", "ANNOUNCE_SKIP"):
            last = c
    return last


def lines(path: Path, pattern) -> str:
    if not path.exists():
        return "(no log)"
    out = [l for l in path.read_text(errors="replace").splitlines() if pattern.search(l)]
    return "\n".join(out[-40:]) or "(nothing logged)"


rj = json.loads((run / "run.json").read_text()) if (run / "run.json").exists() else {}
parts = [f"# Eval run {run.name}\n", (f"Pack `{rj.get('pack')}` · engine commit `{rj.get('commit')}`" + (" (uncommitted changes)" if rj.get("dirty") else "") + "\n") if rj else ""]
rows = []
for d in sorted(p for p in run.iterdir() if p.is_dir()):
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
    glog = (d / "grounded.log").read_text(errors="replace") if (d / "grounded.log").exists() else ""
    alog = (d / "audit-gate.log").read_text(errors="replace") if (d / "audit-gate.log").exists() else ""
    end = re.findall(r"agent_end run=\S+ .*?audits=(\d+) verdict=(\S+(?: \(\w+\))?)", glog)
    audits, verdict = (end[-1] if end else ("-", "-"))
    files = sorted(str(p.relative_to(d / "files")) for p in (d / "files").rglob("*") if p.is_file())
    rows.append(f"| {d.name} | {meta.get('exit', '?')} | {meta.get('seconds', '?')}s | {audits} | {verdict} | "
                f"{'yes' if re.search(r'warning (appended|queued)|round cap reached', glog) else ''} | {len(files)} |")
    parts.append(f"\n---\n\n## {d.name}\n")
    parts.append(f"**Prompt:**\n\n{(d / 'prompt.md').read_text().strip()}\n")
    parts.append(f"\n**Exit:** {meta.get('exit', '?')} · **Time:** {meta.get('seconds', '?')}s · "
                 f"**Session:** `{meta.get('session', '?')}`\n")
    parts.append(f"\n**Grounded log (grounded-pig):**\n\n```\n{lines(d / 'grounded.log', KEEP_G)}\n```\n")
    parts.append(f"\n**Audit-gate log (auditor):**\n\n```\n{lines(d / 'audit-gate.log', KEEP_A)}\n```\n")
    if files:
        parts.append("\n**Files written:** " + ", ".join(f"`{f}`" for f in files) + "\n")
    cli = final_text(d / 'reply.json').strip()
    last = last_session_answer(d / 'agent' / 'events.jsonl')
    trivial = len(cli) < 200 or cli.lower().startswith(("no response requested", "reply_skip"))
    if trivial and last and last.strip() != cli:
        parts.append("\n**Note:** the session continued after the CLI returned (e.g. an async command completed); "
                     "showing the session's last answer, with the CLI's reply below it.\n")
        parts.append(f"\n**Final answer (last in session):**\n\n{last.strip()}\n")
        parts.append(f"\n**CLI reply:**\n\n{cli}\n")
    else:
        parts.append(f"\n**Final answer:**\n\n{cli}\n")

table = ["\n| Case | Exit | Time | Audits | Final verdict | Warning | Files |",
         "|---|---|---|---|---|---|---|", *rows]
parts.insert(1, "\n".join(table) + "\n")
(run / "summary.md").write_text("\n".join(parts))
print(f"wrote {run / 'summary.md'}")
