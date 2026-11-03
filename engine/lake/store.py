"""Lake file helpers shared by all stages."""
import json, time
from pathlib import Path

HERE = Path(__file__).parent
PROTOCOL = "lake-0.9"


def read_json(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def write_json(p: Path, obj):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(p)


def read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def append_jsonl(p: Path, rows):
    with open(p, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _manifest_update_raw(lake: Path, stage: str, info: dict):
    m = read_json(lake / "manifest.json", {})
    m.setdefault("stages", {})[stage] = {"done": time.strftime("%Y-%m-%dT%H:%M:%S"), "protocol": PROTOCOL, **info}
    write_json(lake / "manifest.json", m)



# -- code stamp ------------------------------------------------------------
# Every stage records the code that ran it. A lake rebuilt in parts by different code versions is
# otherwise unreadable: the 28 Oct audit found manifest.scripts describing code that no longer
# existed, and error analysis needs single-vintage lakes (PRINCIPLES E5).
_STAMP = None


def _code_stamp() -> dict:
    global _STAMP
    if _STAMP is None:
        import hashlib, subprocess
        from pathlib import Path as _P
        here = _P(__file__).resolve().parent
        def git(*a):
            try:
                return subprocess.run(["git", "-C", str(here), *a], capture_output=True, text=True,
                                      timeout=10).stdout.strip()
            except Exception:
                return ""
        files = sorted(list(here.glob("*.py")) + list((here / "prompts").glob("*.md")))
        _STAMP = {"commit": git("rev-parse", "--short", "HEAD") or None,
                  "dirty": bool(git("status", "--porcelain", "--", ".")),
                  "files": {str(f.relative_to(here)): hashlib.sha256(f.read_bytes()).hexdigest()[:12]
                            for f in files}}
    return _STAMP


def manifest_update(lake, stage, info=None, *a, **k):
    info = dict(info or {})
    info["code"] = _code_stamp()
    return _manifest_update_raw(lake, stage, info, *a, **k)
