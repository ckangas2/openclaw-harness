#!/usr/bin/env python3
"""Apply four scoped fixes to the evidence lake. Idempotent; verifies before it writes.

    python3 apply_patches.py <repo-root> [--dry-run] [--only A,B,C,D]

A  lake.py      PLAN_SCHEMA caps queries at maxItems 2, but plan.md instructs the model to emit a
                THIRD query carrying PUB_TYPE:"Clinical Trial" whenever a sub-question is about
                outcomes or trials -- and calls that filter "the only reliable way to reach"
                individual trial reports past relevance ranking. The schema makes it impossible, so
                it has never fired. maxItems 2 -> 3.
B  lake.py      Query relaxation drops the LAST AND group when a query returns < RELAX_MIN hits.
                plan.md appends PUB_TYPE as the last AND term, so the first relaxation of a tagged
                query strips exactly the tag -- turning it back into a duplicate of an untagged
                query. Protect field-tagged groups: drop the last UNtagged group instead.
                (Latent until A lands, then load-bearing.)
C  evidence.py  ALWAYS_KEEP ({independent_comparison, review}) admits papers with NO quota, while
                K_PER_SUB caps everything else. In a review-dense field that starves selection:
                ohsv-iv-delivery had 96 no-quota papers for 20 slots, so all 20 went to reviews and
                every primary study was unreachable -- including Ikeda 1999 (254 cites, screened
                relevant on 3 sub-questions). Adds LAKE_ALWAYS_KEEP_PER_SUB. Default 0 = unlimited
                = today's behaviour, so this patch alone changes nothing until you set it.
D  cases-ohsv   ohsv-iv-delivery.scoring says "Streby 2019 AND at least one of Ikeda / Fulci", but
                eval.py flat-counts key_papers, so missing Streby and missing Fulci cost the same.
                Adds role/group so the requirement is machine-readable (lakemap.py reads it).
"""
import argparse, re, sys
from pathlib import Path

PATCHES = {}


def patch(name):
    def deco(fn):
        PATCHES[name] = fn
        return fn
    return deco


class Skip(Exception):
    pass


# ── A ───────────────────────────────────────────────────────────────────────
@patch("A")
def a(repo):
    p = repo / "engine/lake/lake.py"
    t = p.read_text()
    old = '"queries": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}},'
    new = '"queries": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "string"}},'
    if new in t:
        raise Skip("already maxItems 3")
    if t.count(old) != 1:
        raise RuntimeError(f"expected 1 occurrence of the queries schema line, found {t.count(old)}")
    return p, t.replace(old, new)


# ── B ───────────────────────────────────────────────────────────────────────
@patch("B")
def b(repo):
    p = repo / "engine/lake/lake.py"
    t = p.read_text()
    if "FIELD_TAG_RE" in t:
        raise Skip("relaxation already protects field tags")

    anchor = 'RELAX_MIN = int(os.environ.get("LAKE_RELAX_MIN", "30"))   # a query with fewer hits is relaxed'
    if anchor not in t:
        raise RuntimeError("could not find the RELAX_MIN definition")
    helper = anchor + '''
# plan.md appends PUB_TYPE:"Clinical Trial" as the LAST AND group of a tagged query, and that tag is
# the whole point of the query (relevance ranking buries individual trial reports under reviews of
# them). Relaxing by dropping the last group would strip exactly the tag, so drop the last UNtagged
# group instead. A query that is nothing but tags is left alone.
FIELD_TAG_RE = re.compile(r"\\b[A-Z][A-Z_]{2,}:")


def drop_group(groups: list) -> list:
    """Remove the last AND group that carries no field tag; [] if there is nothing safe to drop."""
    for i in range(len(groups) - 1, -1, -1):
        if not FIELD_TAG_RE.search(groups[i]):
            return groups[:i] + groups[i + 1:]
    return []'''
    t = t.replace(anchor, helper, 1)

    old = """            while first is not None and hits < RELAX_MIN and len(groups) > 2:
                groups = groups[:-1]
                level += 1"""
    new = """            while first is not None and hits < RELAX_MIN and len(groups) > 2:
                nxt = drop_group(groups)
                if len(nxt) == len(groups) or len(nxt) < 2:
                    break            # nothing droppable but the field tag: leave the query as it is
                groups = nxt
                level += 1"""
    if t.count(old) != 1:
        raise RuntimeError(f"expected 1 occurrence of the relaxation loop, found {t.count(old)}")
    return p, t.replace(old, new)


