#!/usr/bin/env python3
"""Render a finished lake as a report a person can read.

Each point reads as prose followed by author-year citations. Opening the
citations shows, per paper, the verified quote(s) behind the point, what the
extract step paraphrased them as, how the quote matched, the paper's evidence
type, and links out (DOI, Europe PMC). A reference list and a list of papers
selected but never cited close the page.

If premises.json shows a presupposition with 0 hits, the page opens with a
banner saying so, and every section carries a reminder that what follows is
related literature, not evidence about it. Each section shows "N points from M
papers (type mix)", counted from the citations.

Reads only finished stage files: plan.json, synthesis.json, evidence.jsonl,
selected.json, premises.json (optional). Writes report.html next to them and changes nothing else, so it
cannot affect any result. No model calls, no network.

    python3 engine/lake/render.py data/lakes/<slug>        # one lake
    python3 engine/lake/render.py data/lake-evals/<stamp>       # every case in a run
    python3 engine/lake/render.py <lake> -o /tmp/report.html     # elsewhere
"""
import html
import json
import sys
from pathlib import Path

TYPE_LABEL = {
    "independent_comparison": "independent comparison",
    "review": "review",
    "primary_study": "primary study",
    "new_method": "method paper (authors' own tool)",
    "other": "other",
}


# ---------- loading

