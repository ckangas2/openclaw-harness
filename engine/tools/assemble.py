#!/usr/bin/env python3
"""Build agent instruction files from the engine templates plus the active packs.

usage:
  assemble.py auditor --repo DIR --packs scrna-qc[,other] --user NAME            > AGENTS.md
  assemble.py worker  --repo DIR --packs scrna-qc[,other] --user NAME \
                      --env-name ENV --check-py PATH                               > block.md

Engine rules live in engine/agents/*; domain knowledge lives in packs/<name>/.
Packs are applied in the order given. Output goes to stdout.
"""
import argparse
import json
import re
import sys
from pathlib import Path


def sections(text: str):
    """Split '### Title' sections, dropping HTML comments."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    parts = re.split(r"(?m)^### ", text)[1:]
    out = []
    for p in parts:
        title, _, body = p.partition("\n")
        out.append((title.strip(), body.rstrip() + "\n"))
    return out


def load_pack(repo: Path, name: str):
    d = repo / "packs" / name
    meta = json.loads((d / "pack.json").read_text())
    return d, meta


def auditor(repo: Path, packs, user: str) -> str:
    tmpl = (repo / "engine/agents/auditor/AGENTS.template.md").read_text()
    principles, domains, premise = [], [], ""
    for name in packs:
        d, meta = load_pack(repo, name)
        if meta.get("domain"):
            domains.append(meta["domain"])
        if not premise and meta.get("premiseHint"):
            premise = meta["premiseHint"]
        f = d / "auditor-principles.md"
        if f.exists():
            principles += sections(f.read_text())
    principles += sections((repo / "engine/agents/auditor/principles.md").read_text())
    body = "\n".join(f"### {i}. {t}\n{b}" for i, (t, b) in enumerate(principles, 1))
    out = (tmpl.replace("{{PRINCIPLES}}\n", body + "\n")
               .replace("{{N_PRINCIPLES}}", str(len(principles)))
               .replace("{{DOMAIN}}", " and ".join(domains) or "scientific analysis")
               .replace("{{PREMISE_UNIT}}", f" ({premise})" if premise else "")
               .replace("{{USER}}", user))
    left = re.findall(r"\{\{\w+\}\}", out)
    if left:
        sys.exit(f"unfilled placeholders: {left}")
    return out


def worker(repo: Path, packs, user: str, env_name: str, check_py: str) -> str:
    out = (repo / "engine/agents/worker/harness-note.md").read_text()
    for name in packs:
        d, _ = load_pack(repo, name)
        f = d / "worker-notes.md"
        if f.exists():
            out = out.rstrip() + "\n\n" + re.sub(r"<!--.*?-->\n?", "", f.read_text(), flags=re.S).strip() + "\n"
    out = out.replace("{{USER}}", user).replace("{{ENV_NAME}}", env_name).replace("{{CHECK_PY}}", check_py)
    left = re.findall(r"\{\{\w+\}\}", out)
    if left:
        sys.exit(f"unfilled placeholders: {left}")
    return out


ap = argparse.ArgumentParser()
ap.add_argument("what", choices=["auditor", "worker"])
ap.add_argument("--repo", required=True)
ap.add_argument("--packs", required=True)
ap.add_argument("--user", default="the user")
ap.add_argument("--env-name", default="")
ap.add_argument("--check-py", default="")
a = ap.parse_args()
repo = Path(a.repo)
packs = [p for p in a.packs.split(",") if p]
if a.what == "auditor":
    sys.stdout.write(auditor(repo, packs, a.user))
else:
    sys.stdout.write(worker(repo, packs, a.user, a.env_name, a.check_py))
