#!/usr/bin/env python3
"""One self-contained HTML page for reviewing an eval run (or a single lake) in a browser.

  review.py <eval_dir>            # data/lake-evals/<stamp>/  -> <eval_dir>/review.html
  review.py <lake_dir>            # a single lake
  review.py <dir> -o /tmp/x.html

Reads only lake files; no model calls, no network. The page leads with what needs a human eye
(claims that may overstate their quote, UNVERIFIED points, rejected quotes, relevant papers that
selection dropped), then gives the report and every decision behind it.
"""
import argparse, html, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from store import read_json, read_jsonl  # noqa: E402
import verify  # noqa: E402

def E(x):
    return html.escape("" if x is None else str(x))


# ── tiny markdown renderer (headings, lists, tables, inline, keeps <sub>) ──
def md(text: str) -> str:
    def inline(s):
        s = re.sub(r"<sub>(.*?)</sub>", lambda m: "\x00SUB\x01" + m.group(1) + "\x00/SUB\x01", s)
        s = E(s)
        s = s.replace("\x00SUB\x01", "<sub>").replace("\x00/SUB\x01", "</sub>")
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        s = re.sub(r"(?<![\"=>])(https?://[^\s<)]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
        return s

    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if m := re.match(r"^(#{1,6})\s+(.*)", ln):
            lvl = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
        elif ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[-:| ]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            tb = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table>")
        elif ln.strip().startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ")):
                items.append(f"<li>{inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif not ln.strip():
            i += 1
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("|", "- ", "* ", "#")):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


def n_of(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def badge(txt, kind=""):
    return f'<span class="b {kind}">{E(txt)}</span>'


TYPE_CLASS = {"independent_comparison": "ok", "review": "info", "primary_study": "info",
              "new_method": "warn", "other": "mute"}


def diff_marks(claim: str, quote: str, title: str = "") -> list:
    """Names and numbers in the claim that are absent from its quote (the same heuristic the
    extract check uses) — these are the claims worth eyeballing."""
    ctx = (quote + " " + (title or "")).lower()
    names = sorted({n for n in verify.NAME_RE.findall(claim) if n.lower() not in ctx})
    nums = sorted(verify.numbers(claim) - verify.numbers(quote + " " + (title or "")))
    return names + nums


# ── per-lake rendering ──
def lake_section(d: Path, idx: int, score: dict | None) -> tuple[str, dict]:
    man = read_json(d / "manifest.json", {}) or {}
    plan = read_json(d / "plan.json", {}) or {}
    syn = read_json(d / "synthesis.json", {}) or {}
    sel = read_json(d / "selected.json", {}) or {}
    docs = {x["key"]: x for x in sel.get("docs", [])}
    ev = {e["eid"]: e for e in read_jsonl(d / "evidence.jsonl")}
    rej = read_jsonl(d / "evidence_rejected.jsonl")
    scr = read_jsonl(d / "screen.jsonl")
    recs = {r["key"]: r for r in read_jsonl(d / "records.jsonl")}
    prem = read_json(d / "premises.json", []) or []
    chain = read_json(d / "chain.json", {}) or {}
    qidx = read_json(d / "queries" / "index.json", []) or []
    metrics = read_json(d / "metrics.json", {}) or {}
    cid = (score or {}).get("case") or man.get("slug", d.name)
    anchor = f"c{idx}"
    subs = {s["id"]: s for s in plan.get("subquestions", [])}
    points = [(sid, p) for sid, b in syn.get("subquestions", {}).items() for p in b.get("points", [])]
    allp = points + [("Summary", p) for p in syn.get("summary", [])]

    # things that want a human
    flagged = [(e, m) for e in ev.values() if (m := diff_marks(e["claim"], e["quote"], e.get("title")))]
    unver = [(sid, p) for sid, p in allp if p.get("issues")]
    dropped = [p for sid, p in allp if p.get("dropped")]
    rel_unsel = [r for r in scr if r.get("relevant") and r.get("in_scope", True) and r["key"] not in docs]

    H = [f'<section id="{anchor}"><h2>{E(cid)}</h2>']
    H.append(f'<p class="q">{E(plan.get("question", ""))}</p>')
    meta = [f'protocol {man.get("protocol", "?")}', f'model {man.get("model", "?")}']
    if score and score.get("wall_secs"):
        meta.append(f'{round(score["wall_secs"] / 60, 1)} min')
    if plan.get("scope"):
        meta.append("scope: " + plan["scope"])
    H.append('<p class="meta">' + " · ".join(E(m) for m in meta) + "</p>")

    # headline numbers
    tiles = [("evidence kept", metrics.get("evidence_kept")), ("quotes rejected", metrics.get("quotes_rejected")),
             ("points", metrics.get("points")), ("fixed by checks", metrics.get("points_fixed")),
             ("UNVERIFIED", metrics.get("points_unverified")), ("references", metrics.get("references")),
             ("independent refs", metrics.get("independent_references")), ("model calls", metrics.get("llm_calls"))]
    H.append('<div class="tiles">' + "".join(
        f'<div class="tile"><b>{E(v if v is not None else "-")}</b><span>{E(k)}</span></div>' for k, v in tiles) + "</div>")

    # key papers
    if score and score.get("keys"):
        rows = []
        for k in score["keys"]:
            stage = "cited" if k["cited"] else "selected, not cited" if k["selected"] else "found, not selected" if k["found"] else "not found"
            cls = "ok" if k["cited"] else "bad"
            rows.append(f'<tr><td>{E(k["label"])}</td><td>{badge("found" if k["found"] else "no", "ok" if k["found"] else "bad")}</td>'
                        f'<td>{badge("yes" if k["selected"] else "no", "ok" if k["selected"] else "bad")}</td>'
                        f'<td>{badge("yes" if k["cited"] else "no", "ok" if k["cited"] else "bad")}</td>'
                        f'<td>{badge(stage, cls)}</td></tr>')
        H.append('<h3>Key papers</h3><table><thead><tr><th>paper</th><th>in search</th><th>selected</th>'
                 "<th>cited</th><th>lost where</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    # needs your eye
    H.append('<h3>Needs your eye</h3>')
    if not (flagged or unver or rej or rel_unsel or dropped):
        H.append('<p class="mute">Nothing flagged: every claim stayed within its quote, every point passed the checks.</p>')
    else:
        H.append('<div class="eye">')
        if flagged:
            H.append(f'<details open><summary>{n_of(len(flagged), "claim")} naming something that is not in the quote'
                     f'<span class="mute"> — read these</span></summary>')
            for e, marks in flagged[:40]:
                H.append(f'<div class="pair"><div class="c"><b>{E(e["eid"])}</b> {E(e["claim"])}'
                         f'<div class="marks">not in quote: {"".join(badge(m, "warn") for m in marks)}</div></div>'
                         f'<div class="qt">{E(e["quote"])}</div></div>')
            H.append("</details>")
        if unver:
            H.append(f'<details open><summary>{n_of(len(unver), "point")} shipped UNVERIFIED</summary>')
            for sid, p in unver:
                H.append(f'<div class="pt bad"><span class="sid">{E(sid)}</span> {E(p["text"])}'
                         f'<div class="why">{"".join(badge(i, "bad") for i in p["issues"])}</div></div>')
            H.append("</details>")
        if rej:
            H.append(f'<details><summary>{n_of(len(rej), "claim")} rejected by the checks</summary>')
            for r in rej[:40]:
                why = r.get("why", "") + (": " + "; ".join(r.get("issues", [])) if r.get("issues") else "")
                H.append(f'<div class="pt"><div class="why">{badge(why, "warn")} {badge(r.get("sid", ""), "mute")} '
                         f'{badge((r.get("pmcid") or r.get("key") or ""), "mute")}</div>'
                         f'<div class="qt">“{E(r.get("quote", ""))}”</div><div class="mute">claim: {E(r.get("claim", ""))}</div></div>')
            H.append("</details>")
        if rel_unsel:
            H.append(f'<details><summary>{n_of(len(rel_unsel), "paper")} screened relevant but not selected</summary>'
                     "<table><thead><tr><th>sub-q</th><th>type</th><th>cited by</th><th>title</th></tr></thead><tbody>")
            seen = set()
            for r in sorted(rel_unsel, key=lambda x: x["sid"]):
                if (r["sid"], r["key"]) in seen:
                    continue
                seen.add((r["sid"], r["key"]))
                rec = recs.get(r["key"], {})
                H.append(f'<tr><td>{E(r["sid"])}</td><td>{badge(r.get("evidence_type", "?"), TYPE_CLASS.get(r.get("evidence_type"), ""))}</td>'
                         f'<td>{E(rec.get("cited_by", "-"))}</td><td>{E((rec.get("title") or "")[:120])}</td></tr>')
            H.append("</tbody></table></details>")
        if dropped:
            H.append(f'<details><summary>{n_of(len(dropped), "point")} dropped during checking</summary>' +
                     "".join(f'<div class="pt mute">{E(p["text"])}</div>' for p in dropped) + "</details>")
        H.append("</div>")

    # premise check
    if prem:
        rows = "".join(
            f'<tr><td>{E(p["name"])}</td><td>{badge(p.get("hits", 0) or "NOT FOUND", "ok" if p.get("hits") else "bad")}</td>'
            f'<td><code>{E(p["query"])}</code></td><td>{E("; ".join((t.get("title") or "")[:60] for t in p.get("top", [])))}</td></tr>'
            for p in prem)
        H.append("<h3>Premise check</h3><table><thead><tr><th>what the question assumes</th><th>hits</th>"
                 "<th>search</th><th>top results</th></tr></thead><tbody>" + rows + "</tbody></table>")

    # report
    rp = d / "report.md"
    if rp.exists():
        H.append(f'<details class="big" open><summary>Report</summary><div class="report">{md(rp.read_text())}</div></details>')

    # points and what the checks did
    if allp:
        H.append('<details class="big"><summary>Points, and what the checks changed</summary>')
        for sid in list(syn.get("subquestions", {})) + ["Summary"]:
            ps = [p for s, p in allp if s == sid]
            if not ps:
                continue
            title = subs.get(sid, {}).get("text", "")
            H.append(f'<h4>{E(sid)}{". " + E(title) if title else ""}</h4>')
            for p in ps:
                cls = "bad" if p.get("issues") else "fixed" if p.get("issues_initial") else ""
                H.append(f'<div class="pt {cls}">{E(p["text"])}')
                cites = " ".join(badge(x, "mute") for x in p.get("eids", []))
                H.append(f'<div class="why">{cites}')
                if p.get("issues_initial"):
                    H.append(" " + "".join(badge("caught: " + i, "warn") for i in p["issues_initial"]))
                    H.append(badge(f'{p.get("rounds", 0)} revise round(s)', "info"))
                if p.get("issues"):
                    H.append("".join(badge("still: " + i, "bad") for i in p["issues"]))
                H.append("</div></div>")
            gaps = syn.get("subquestions", {}).get(sid, {}).get("gaps") or []
            if gaps:
                H.append('<div class="gaps">' + "".join(f"<div>gap: {E(g)}</div>" for g in gaps) + "</div>")
        H.append("</details>")

    # evidence
    if ev:
        H.append(f'<details class="big"><summary>Evidence ({len(ev)})</summary>'
                 '<input class="filter" placeholder="filter evidence…" oninput="flt(this)">'
                 '<div class="tbl">')
        for e in ev.values():          # extraction order; eids are content hashes, not counters
            marks = diff_marks(e["claim"], e["quote"], e.get("title"))
            H.append(f'<div class="pair row">'
                     f'<div class="c"><b>{E(e["eid"])}</b> {badge(e.get("evidence_type", ""), TYPE_CLASS.get(e.get("evidence_type"), ""))}'
                     f'{badge(e.get("kind", ""), "mute")}'
                     f'{badge("secondhand", "warn") if e.get("source") == "cited_work" else ""}'
                     f'{badge(e.get("match", ""), "ok" if e.get("match") == "exact" else "warn")}'
                     f'{badge("repaired", "warn") if e.get("claim_repaired") else ""}'
                     f'<div>{E(e["claim"])}</div>'
                     + (f'<div class="marks">not in quote: {"".join(badge(m, "warn") for m in marks)}</div>' if marks else "")
                     + f'<div class="mute">{E((e.get("title") or "")[:90])} · {E(e.get("section", ""))}</div></div>'
                     f'<div class="qt">{E(e["quote"])}</div></div>')
        H.append("</div></details>")

    # selected papers
    if docs:
        rows = "".join(
            f'<tr><td>{badge(x.get("evidence_type", ""), TYPE_CLASS.get(x.get("evidence_type"), ""))}</td>'
            f'<td>{E(x.get("pub_year", "-"))}</td><td>{E(x.get("cited_by", "-"))}</td>'
            f'<td>{badge(x.get("text_source", ""), "ok" if x.get("text_source") == "fulltext" else "warn")}'
            f'{badge("preprint", "warn") if x.get("is_preprint") else ""}</td>'
            f'<td>{E(", ".join(x.get("sids", [])))}</td>'
            f'<td>{E((x.get("title") or "")[:120])}</td></tr>' for x in sel.get("docs", []))
        extra = (sel.get("rule") or {}).get("excluded_out_of_scope") or []
        H.append(f'<details class="big"><summary>Selected papers ({len(docs)})</summary>'
                 "<table><thead><tr><th>type</th><th>year</th><th>cited</th><th>text</th><th>sub-q</th><th>title</th></tr></thead><tbody>"
                 + rows + "</tbody></table>")
        if extra:
            H.append('<p class="mute">excluded as out of scope: ' + " ".join(
                E((recs.get(k, {}).get("title") or k)[:90]) for k in extra) + "</p>")
        H.append("</details>")

    # screening
    if scr:
        rows = []
        for r in scr:
            rec = recs.get(r["key"], {})
            v = r.get("relevant")
            rows.append(f'<tr><td>{E(r["sid"])}</td>'
                        f'<td>{badge("yes" if v else "no" if v is False else "?", "ok" if v else "mute")}</td>'
                        f'<td>{badge("out of scope", "warn") if r.get("in_scope") is False else ""}</td>'
                        f'<td>{badge(r.get("evidence_type", ""), TYPE_CLASS.get(r.get("evidence_type"), ""))}</td>'
                        f'<td>{badge("selected", "ok") if r["key"] in docs else ""}</td>'
                        f'<td>{E((rec.get("title") or r["key"])[:110])}</td>'
                        f'<td class="mute">{E(r.get("reason", ""))}</td></tr>')
        H.append(f'<details class="big"><summary>Screening decisions ({len(scr)})</summary>'
                 '<input class="filter" placeholder="filter rows…" oninput="flt(this)">'
                 '<div class="tbl"><table><thead><tr><th>sub-q</th><th>relevant</th><th>scope</th><th>type</th><th></th>'
                 "<th>title</th><th>reason</th></tr></thead><tbody class=\"rows\">" + "".join(rows) + "</tbody></table></div></details>")

    # queries, chaining, stages
    if qidx or chain:
        rows = "".join(f'<tr><td>{E(v["qid"])}</td><td>{E(v["sort"])}</td><td>{E(v.get("hit_count", "-"))}</td>'
                       f'<td>{E(v["n"])}</td><td><code>{E(v["query"])}</code></td></tr>'
                       for v in qidx if v["sort"] == "relevance")
        H.append('<details class="big"><summary>Searches and chaining</summary>'
                 "<table><thead><tr><th>id</th><th>sort</th><th>hits</th><th>kept</th><th>query</th></tr></thead><tbody>"
                 + rows + "</tbody></table>")
        if chain:
            H.append(f'<p class="mute">chaining: {len(chain.get("seeds", []))} seed paper(s), '
                     f'{chain.get("candidates", 0)} linked, {len(chain.get("added", []))} added</p>')
        st = man.get("stages", {})
        fails = [f'{k}: {v["failed"]}' for k, v in st.items() if v.get("failed")]
        if fails:
            H.append('<p class="mute">failures: ' + E("; ".join(fails)) + "</p>")
        H.append("</details>")

    # claim repairs
    reps = [e for e in ev.values() if e.get("claim_original")]
    if reps:
        H.append(f'<details class="big"><summary>Claim repairs ({len(reps)})</summary>')
        for e in reps:
            H.append(f'<div class="pt"><div class="why">{badge(e["eid"], "mute")}</div>'
                     f'<div class="mute">before: {E(e["claim_original"])}</div><div>after: {E(e["claim"])}</div></div>')
        H.append("</details>")

    H.append("</section>")
    return "\n".join(H), {"id": cid, "anchor": anchor, "flagged": len(flagged), "unver": len(unver),
                          "rej": len(rej), "unsel": len(rel_unsel)}


CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a18;--mute:#6b6b66;--line:#e2e1dc;--card:#fff;--ok:#1f7a4d;--okbg:#e6f4ec;
--warn:#8a5a00;--warnbg:#fdf3e0;--bad:#9b2226;--badbg:#fbeaea;--info:#2a5d9f;--infobg:#e9f0fa;--accent:#3b5bdb}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e8e4;--mute:#9a9a94;--line:#2e2e34;--card:#1e1e24;
--ok:#6bd39b;--okbg:#14301f;--warn:#e0b060;--warnbg:#332713;--bad:#f09496;--badbg:#3a1a1b;--info:#8db4ea;--infobg:#17243a;--accent:#8da2fb}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 80px}
.wrap.top{padding-bottom:0}
header{border-bottom:1px solid var(--line);margin-bottom:24px;padding:28px 0 16px}
h1{font-size:22px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}
h3{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:26px 0 8px}
h4{font-size:14px;margin:18px 0 6px}
section{border-top:1px solid var(--line);padding-top:28px;margin-top:28px;scroll-margin-top:60px}
.q{font-size:16px;font-weight:600;margin:0 0 4px}
.meta,.mute{color:var(--mute);font-size:13px}
nav{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 0;z-index:5;margin-bottom:-1px}
nav a{display:inline-block;margin:2px 8px 2px 0;padding:3px 10px;border:1px solid var(--line);border-radius:999px;
color:var(--fg);text-decoration:none;font-size:13px}
nav a:hover{border-color:var(--accent);color:var(--accent)}
nav a .n{color:var(--bad);font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:0}
.xscroll{overflow-x:auto;margin:6px 0 14px;max-width:100%}
td{overflow-wrap:anywhere}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);font-weight:600}
a{color:var(--accent)}
code{overflow-wrap:anywhere;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--card);padding:1px 4px;border-radius:4px}
.b{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:999px;margin:1px 4px 1px 0;
background:var(--card);border:1px solid var(--line);color:var(--mute);white-space:nowrap}
.b.ok{background:var(--okbg);border-color:transparent;color:var(--ok)}
.b.warn{background:var(--warnbg);border-color:transparent;color:var(--warn)}
.b.bad{background:var(--badbg);border-color:transparent;color:var(--bad)}
.b.info{background:var(--infobg);border-color:transparent;color:var(--info)}
.b.mute{white-space:normal}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;min-width:92px}
.tile b{display:block;font-size:19px;line-height:1.2}
.tile span{font-size:11.5px;color:var(--mute)}
details{border:1px solid var(--line);border-radius:10px;background:var(--card);margin:8px 0;padding:0 14px}
details[open]{padding-bottom:12px}
summary{cursor:pointer;padding:10px 0;font-weight:600;font-size:14px}
summary:hover{color:var(--accent)}
details.big>summary{font-size:15px}
.eye details{background:transparent}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.pair .qt{font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);border-left:3px solid var(--line);
padding:8px 10px;border-radius:0 6px 6px 0;color:var(--fg)}
.pair .c{font-size:14px}
.marks{margin-top:4px;font-size:12px;color:var(--warn)}
.pt{padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}
.pt.bad{border-left:3px solid var(--bad);padding-left:10px}
.pt.fixed{border-left:3px solid var(--warn);padding-left:10px}
.pt .sid{color:var(--mute);font-size:12px;margin-right:6px}
.why{margin-top:4px}
.gaps{font-size:13px;color:var(--mute);padding:6px 0 10px;border-bottom:1px solid var(--line)}
.report{font-size:14.5px}
.report h3{text-transform:none;letter-spacing:0;color:var(--fg);font-size:17px;margin:20px 0 6px}
.report h4,.report h5{font-size:15px;margin:16px 0 4px}
.report sub{font-size:11px;color:var(--mute)}
.report strong{color:var(--fg)}
.filter{width:100%;max-width:340px;padding:6px 10px;margin:6px 0;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--fg);font-size:13px}
.tbl{max-height:70vh;overflow:auto}
.tbl .xscroll{margin:0}
.hide{display:none}
@media(max-width:760px){.pair{grid-template-columns:1fr}}
"""

JS = """
function flt(inp){var q=inp.value.toLowerCase(),box=inp.nextElementSibling;
if(!box)return;var rows=box.querySelectorAll('tbody tr, .row');
rows.forEach(function(r){r.classList.toggle('hide',q&&r.textContent.toLowerCase().indexOf(q)<0)});}
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="eval folder (data/lake-evals/<stamp>) or a single lake folder")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    root = Path(a.target).expanduser().resolve()
    scores = read_json(root / "scores.json", None)
    if scores is not None:
        lakes = []
        by_slug = {}
        for d in sorted(p for p in root.iterdir() if (p / "manifest.json").exists()):
            by_slug[d.name] = d
        used = set()
        for s in scores:
            d = Path(s["lake"]) if s.get("lake") and Path(s["lake"]).exists() else None
            if d is None or not (d / "manifest.json").exists():
                d = next((x for n, x in by_slug.items() if n not in used), None)
            if d:
                used.add(d.name)
                lakes.append((d, s))
        for n, d in by_slug.items():
            if n not in used:
                lakes.append((d, None))
    elif (root / "manifest.json").exists():
        lakes = [(root, None)]
    else:
        raise SystemExit(f"{root} has neither scores.json nor manifest.json")

    body, navs = [], []
    for i, (d, s) in enumerate(lakes, 1):
        sec, nav = lake_section(d, i, s)
        body.append(sec)
        navs.append(nav)

    head = [f"<h1>Evidence lake review — {E(root.name)}</h1>"]
    sm = root / "scores.md"
    if sm.exists():
        head.append(f'<div class="report">{md(sm.read_text())}</div>')
    nav = "".join(
        f'<a href="#{n["anchor"]}">{E(n["id"])}'
        + (f' <span class="n" title="claims and points needing review">{n["flagged"] + n["unver"]}</span>'
           if n["flagged"] + n["unver"] else "")
        + "</a>" for n in navs)
    page_body = "".join(body).replace("<table>", '<div class="xscroll"><table>').replace("</table>", "</table></div>")
    head_html = "".join(head).replace("<table>", '<div class="xscroll"><table>').replace("</table>", "</table></div>")
    out = Path(a.out).expanduser() if a.out else root / "review.html"
    out.write_text(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Lake review — {E(root.name)}</title><style>{CSS}</style></head><body>"
        f'<div class="wrap top"><header>{head_html}</header></div>'
        f'<nav><div class="wrap">{nav}</div></nav>'
        f'<div class="wrap">{page_body}</div>'
        f"<script>{JS}</script></body></html>")
    print(out)


if __name__ == "__main__":
    main()