def _json(p, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def _jsonl(p):
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def is_lake(d):
    return (Path(d) / "synthesis.json").exists()


# ---------- citation labels

def surname(author):
    """'Xi NM' -> 'Xi'; 'van der Berg AB' -> 'van der Berg'; 'Tabula Sapiens Consortium' unchanged."""
    parts = author.split()
    if len(parts) > 1 and parts[-1].isalpha() and parts[-1].isupper() and len(parts[-1]) <= 3:
        parts = parts[:-1]
    return " ".join(parts) or "?"


def author_part(authors):
    s = [surname(a) for a in (authors or []) if a and a.strip()]
    if not s:
        return "Anon."
    if len(s) == 1:
        return s[0]
    if len(s) == 2:
        return f"{s[0]} & {s[1]}"
    return f"{s[0]} et al."


def esc(x):
    return html.escape(str(x), quote=True)


def epmc_url(key, pmcid):
    if key and ":" in key:
        src, ident = key.split(":", 1)
        return f"https://europepmc.org/article/{src}/{ident}"
    if pmcid:
        return f"https://europepmc.org/article/PMC/{pmcid}"
    return None


# ---------- model of the report

def collect_sections(plan, syn):
    """[(id, heading, points, gaps)] in plan order; summary first if present."""
    out = []
    summ = syn.get("summary")
    if isinstance(summ, dict):
        summ = summ.get("points")
    if isinstance(summ, list) and summ:
        pts = [p if isinstance(p, dict) else {"text": str(p), "eids": []} for p in summ]
        out.append(("summary", "Summary", pts, []))
    heads = {s["id"]: s.get("text", s["id"]) for s in (plan or {}).get("subquestions", [])}
    subs = syn.get("subquestions", {})
    order = [s for s in heads if s in subs] + [s for s in subs if s not in heads]
    for sid in order:
        sq = subs[sid]
        out.append((sid, heads.get(sid, sid), sq.get("points", []), sq.get("gaps", [])))
    return out


def build(lake):
    lake = Path(lake)
    plan = _json(lake / "plan.json", {}) or {}
    syn = _json(lake / "synthesis.json", {}) or {}
    ev = {e["eid"]: e for e in _jsonl(lake / "evidence.jsonl")}
    sel = _json(lake / "selected.json", {}) or {}
    sel_docs = {d["key"]: d for d in sel.get("docs", [])}

    # one record per paper: selected.json first, evidence fills gaps
    papers = {}
    for k, d in sel_docs.items():
        papers[k] = dict(d, year=d.get("pub_year") or d.get("year"))
    for e in ev.values():
        p = papers.setdefault(e["key"], {"key": e["key"]})
        for f in ("pmcid", "title", "doi", "url", "authors", "evidence_type", "year"):
            if not p.get(f) and e.get(f):
                p[f] = e[f]

    sections = collect_sections(plan, syn)

    # number references by first citation
    order = []
    for _, _, pts, _ in sections:
        for pt in pts:
            for eid in pt.get("eids", []):
                k = ev.get(eid, {}).get("key")
                if k and k not in order:
                    order.append(k)
    for i, k in enumerate(order, 1):
        papers[k]["n"] = i

    # author-year labels, with a/b for collisions
    groups = {}
    for k in order:
        p = papers[k]
        base = f"{author_part(p.get('authors'))} {p.get('year') or 'n.d.'}"
        groups.setdefault(base, []).append(k)
    for base, ks in groups.items():
        for j, k in enumerate(ks):
            papers[k]["label"] = base + ("abcdefghijklmnopqrstuvwxyz"[j] if len(ks) > 1 else "")

    uncited = [d for k, d in sel_docs.items() if k not in order]
    premises = _json(lake / "premises.json", []) or []
    stability = _json(lake / "stability.json", {}) or {}
    truth = _json(lake / "truth.json", {}) or {}
    return dict(lake=lake, plan=plan, syn=syn, ev=ev, papers=papers,
                order=order, sections=sections, uncited=uncited, premises=premises, stability=stability, truth=truth,
                missing_premises=[p for p in premises if not p.get("hits")])


def support(pts, m):
    """Counts for a list of points: points, distinct papers cited, papers per evidence type."""
    keys = []
    for pt in pts:
        for eid in pt.get("eids", []):
            k = m["ev"].get(eid, {}).get("key")
            if k and k not in keys:
                keys.append(k)
    types = {}
    for k in keys:
        t = m["papers"][k].get("evidence_type") or "other"
        types[t] = types.get(t, 0) + 1
    return len(pts), len(keys), types


def support_line(pts, m):
    n_pts, n_papers, types = support(pts, m)
    if not n_pts:
        return ""
    mix = ", ".join(f"{v} {TYPE_LABEL.get(t, t)}" for t, v in
                    sorted(types.items(), key=lambda x: list(TYPE_LABEL).index(x[0]) if x[0] in TYPE_LABEL else 99))
    s = (f"{n_pts} point{'s' if n_pts != 1 else ''} from {n_papers} paper{'s' if n_papers != 1 else ''}"
         + (f" ({mix})" if mix else ""))
    return f'<div class="support">{esc(s)}</div>'


# ---------- html

def tag(etype, in_scope=None):
    out = ""
    if etype:
        out = f'<span class="tag t-{esc(etype)}">{esc(TYPE_LABEL.get(etype, etype))}</span>'
    if in_scope is False:
        out += '<span class="tag t-oos" title="different setting from the review scope; used as transferable evidence">out of scope</span>'
    return out


def links(p):
    out = []
    if p.get("doi"):
        out.append(f'<a href="https://doi.org/{esc(p["doi"])}" target="_blank" rel="noopener">DOI</a>')
    elif p.get("url"):
        out.append(f'<a href="{esc(p["url"])}" target="_blank" rel="noopener">link</a>')
    u = epmc_url(p.get("key"), p.get("pmcid"))
    if u:
        out.append(f'<a href="{esc(u)}" target="_blank" rel="noopener">Europe PMC</a>')
    return " · ".join(out)


def match_note(e):
    m = e.get("match")
    if m == "exact" or m is None:
        return "verbatim match" if m else ""
    r = e.get("match_ratio")
    return f"{esc(m)} match" + (f" ({r:.0%})" if isinstance(r, (int, float)) else "")


def render_point(pt, m):
    ev, papers = m["ev"], m["papers"]
    eids = pt.get("eids", [])
    by_paper, missing = {}, []
    for eid in eids:
        e = ev.get(eid)
        if e is None:
            missing.append(eid)
        else:
            by_paper.setdefault(e["key"], []).append(e)

    chips = [esc(papers[k].get("label", k)) for k in by_paper]
    cite = "(" + "; ".join(chips) + ")" if chips else "(no citation)"
    nq = sum(len(v) for v in by_paper.values())

    flags = []
    if pt.get("issues"):
        flags.append('<span class="flag warn" title="unresolved check">unresolved check</span>')
    elif pt.get("rounds") and pt.get("issues_initial"):
        flags.append('<span class="flag note">rewritten after check</span>')
    if missing:
        flags.append('<span class="flag warn">evidence missing</span>')
    st = m.get("stability", {}).get(pt.get("id"))
    if st:
        k, n = st.get("in", 0), st.get("of", 0)
        cls = "ok" if k == n else ("note" if k * 2 > n else "warn")
        flags.append(f'<span class="flag {cls}" title="present in {k} of {n} sampled syntheses">in {k}/{n} samples</span>')

    tr = m.get("truth", {}).get(pt.get("id"))
    if tr:
        cls = "ok" if tr.get("meets") else ("note" if tr.get("level") == 3 else "warn")
        lab = tr.get("label", "")
        if tr.get("papers", 0) > tr.get("groups", 0) and tr.get("groups"):
            lab += f" · {tr['papers']} papers"
        g = tr.get("grade")
        if g:
            cls = {"High": "ok", "Moderate": "note"}.get(g["grade"], "warn")
            lab = f"{g['grade']} · {lab}"
        flags.append(f'<span class="flag {cls}" title="independent groups among the cited primary papers '
                     f'(standard: {tr.get("need", 3)})">{esc(lab)}</span>')
        if any("RETRACTED" in f for f in tr.get("flags", [])):
            flags.append('<span class="flag warn">retracted source</span>')
    body = []
    if tr:
        rows = []
        full = {x["key"]: x.get("authors") or [] for x in tr.get("primary", [])}
        for i, g in enumerate(tr.get("group_list", []), 1):
            names = "; ".join(esc(m["papers"].get(k, {}).get("label", k)) for k in g["keys"])
            last = {(full.get(k) or ["?"])[-1] for k in g["keys"]}
            rows.append(f"<li>group {i}: {names} <span class='meta'>(last author {esc(', '.join(sorted(last)))})</span>"
                        + "".join(f"<div class='meta'>grouped: {esc(w)}</div>" for w in g.get("why", [])) + "</li>")
        rel = "".join(f"<li class='meta'>not counted: {esc(m['papers'].get(r['key'], {}).get('label', r['key']))} ({esc(r['why'])})</li>"
                      for r in tr.get("relayed", []))
        fl = "".join(f"<li class='meta'>{esc(f)}</li>" for f in tr.get("flags", []))
        g = tr.get("grade")
        gline = ""
        if g:
            meth = ", ".join(x.replace("_", " ") for x in g.get("methods", [])) or "none recognised"
            syst = ", ".join(x.replace("_", " ") for x in g.get("systems", [])) or "unclear"
            gline = (f"<div><b>{esc(g['grade'])}</b>: {esc(' · '.join(g['steps']))}</div>"
                     f"<div class='meta'>methods: {esc(meth)} · systems: {esc(syst)} · "
                     f"{g.get('direct', 0)} of {g.get('items', 0)} items in the question's own system</div>")
        body.append(f"<div class='truth'><b>Truthinator.</b> {gline}{esc(tr.get('label', ''))}: {tr.get('groups', 0)} independent "
                    f"group(s) among {tr.get('papers', 0)} primary paper(s); standard {tr.get('need', 3)}."
                    f"<ul>{''.join(rows)}{rel}{fl}</ul>"
                    "<div class='meta'>Grouped by authorship only; shared patients, cell lines or reagents are not visible here.</div></div>")
    if pt.get("issues_initial") or pt.get("issues"):
        rows = "".join(f"<li>{esc(i)}</li>" for i in pt.get("issues_initial", []))
        rest = "".join(f"<li>{esc(i)}</li>" for i in pt.get("issues", []))
        body.append('<div class="checks"><b>Checks.</b> ')
        if rows:
            body.append(f'First draft flagged:<ul>{rows}</ul>')
            body.append(f'Rewritten {esc(pt.get("rounds", 0))}×; ')
        body.append(f'still flagged:<ul>{rest}</ul></div>' if rest else 'now passes.</div>')
    for k, es in by_paper.items():
        p = papers[k]
        body.append('<div class="paper">')
        body.append(f'<div class="ph"><a href="#ref-{p.get("n")}">[{p.get("n")}] {esc(p.get("label", k))}</a> '
                    f'{tag(p.get("evidence_type"), p.get("in_scope"))} <span class="lk">{links(p)}</span></div>')
        for e in es:
            meta = " · ".join(x for x in [esc(e["eid"]), esc(e.get("section") or ""), match_note(e)] if x)
            body.append(f'<blockquote>{esc(e.get("quote", ""))}</blockquote>')
            if e.get("claim"):
                body.append(f'<div class="claim">extracted as: {esc(e["claim"])}</div>')
            body.append(f'<div class="meta">{meta}</div>')
        body.append('</div>')
    if missing:
        body.append(f'<div class="checks warn">Cited but not in evidence.jsonl: {esc(", ".join(missing))}</div>')

    kind = pt.get("kind")
    kind_html = f'<span class="kind">{esc(kind)}</span> ' if kind and kind != "finding" else ""
    return (f'<li class="point" id="{esc(pt.get("id", ""))}"><p>{kind_html}{esc(pt.get("text", ""))} '
            f'{" ".join(flags)}</p>'
            f'<details><summary><span class="cites">{cite}</span>'
            f'<span class="nq">{nq} quote{"s" if nq != 1 else ""}</span></summary>'
            f'<div class="panel">{"".join(body)}</div></details></li>')


def render_ref(p):
    a = p.get("authors") or []
    al = ", ".join(a[:6]) + (", et al." if len(a) > 6 else "")
    extra = []
    if p.get("journal"):
        extra.append(f"<i>{esc(p['journal'])}</i>")
    if p.get("cited_by") is not None:
        extra.append(f"cited by {esc(p['cited_by'])}")
    if p.get("sids"):
        extra.append("selected for " + esc(", ".join(p["sids"])))
    if p.get("score") is not None:
        extra.append(f"selection score {esc(p['score'])}")
    if p.get("text_source"):
        extra.append(f"read from {esc(p['text_source'])}")
    return (f'<li id="ref-{p.get("n", "")}"><span class="rl">{esc(p.get("label", ""))}</span> '
            f'{esc(al)} ({esc(p.get("year") or "n.d.")}). {esc(p.get("title", ""))} '
            f'{tag(p.get("evidence_type"), p.get("in_scope"))}<div class="rx">{" · ".join(x for x in extra + [links(p)] if x)}</div></li>')


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--line:#e3e0d8;--panel:#f3f1ea;--acc:#2f5d8a;
--ind:#2f7a4f;--rev:#2f5d8a;--pri:#6b5a2a;--own:#a24a2a;--oth:#777;--warn:#a8322a;--note:#7a6420}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--fg:#e8e6e0;--mut:#9a978f;--line:#33322e;
--panel:#22211f;--acc:#8db4dd;--ind:#7cc79a;--rev:#8db4dd;--pri:#cdb877;--own:#e49a7c;--oth:#aaa;
--warn:#ee8a80;--note:#d9c06a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 Georgia,'Iowan Old Style',serif}
main{max-width:46rem;margin:0 auto;padding:2.5rem 16px 5rem}
h1{font-size:1.6rem;line-height:1.3;margin:0 0 .4rem}
h2{font-size:1.15rem;line-height:1.35;margin:2.4rem 0 .6rem;padding-top:1rem;border-top:1px solid var(--line)}
.sub,.rx,.meta,.claim,.nq,.lk,.bar,.legend{font-family:system-ui,-apple-system,sans-serif}
.sub{color:var(--mut);font-size:.85rem;margin-bottom:1rem}
.bar{position:sticky;top:0;background:var(--bg);padding:.5rem 0;border-bottom:1px solid var(--line);
display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;font-size:.8rem;z-index:2}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:4px;
padding:.2rem .6rem;cursor:pointer}
.legend{color:var(--mut)}
ul.points{list-style:none;padding:0;margin:0}
.point{margin:0 0 1rem}
.point p{margin:0}
details summary{cursor:pointer;list-style:none;font-size:.95rem;color:var(--acc)}
details summary::-webkit-details-marker{display:none}
details summary::before{content:'▸ ';color:var(--mut)}
details[open] summary::before{content:'▾ '}
.nq{color:var(--mut);font-size:.75rem;margin-left:.5rem}
.panel{background:var(--panel);border-left:3px solid var(--line);padding:.6rem .9rem;margin:.4rem 0 .2rem}
.paper+.paper{margin-top:.8rem;padding-top:.6rem;border-top:1px dashed var(--line)}
.ph{font-family:system-ui,sans-serif;font-size:.85rem}
.ph a,.rx a{color:var(--acc)}
blockquote{margin:.4rem 0 .1rem;padding:0 0 0 .8rem;border-left:2px solid var(--acc);font-style:italic}
.claim{font-size:.8rem;color:var(--mut)}
.meta{font-size:.72rem;color:var(--mut)}
.tag{display:inline-block;font:600 .68rem/1.5 system-ui,sans-serif;padding:0 .4rem;border-radius:3px;
border:1px solid currentColor;margin-left:.3rem;vertical-align:1px}
.t-independent_comparison{color:var(--ind)}.t-review{color:var(--rev)}.t-primary_study{color:var(--pri)}
.t-new_method{color:var(--own)}.t-other{color:var(--oth)}.t-oos{color:var(--note)}
.flag{font:600 .68rem system-ui,sans-serif;padding:0 .35rem;border-radius:3px;margin-left:.3rem;vertical-align:1px}
.flag.warn{color:var(--warn);border:1px solid var(--warn)}.flag.note{color:var(--note);border:1px solid var(--note)}
.kind{font:600 .7rem system-ui,sans-serif;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.checks{font:.8rem/1.45 system-ui,sans-serif;color:var(--note);margin-bottom:.6rem}
.checks.warn{color:var(--warn)}.checks ul{margin:.2rem 0 .3rem 1.1rem;padding:0}
.gaps{font-size:.92rem;color:var(--mut);margin-top:.8rem}
.gaps b{font-family:system-ui,sans-serif;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
ol.refs{padding-left:1.6rem;font-size:.92rem}ol.refs li{margin-bottom:.7rem}
.rl{font-weight:600}.rx{font-size:.75rem;color:var(--mut)}
.banner{border:2px solid var(--warn);color:var(--fg);background:color-mix(in srgb,var(--warn) 9%,transparent);
border-radius:6px;padding:.8rem 1rem;margin:1rem 0;font:.95rem/1.5 system-ui,sans-serif}
.banner b{color:var(--warn)}
.secwarn{font:600 .78rem system-ui,sans-serif;color:var(--warn);margin:-.2rem 0 .3rem}
.support{font:.78rem system-ui,sans-serif;color:var(--mut);margin:-.2rem 0 .7rem}
.support.top{margin:.2rem 0 .8rem;font-size:.85rem}
.flag.ok{color:var(--ind);border:1px solid var(--ind)}
details.premises{margin:.6rem 0 1rem;font:.88rem system-ui,sans-serif}
details.premises ul{margin:.4rem 0;padding-left:1.2rem}
.truth{font:.8rem/1.45 system-ui,sans-serif;margin-bottom:.6rem}.truth ul{margin:.2rem 0 .3rem 1.1rem;padding:0}
:target{background:color-mix(in srgb,var(--acc) 12%,transparent)}
"""

JS = """
function all(open){document.querySelectorAll('details').forEach(d=>d.open=open)}
"""


def render(m):
    plan, papers = m["plan"], m["papers"]
    q = plan.get("question") or m["lake"].name
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{esc(q)}</title><style>{CSS}</style><script>{JS}</script></head><body><main>',
           f'<h1>{esc(q)}</h1>']
    sub = [f"Lake <code>{esc(m['lake'].name)}</code>"]
    if plan.get("scope"):
        sub.append(f"Scope: {esc(plan['scope'])}")
    out.append(f'<div class="sub">{" · ".join(sub)}</div>')

    miss = m["missing_premises"]
    if miss:
        names = "; ".join(f"“{esc(p.get('name', '?'))}” (search: {esc(p.get('query', ''))})" for p in miss)
        out.append(f'<div class="banner"><b>Premise not found.</b> The question assumes something that a direct '
                   f'Europe PMC search did not find: {names}. Anything below is related literature, '
                   f'<b>not evidence about it</b>. Read it as context, and treat the premise itself as unsupported.</div>')
    all_pts = [pt for s in m["sections"] if s[0] != "summary" for pt in s[2]]
    line = support_line(all_pts, m)
    if line:
        out.append(line.replace('class="support"', 'class="support top"'))
    if m["premises"]:
        rows = []
        for p in m["premises"]:
            hits = p.get("hits") or 0
            status = (f'<span class="flag warn">not found</span>' if not hits
                      else f'<span class="flag ok">found · {esc(hits)} hit{"s" if hits != 1 else ""}</span>')
            tops = []
            for t in (p.get("top") or [])[:3]:
                if isinstance(t, dict):
                    tops.append(esc(t.get("title") or t.get("key") or t.get("pmcid") or ""))
                else:
                    tops.append(esc(t))
            top_html = ("<div class='rx'>top hits: " + " · ".join(x for x in tops if x) + "</div>") if any(tops) else ""
            rows.append(f'<li>“{esc(p.get("name", "?"))}” {status}'
                        f'<div class="rx">search: {esc(p.get("query", ""))}</div>{top_html}</li>')
        out.append('<details class="premises"' + (" open" if miss else "") + '><summary>Premise check '
                   '<span class="nq">what the question takes for granted, searched directly (no model)</span>'
                   '</summary><ul>' + "".join(rows) + '</ul></details>')
    out.append('<div class="bar"><button onclick="all(true)">Show all quotes</button>'
               '<button onclick="all(false)">Hide</button>'
               '<span class="legend">Click a citation to see the verified quotes behind the point.</span></div>')

    for sid, head, pts, gaps in m["sections"]:
        h = esc(head) if sid == "summary" else f"{esc(sid)}. {esc(head)}"
        out.append(f'<h2 id="sec-{esc(sid)}">{h}</h2>')
        if miss and pts and sid != "summary":
            out.append('<div class="secwarn">Premise not found: related literature, not evidence about it.</div>')
        if sid != "summary":
            out.append(support_line(pts, m))
        out.append('<ul class="points">')
        out += [render_point(pt, m) for pt in pts]
        out.append("</ul>")
        if gaps:
            out.append('<div class="gaps"><b>Gaps</b><ul>' +
                       "".join(f"<li>{esc(g)}</li>" for g in gaps) + "</ul></div>")

    out.append('<h2 id="refs">References</h2><ol class="refs">')
    out += [render_ref(papers[k]) for k in m["order"]]
    out.append("</ol>")
    if m["uncited"]:
        out.append(f'<h2 id="uncited">Selected but not cited ({len(m["uncited"])})</h2>'
                   '<div class="sub">Read by the lake, cited by no point.</div><ul class="refs">')
        for d in m["uncited"]:
            p = dict(d, year=d.get("pub_year") or d.get("year"),
                     label=f"{author_part(d.get('authors'))} {d.get('pub_year') or d.get('year') or 'n.d.'}")
            out.append(render_ref(p).replace(' id="ref-"', ""))
        out.append("</ul>")
    out.append("</main></body></html>")
    return "\n".join(out)


def render_lake(lake, out_path=None):
    lake = Path(lake)
    out_path = Path(out_path) if out_path else lake / "report.html"
    out_path.write_text(render(build(lake)))
    return out_path


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    target = Path(argv[0]).expanduser()
    out = Path(argv[argv.index("-o") + 1]) if "-o" in argv else None
    if is_lake(target):
        print(render_lake(target, out))
        return 0
    cases = sorted(d for d in target.iterdir() if d.is_dir() and is_lake(d))
    if not cases:
        print(f"no lake (synthesis.json) in {target} or its subfolders", file=sys.stderr)
        return 1
    for d in cases:
        print(render_lake(d))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
