# Report anatomy

What a report contains, in order, and why each part is there.

## The problem it solves

> *A report built 80% on reviews reads identically to one built on experiments. One line stating
> the counts would be the most useful sentence in the ohsv-iv-delivery report.* — STATUS.md

Today the report's own summary line is `points unverified: 0`. That is true in the pipeline's
terms — every point passed `check_point` — and a reader takes it to mean the points are
established. On the same run, the grader found 0 of 30 points corroborated by two independent
groups, 16 resting only on relayed material, and 14 on a single paper. Both facts are correct.
Only one of them is in the report.

## Section 1 — What this report is built on

A block, above the summary, before any claim. Every figure is already computed somewhere in the
pipeline; none of it needs a model.

```
WHAT THIS REPORT IS BUILT ON

  13 papers            7 primary studies, 6 reviews
  2 of 13              read as full text; 11 as abstract only (not open access)
  30 points            0 corroborated by two independent research groups
                       16 rest only on findings relayed by another paper
                       14 rest on a single paper
  2 of 2               key papers for this question, cited
  1 premise checked    the named trial exists
  scope                approved by you · transferable evidence used in 4 points
```

Rules for this block:

- **Counts, not adjectives.** No "comprehensive", no "high-confidence".
- **Every line is a number the pipeline already has.** Composition comes from `selected.json`,
  corroboration from the grader, full-text status from `fulltext_status`, key papers from the
  pack.
- **It appears even when the news is bad.** Especially then — that is the whole point.
- **Nothing here is a model's opinion about its own work.**

## Section 2 — Premise check

If the question presupposes a named trial, dataset or tool, whether it was found. A question
about a study that does not exist opens by saying so.

## Section 3 — Summary, then one section per sub-question

Points, each with the evidence ids it rests on. A point that failed its checks twice is printed
and marked **UNVERIFIED**, not removed — a reader is better served by a flagged claim than by a
silent deletion.

Points citing transferable evidence must name the difference (see [scope.md](scope.md)).

## Section 4 — What the evidence does not cover

Two kinds of gap, and they must be distinguishable:

- **Searched and not found.** "No randomised comparison of X and Y appears in the literature
  searched." The system knows this; it ran the query.
- **Not covered.** "The evidence here does not address paediatric dosing." The system does not
  know whether such evidence exists.

Today all gaps are model prose and nothing distinguishes the two. This is the largest remaining
honesty gap in the product, and it is open work: gaps should come from the searches, not from the
model's impression of what is missing.

## Section 5 — References, evidence, and how this was built

Each reference carries its type, whether it was read in full or as an abstract and why, and
preprint or retracted flags. Then every evidence id with its quote, so the report is checkable
on paper. Then the settings and the code version that produced it.

## The audit view

`review.py` renders the same report as a page where clicking a sentence shows its quotes, their
papers, and which checks it passed. This is the feature that distinguishes the product, and it
already exists.

A report should link to it by default.

## What this changes in code

| Part | Status |
|---|---|
| Composition counts | data exists in `selected.json`, not reported |
| Corroboration counts | grader computes them; not wired into the report |
| Full-text vs abstract | reported per reference; not summarised |
| Key-paper recall | `lakemap.py` computes it; not in the report |
| Searched-vs-not-covered gaps | not built |
| Link to the audit view | not built |

Five of the six are plumbing: the numbers exist and are not surfaced. That is what makes this the
cheapest large improvement available.
