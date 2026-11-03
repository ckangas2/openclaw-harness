# Evidence lake

Research as a protocol: code runs every step in a fixed order, and the model (Ollama, temperature 0,
seed 42) fills one narrow slot per call with a JSON schema. Everything the model contributes is
checked by code before it is used. One question → one folder (the lake) holding every search,
record, decision, quote and model call, so any sentence in the report can be traced to its source
and the prompt that produced it.

Protocol version: see `PROTOCOL` in `store.py` (each stage records the version it ran under).

## Stages

| Stage | Who | What | Output |
|---|---|---|---|
| plan | model | scope (data and setting only), what the question presupposes, 3-5 neutral sub-questions, 2 queries + 1 counter-query each | `plan.json` |
| collect | code | Europe PMC search per query, by relevance and again by citation count; queries with < 30 hits relaxed by dropping the last AND group; records merged on DOI/title (journal version over preprint); premise searches | `queries/`, `records.jsonl`, `premises.json` |
| screen | model | top 30 per query version, 8 per call: evidence type, in scope, relevant, reason (sees the question, sub-question and scope) | `screen.jsonl` |
| chain | code | references and citing papers of screened independent comparisons and reviews; papers linked by 2+ of them are added and screened | `chain.json` |
| select | code | per sub-question: all independent comparisons and reviews, up to 5 others by type weight + citations; at most 20 papers; rule written into the file | `selected.json` |
| fetch | code | open-access full text (JATS: sections, paragraphs, tables, figure captions); abstract otherwise | `sources/PMC*.json` |
| extract | code + model | code picks the abstract + 8 passages closest to the sub-question (bge-m3); model returns claims with a verbatim quote, passage id, kind, and whether it reports this paper or a cited one | `evidence.jsonl`, `evidence_rejected.jsonl` |
| synthesize | model | 2-6 points per sub-question, each citing evidence ids; gaps | `synthesis_raw.json` |
| check | code + model | every point checked in code; flagged points go back with the problem (and hints), at most 2 rounds, then marked UNVERIFIED; summary written from checked points only, then checked the same way | `synthesis.json` |
| report | code | report with premise check, sections, gaps, numbered references (type, full text or abstract, preprint/retracted), quote appendix, metrics | `report.md`, `metrics.json` |

Every model call is logged to `calls.jsonl`: step, prompt file and its hash, inputs, output,
settings, token counts, and the probabilities of key decision fields where Ollama returns them.

## Checks done in code

- **Quote exists**: each claim's quote must be found in the stored text (exact, or ≥ 92% similar
  after normalizing quotes, dashes, spaces and reference brackets). Otherwise the claim is rejected.
- **Claim says no more than its quote**: no method/tool name and no number that is not in the quote
  or title; "their own/new method" only for papers presenting their own method. One rewrite, then reject.
- **Point cites real evidence**: every evidence id exists; every number appears in the cited
  evidence (numbers from the question are allowed; thousands may be written 17,000 / 17 000).
- **Independence**: "independent benchmark/comparison/study" needs independent comparisons or
  reviews behind it; a method paper's self-evaluation must be attributed to its authors; papers
  sharing authors are not presented as separate studies; secondhand findings must say so.
- **Coverage**: a section resting on one paper, or on no independent comparison, gets a gap line.
- **Premises**: anything the question names (a study, tool, dataset) is searched directly; if not
  found, the summary must say so first.

What code cannot check, and nothing yet does: whether a paraphrase is faithful when it adds no new
name or number, whether a paper is truly in scope (e.g. a bulk RNA-seq comparison in a single-cell
question), and whether the synthesis weighs the evidence well. That is the job of a future
independent checker (a different model reviewing judgment calls).

## Running

```
python3 engine/lake/lake.py run "<question>"            # resumes an existing lake for the same question
python3 engine/lake/lake.py run "<question>" --fresh    # moves the old lake aside, starts over
python3 engine/lake/lake.py <stage> <lake_dir> --force  # redo a stage; clears every later stage's outputs
python3 engine/lake/lake.py status <lake_dir>
```
Lakes: `data/lakes/<slug>/` (`LAKE_ROOT`). Model: `LAKE_MODEL` (default `qwen-q8:latest`),
`OLLAMA_URL`. Limits: `LAKE_COLLECT_MAX`, `LAKE_SCREEN_TOP`, `LAKE_K_PER_SUB`, `LAKE_MAX_DOCS`,
`LAKE_CHAIN_MAX`, `LAKE_RELAX_MIN`.

## Reviewing a run in a browser

```
python3 engine/lake/review.py data/lake-evals/<stamp>     # -> <stamp>/review.html
python3 engine/lake/review.py data/lakes/<slug>      # a single lake
```
`eval.py run` writes this page automatically. It leads with what needs a human eye — claims that
name something absent from their quote, points shipped UNVERIFIED, claims the checks rejected,
papers screened relevant but not selected — then the report, every point with what the checks
caught and changed, each evidence item beside its quote, the selected papers, every screening
decision, the searches and chaining, and the claim repairs. No network, no model calls.

## Evaluating

`packs/deep-research/cases.json` holds the dev cases; `eval.py` builds a fresh lake per case under
`data/lake-evals/<stamp>/` and scores it from lake files only: key papers found / selected / cited,
points fixed and left UNVERIFIED, rejected quotes, independent references, premise and forbidden-name
checks for trap cases, time. `eval.py repro <case>` builds a case twice from scratch and compares
plans, screening, evidence and report (Jaccard per stage).

Held-out cases: same JSON format, kept outside the repo, run with `--cases`, never tuned on.

## History (dev set results)

| Version | Change | Effect |
|---|---|---|
| 0.1-0.3 | plan, collect, screen, select, fetch, extract with quote checks, synthesize, check, report | first end-to-end report on d01 in about 5 min; 3 paraphrases caught as fake quotes |
| 0.4 | scope; stricter independence and shared-author checks | off-topic chromatin papers and "two studies disagree" (same group's protocol) gone |
| 0.5 | premise check, eval pack, repro | nonexistent study reported as not found; two builds from scratch identical |
| 0.6 | citation-sorted searches, DOI/title merge, question in screening, claim-vs-quote checks | d02 key papers 0/3 → 1/3; ~9 duplicates merged per lake; 17 claims repaired |
| 0.7 | query relaxation; number parsing fix | d02 still 1/3: missing papers use words the plan never used |
| 0.8 | citation chaining; secondhand fix | not yet run |
