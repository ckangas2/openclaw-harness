#!/usr/bin/env python3
"""Human check page for a Truthinator run: the few things worth a scientist's eye, explained in plain sentences.

    python3 engine/lake/check_view.py data/experiments/truthinator/<run>     # -> <run>/check.html

Cards, in order: points graded High (do you agree?), points with 3+ labs that were rated down (is the reason
fair?), evidence labels the quote did not back (was the reset right?). Each card has "Looks right" /
"Something's off" buttons and a tally; choices are remembered in this browser only.
Reads truth.json, synthesis.json, evidence.jsonl, evidence_profile.jsonl, selected.json. No model calls.
"""
import html, json, sys
from pathlib import Path

SYSTEM = {"human_patients": "patients", "human_primary_cells": "cells taken from people", "human_cell_line": "human cell lines",
          "animal_in_vivo": "living animals", "animal_cells": "animal cells", "in_vitro_biochemical": "test-tube / purified protein work",
          "computational": "computer analysis", "unclear": "an unclear system"}
METHOD = {"human_genetic_variant": "people with a natural mutation", "genetic_knockout_or_knockdown": "knocking out or silencing the gene",
          "blocking_antibody_or_receptor_blockade": "blocking antibodies", "drug_or_inhibitor": "a drug or inhibitor",
          "overexpression_or_recombinant": "adding the protein or overexpressing it", "intervention_trial": "a controlled trial",
          "observational_correlation": "observing a correlation", "computational_prediction": "a computational prediction",
          "descriptive_other": "description only"}
esc = lambda x: html.escape(str(x))


def surname(name):
    """'de Saint Basile G' -> 'de Saint Basile'; 'Kroczek RA' -> 'Kroczek'."""
    parts = (name or "?").split()
    return " ".join(parts[:-1]) if len(parts) > 1 and parts[-1].isalpha() and parts[-1].isupper() and len(parts[-1]) <= 3 else " ".join(parts)


def lab(p):
    a = p.get("authors") or ["?"]
    yr = p.get("year") or ""
    who = f"{surname(a[0])} et al. {yr}" if len(a) > 2 else f"{' & '.join(surname(x) for x in a)} {yr}"
    return f"{who.strip()} ({surname(a[-1])} lab)"


def sentence(step):
    """'-1 2 groups (standard 3)' -> a plain sentence."""
    s = step
    if s.startswith("start High"):
        return "Starts high: at least one study did an experiment (changed something and watched the effect), not just observed."
    if s.startswith("start Low"):
        return "Starts low: the studies observed an association; nobody intervened."
    if s.startswith("start Very low"):
        return "Starts very low: nothing first-hand, or computer predictions only."
    if "one paper" in s:
        return "Rated down: only one paper reports this."
    if "one group" in s:
        return "Rated down: several papers, but all from one lab."
    if "groups (standard" in s:
        n = s.split()[1]
        return f"Rated down: only {n} independent labs have reported this (your standard is 3)."
    if "own system" in s:
        return "Rated down: none of the evidence comes from the system the question is about; it is all from related systems."
    if "disagreeing" in s:
        return "Rated down: the point itself reports conflicting results."
    if "unverified" in s:
        return "Rated down: the point still failed one of the report's automatic checks."
    if "retracted" in s:
        return "Rated down hard: it cites a retracted paper."
    if "preprints" in s:
        return "Rated down: only preprints, nothing peer reviewed."
    if "seed samples" in s:
        return f"Rated down: the point only showed up in {s.split('in ')[-1]} when the synthesis was rerun with variation."
    if "methods agree" in s:
        return "Rated up: different kinds of experiment point the same way."
    if "human genetic" in s:
        return "Rated up: people with a natural mutation show it (an experiment of nature)."
    if s.startswith("capped") and "checks" in s:
        return f"Capped at {s.split()[2].rstrip(':')}: the sentence failed the report's own checks (shown above), so it cannot be graded higher."
    if s.startswith("capped"):
        return "Kept below High because fewer than 3 independent labs have reported it."
    return s