# ── C ───────────────────────────────────────────────────────────────────────
@patch("C")
def c(repo):
    p = repo / "engine/lake/evidence.py"
    t = p.read_text()
    if "ALWAYS_KEEP_PER_SUB" in t:
        raise Skip("always-keep quota already present")

    anchor = 'ALWAYS_KEEP = {"independent_comparison", "review"}'
    if anchor not in t:
        raise RuntimeError("could not find ALWAYS_KEEP")
    t = t.replace(anchor, anchor + '''
# ALWAYS_KEEP bypassed K_PER_SUB with no ceiling of its own. In a review-dense field that starves
# the MAX_DOCS cut: ohsv-iv-delivery (25 Oct eval) had 96 relevant reviews for 20 slots, so all 20
# went to reviews and no primary study was reachable -- Ikeda 1999 (254 cites, relevant on 3
# sub-questions) was dropped "below max_docs in the final sort". 0 = unlimited = old behaviour.
ALWAYS_KEEP_PER_SUB = int(os.environ.get("LAKE_ALWAYS_KEEP_PER_SUB", "0"))''', 1)

    old = """        n_other = 0
        for score, et, r, rec in scored:
            keep = et in ALWAYS_KEEP or r["key"] in multi_primary or n_other < K_PER_SUB
            if not keep:
                # A stage that discards records logs why it discarded each one. Screening does;
                # select did not, which cost an hour of guessing (24 Oct, d02).
                dropped.append({"key": r["key"], "sid": s["id"], "evidence_type": et,
                                "score": round(score, 3), "cited_by": rec.get("cited_by"),
                                "why": f"k_per_sub reached ({K_PER_SUB}) for {s['id']}"})
                continue
            if et not in ALWAYS_KEEP and r["key"] not in multi_primary:
                n_other += 1"""
    new = """        n_other = n_always = 0
        for score, et, r, rec in scored:
            always = et in ALWAYS_KEEP
            if always and ALWAYS_KEEP_PER_SUB and n_always >= ALWAYS_KEEP_PER_SUB:
                dropped.append({"key": r["key"], "sid": s["id"], "evidence_type": et,
                                "score": round(score, 3), "cited_by": rec.get("cited_by"),
                                "why": f"always_keep quota reached ({ALWAYS_KEEP_PER_SUB}) for {s['id']}"})
                continue
            keep = always or r["key"] in multi_primary or n_other < K_PER_SUB
            if not keep:
                # A stage that discards records logs why it discarded each one. Screening does;
                # select did not, which cost an hour of guessing (24 Oct, d02).
                dropped.append({"key": r["key"], "sid": s["id"], "evidence_type": et,
                                "score": round(score, 3), "cited_by": rec.get("cited_by"),
                                "why": f"k_per_sub reached ({K_PER_SUB}) for {s['id']}"})
                continue
            if always:
                n_always += 1
            elif r["key"] not in multi_primary:
                n_other += 1"""
    if t.count(old) != 1:
        raise RuntimeError(f"expected 1 occurrence of the select loop, found {t.count(old)}")
    t = t.replace(old, new)

    old2 = '"k_per_sub": K_PER_SUB, "max_docs": MAX_DOCS, "excluded_out_of_scope": out_of_scope,'
    new2 = ('"k_per_sub": K_PER_SUB, "max_docs": MAX_DOCS, "excluded_out_of_scope": out_of_scope,\n'
            '                                                 "always_keep_per_sub": ALWAYS_KEEP_PER_SUB,')
    if t.count(old2) != 1:
        raise RuntimeError("could not find the selected.json rule dict")
    return p, t.replace(old2, new2)


# ── D ───────────────────────────────────────────────────────────────────────
@patch("D")
def d(repo):
    p = repo / "packs/deep-research/cases-ohsv.json"
    t = p.read_text()
    if '"role"' in t:
        raise Skip("roles already present")
    roles = {"Ikeda 1999": '"role": "alternative", "group": "clearance-barrier"',
             "Fulci 2006": '"role": "alternative", "group": "clearance-barrier"',
             "Streby 2019": '"role": "required"'}
    out = []
    hit = 0
    for line in t.splitlines(keepends=True):
        for label, add in roles.items():
            if f'"label": "{label}' in line and line.rstrip().rstrip(",").endswith("}"):
                stripped = line.rstrip()
                comma = "," if stripped.endswith(",") else ""
                body = stripped.rstrip(",")[:-1].rstrip().rstrip(",")
                line = f"{body}, {add}}}{comma}\n"
                hit += 1
                break
        out.append(line)
    if hit != 3:
        raise RuntimeError(f"expected to annotate 3 key papers, annotated {hit}")
    return p, "".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="A,B,C,D")
    a = ap.parse_args(argv)
    repo = a.repo.expanduser().resolve()
    if not (repo / "engine/lake/lake.py").exists():
        print(f"not a harness repo: {repo} (no engine/lake/lake.py)", file=sys.stderr)
        return 1
    want = [x.strip().upper() for x in a.only.split(",") if x.strip()]
    rc = 0
    for name in want:
        if name not in PATCHES:
            print(f"[{name}] unknown patch"); rc = 1; continue
        try:
            path, text = PATCHES[name](repo)
        except Skip as e:
            print(f"[{name}] skip: {e}")
            continue
        except Exception as e:
            print(f"[{name}] FAILED, nothing written: {e}")
            rc = 1
            continue
        if a.dry_run:
            print(f"[{name}] would patch {path.relative_to(repo)}")
        else:
            path.write_text(text)
            print(f"[{name}] patched {path.relative_to(repo)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