def build(run: Path) -> str:
    truth = json.loads((run / "truth.json").read_text())
    syn = json.loads((run / "synthesis.json").read_text())
    ev = {}
    with open(run / "evidence.jsonl") as f:
        for l in f:
            if l.strip():
                e = json.loads(l); ev[e["eid"]] = e
    prof = {}
    pp = run / "evidence_profile.jsonl"
    if pp.exists():
        with open(pp) as f:
            for l in f:
                if l.strip():
                    q = json.loads(l); prof[q["eid"]] = q
    sel = {d["key"]: d for d in json.loads((run / "selected.json").read_text()).get("docs", [])} if (run / "selected.json").exists() else {}
    pts = {p["id"]: p for b in syn.get("subquestions", {}).values() for p in b.get("points", [])}
    pts.update({p["id"]: p for p in syn.get("summary", []) if p.get("id")})
    q = json.loads((run / "plan.json").read_text()).get("question", run.name) if (run / "plan.json").exists() else run.name

    cards = []

    def point_card(pid, ask):
        t, p = truth[pid], pts.get(pid, {})
        g = t.get("grade", {})
        prim = {x["key"]: x for x in t["primary"]}
        rows = []
        for i, grp in enumerate(t["group_list"], 1):
            names = " + ".join(lab(prim[k]) for k in grp["keys"])
            rows.append(f"<div class='lab'><div class='labname'>Lab {i}: {esc(names)}</div>")
            for x in p.get("eids", []):
                e = ev.get(x)
                if not e or e["key"] not in grp["keys"]:
                    continue
                q = prof.get(x, {})
                did = METHOD.get(q.get("method"), "not recognised")
                where = SYSTEM.get(q.get("system"), "an unclear system")
                d = {"direct": "the question's own system", "adjacent": "a related system, not the question's own"}.get(q.get("directness"), "unclear whether it is the question's system")
                rows.append(f"<div class='ev'><b>{esc(did)}</b>, in {esc(where)} ({esc(d)})"
                            f"<blockquote>{esc(e.get('quote', '')[:400])}</blockquote></div>")
            rows.append("</div>")
        for r in t.get("relayed", []):
            rows.append(f"<p class='small'>Not counted: {esc(sel.get(r['key'], {}).get('title', r['key'])[:80])} ({esc(r['why'])}).</p>")
        flagged = "".join(f"<li>{esc(i)}</li>" for i in (p.get("issues") or []))
        if flagged:
            flagged += "<li class='small'>This flag is about the sentence's wording; it does not change the evidence grade. It has its own card below.</li>"
        reasons = "".join(f"<li>{esc(sentence(s))}</li>" for s in g.get("steps", []))
        body = (f"<p class='claim'>“{esc(p.get('text', ''))}”</p>"
                + (f"<div class='flagged'><b>The report's checker flagged this sentence:</b><ul>{flagged}</ul></div>" if flagged else "")
                + f"<p><b>The evidence, by lab ({t['groups']} independent):</b></p>{''.join(rows)}"
                  f"<p><b>How the Truthinator got to {esc(g.get('grade', ''))}:</b></p><ul>{reasons}</ul>")
        cards.append((f"{g.get('grade', '')} · {pid}", ask.replace("{grade}", g.get("grade", "")), body, "grade"))

    high = [k for k, t in truth.items() if t.get("grade", {}).get("grade") == "High"]
    down = [k for k, t in truth.items() if t.get("meets") and t.get("grade", {}).get("grade") != "High"]
    for k in high:
        point_card(k, "Given this evidence, is {grade} the grade you would give? (Also: are these really different labs?)")
    for k in down:
        point_card(k, "Three labs, but rated {grade}. Given this evidence, is {grade} the grade you would give?")
    for pid, p in pts.items():
        if not p.get("issues") or p.get("dropped") or pid not in truth:
            continue
        cited = []
        for x in p.get("eids", []):
            e = ev.get(x)
            if e:
                cited.append(f"<div class='ev'><b>{esc(lab(next((y for y in truth[pid]['primary'] if y['key'] == e['key']), {'authors': e.get('authors'), 'year': e.get('year')})))}</b>"
                             f"<blockquote>{esc(e.get('quote', '')[:400])}</blockquote></div>")
        body = (f"<p class='claim'>“{esc(p.get('text', ''))}”</p>"
                f"<div class='flagged'><b>The checker said:</b><ul>{''.join(f'<li>{esc(i)}</li>' for i in p['issues'])}</ul></div>"
                f"<p><b>The quotes it cites:</b></p>{''.join(cited)}")
        cards.append((f"Checker flag · {pid}", "Does the sentence really say more than these quotes (flag is right), "
                      "or is it fine (false alarm)?", body, "flag"))
    for x, qq in prof.items():
        if not qq.get("notes"):
            continue
        e = ev.get(x, {})
        title = sel.get(e.get("key"), {}).get("title") or e.get("key", "")
        said = []
        for n in qq["notes"]:
            if n.startswith("system"):
                said.append("it was studied in <b>" + esc(n.split("'")[1].replace("_", " ")) + "</b>")
            elif n.startswith("method"):
                said.append("the method was <b>" + esc(n.split("'")[1].replace("_", " ")) + "</b>")
        body = (f"<p class='small'>{esc(title)}</p>"
                f"<p>The model said {' and '.join(said)}, but the words it pointed to are not in the quote, so the label was "
                f"set to <i>unclear</i>.</p><blockquote>{esc(e.get('quote', ''))}</blockquote>")
        cards.append((f"Label check · {x}", "Read the quote: is the model's label really there?", body, "label"))

    BTN = {"grade": [("high", "Too high"), ("ok", "About right"), ("low", "Too low")],
           "label": [("ok", "Not there: reset was right"), ("off", "It is there: reset too strict")],
           "flag": [("ok", "Flag is right"), ("off", "False alarm")]}
    items = "".join(
        f"<section class='card' data-id='{esc(cid)}'><h3>{esc(cid)}</h3><p class='ask'>{esc(ask)}</p>{body}"
        f"<div class='btns'>{''.join(f'<button data-v={v}>{esc(lbl)}</button>' for v, lbl in BTN[kind])}"
        f"<input placeholder='why (optional)'></div></section>"
        for cid, ask, body, kind in cards)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Truthinator check</title><style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--line:#e3e0d8;--card:#fff;--acc:#2f5d8a;--ok:#2f7a4f;--off:#a8322a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171716;--fg:#e8e6e0;--mut:#9a978f;--line:#33322e;--card:#211f1d;--acc:#8db4dd;--ok:#7cc79a;--off:#ee8a80}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,sans-serif}}
main{{max-width:44rem;margin:0 auto;padding:2rem 16px 4rem}}h1{{font-size:1.4rem;margin:0 0 .3rem}}
.top{{position:sticky;top:0;background:var(--bg);padding:.6rem 0;border-bottom:1px solid var(--line);z-index:2}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:1rem 1.1rem;margin:1rem 0}}
.card h3{{margin:0;font-size:.85rem;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}}
.ask{{font-weight:600;margin:.3rem 0 .6rem}}.claim{{font-family:Georgia,serif;font-size:1.05rem}}
.small{{color:var(--mut);font-size:.85rem}}.intro{{font-size:.95rem;margin:.6rem 0 1rem}}
.lab{{border-left:3px solid var(--line);padding:.1rem 0 .1rem .7rem;margin:.5rem 0}}.labname{{font-weight:600}}
.ev{{margin:.35rem 0 .2rem}}.flagged{{background:color-mix(in srgb,var(--off) 10%,transparent);border-radius:6px;padding:.5rem .7rem;margin:.5rem 0}}
.flagged ul{{margin:.2rem 0 0 1.1rem}}blockquote{{margin:.5rem 0;padding-left:.8rem;border-left:3px solid var(--acc);font-family:Georgia,serif;font-style:italic}}
ul{{margin:.2rem 0 .5rem 1.2rem;padding:0}}.btns{{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.7rem}}
button{{font:inherit;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:6px;padding:.3rem .8rem;cursor:pointer}}
button.on{{background:var(--acc);color:#fff;border-color:var(--acc)}}button.on[data-v=ok]{{background:var(--ok);border-color:var(--ok)}}
input{{flex:1;min-width:10rem;font:inherit;border:1px solid var(--line);border-radius:6px;padding:.3rem .5rem;background:transparent;color:var(--fg)}}
</style></head><body><main>
<h1>Truthinator check</h1><p class="small">{esc(q)}</p>
<p class="intro">You are checking the <b>Truthinator's reasoning</b>, not whether each claim is true in the world. For each point:
read the claim, the evidence lab by lab, and how the grade was reached; then say whether that grade fits the evidence shown.
Checker-flag cards ask whether the report's own fact checker was right about a sentence.
Label cards ask whether the model's description of a study is really in its quote.</p>
<div class="top"><b id="tally">0 of {len(cards)} checked</b> · <span class="small">only the {len(cards)} {"thing" if len(cards) == 1 else "things"} worth your eye; the full detail is in report.html</span>
<button id="copy" style="float:right">Copy my answers</button></div>
{items or "<p>Nothing to check: no High points, no rated-down 3-lab points, no reset labels.</p>"}
</main><script>
const K='truthcheck:'+document.title+location.pathname;let s={{}};try{{s=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
const save=()=>{{try{{localStorage.setItem(K,JSON.stringify(s))}}catch(e){{}};tally()}};
function tally(){{const v=Object.values(s).filter(x=>x.v);const ok=v.filter(x=>x.v==='ok').length;
document.getElementById('tally').textContent=v.length+' of {len(cards)} answered · '+ok+' agree with the Truthinator · '+(v.length-ok)+' disagree'}}
document.querySelectorAll('.card').forEach(c=>{{const id=c.dataset.id;const st=s[id]||{{}};
c.querySelectorAll('button').forEach(b=>{{if(st.v===b.dataset.v)b.classList.add('on');b.onclick=()=>{{s[id]=Object.assign(s[id]||{{}},{{v:b.dataset.v}});
c.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));save()}}}});
const i=c.querySelector('input');i.value=st.n||'';i.oninput=()=>{{s[id]=Object.assign(s[id]||{{}},{{n:i.value}});save()}}}});
document.getElementById('copy').onclick=()=>{{const t=Object.entries(s).map(([k,x])=>k+': '+(x.v||'-')+(x.n?' ('+x.n+')':'')).join('\\n');
navigator.clipboard&&navigator.clipboard.writeText(t);document.getElementById('copy').textContent='Copied'}};tally();
</script></body></html>"""


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__); return 1
    run = Path(argv[0]).expanduser()
    out = run / "check.html"
    out.write_text(build(run))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
