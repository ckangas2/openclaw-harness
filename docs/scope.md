# Scope

The single highest-leverage object in the system, and currently the weakest.

## The failure this fixes

On `ohsv-iv-delivery` the model wrote this scope:

> systemic administration of oncolytic herpes simplex virus type 1 (HSV-1)

That is a faithful reading of the question. It is also why the report was poor. **18 of the 20
most co-cited papers were screened `relevant=False`, every one of them for a scope reason** —
"poxvirus (JX-594), not HSV-1", "intraneoplastic, not intravenous", "intralesional T-VEC, not
intravenous". Each rejection was individually correct. Together they excluded most of the
evidence that answers the question, because the barriers the question asks about — complement,
neutralising antibody, hepatic sequestration — are platform-agnostic. They do not care which
virus you are using.

Two things were wrong, and they are different problems:

1. **The scope was never shown to the user.** A sentence that decides what counts as evidence
   was written by a model and acted on silently.
2. **The scope had one band.** A paper was in or out. There was no way to say "this is about a
   different agent, and it still bears on the barrier" — which is the true status of most of
   the excluded work.

## The shape

Scope becomes a structured object with three bands, not a sentence.

```json
{
  "core": {
    "subject":    ["oncolytic herpes simplex virus type 1", "oHSV-1", "HSV-1"],
    "setting":    ["systemic administration", "intravenous", "IV"],
    "population": ["patients", "human"],
    "outcome":    ["delivery to tumour", "biodistribution", "viraemia"]
  },
  "transferable": {
    "rule": "evidence about another agent or another route that bears on the same barrier",
    "admits": ["other oncolytic viruses given systemically",
               "intratumoural oHSV where the finding is about a platform-agnostic barrier"],
    "carried_as": "caveat"
  },
  "out": {
    "rule": "not about delivering a replicating agent to a tumour",
    "excludes": ["non-oncolytic gene therapy vectors", "oncolytic virus manufacturing"]
  }
}
```

Three rules govern it:

- **Core is narrow.** It is what the question literally asks.
- **Transferable is a rule, not a list.** It names the mechanism by which other evidence applies
  ("the same barrier"), so screening can judge a paper nobody anticipated.
- **Out is also a rule**, and it must be narrower than "not core". If the out rule and the core
  rule together cover everything, the transferable band is empty and nothing has been gained.

## What each band does downstream

| Band | At screening | At selection | In the report |
|---|---|---|---|
| core | relevant | normal quotas | cited plainly |
| transferable | relevant, flagged | its own quota, so it cannot crowd out core | **cited with the difference stated**: "in a different oncolytic virus given systemically…" |
| out | not relevant | excluded | not cited; counted in the funnel so the exclusion is visible |

The third column is the load-bearing one. Transferable evidence is only honest if the sentence
using it says how it differs. That is a `check_point` rule, in the same family as the existing
rule that relayed findings must be marked as relayed:

> A point citing transferable evidence must name the difference, or it fails the check.

This also closes a reporting hole recorded in STATUS: `excluded_out_of_scope` has always been
zero and structurally cannot be anything else, because the model marks out-of-scope records
irrelevant for the same reason. With three bands, the counts separate and the funnel stops lying.

## The approval step

After `plan`, before `collect`, the user sees:

```
Question  what are the barriers to intravenous delivery of oncolytic viruses?

Core          oncolytic HSV-1 · given systemically · in patients
Transferable  other oncolytic viruses given systemically, and intratumoural work
              where the barrier is platform-agnostic         → cited with the difference stated
Out           non-oncolytic vectors · manufacturing

Sub-questions
  S1  What limits circulation time after intravenous dosing?
  S2  ...

[enter] accept    [e] edit    [w] widen transferable    [n] narrow core
```

Three reasons this is the right interaction:

- It is the one judgement a person makes better than the system, and it takes about ten seconds.
- It is the last point at which a mistake is cheap. After `collect` the mistake is baked into
  928 records and a 15-minute run.
- It makes the scope the user's decision, which is what makes the report theirs to defend.

Unattended runs (`--yes`) accept the proposed scope and record in the manifest that no human
approved it; the report says so in its header.

## What code enforces

The model proposes the bands and copies the words from the question. Code requires:

- core must have a subject and at least one of setting/population/outcome
- the transferable rule must be a sentence naming a mechanism, not a restatement of core
- every word in any band must appear in the question, or in the scope sentence the model wrote
  from it — the same copy-the-words guard used on evidence profiles, so a band cannot be
  invented wholesale
- at least three usable sub-questions, as today

## Open question

Whether `transferable` should be judged per document (at screening) or per claim (at extraction).
The measurement we have says both matter: two reviews on the IV question were correctly in scope
as documents, while the out-of-scope material sat in passages inside them. Per-document is
cheaper and is where this should start. Per-claim is a second step, and should not be built until
a run shows per-document is insufficient.

## CONTROLS row

| Control | Where | Incident | Tested both ways | Expires when |
|---|---|---|---|---|
| Scope has a transferable band, carried as a caveat rather than an exclusion | `plan` + `screen` + `select` + `check_point` | ohsv-iv-delivery, 25 Oct: 18 of the 20 most co-cited papers screened `relevant=False`, every one for a scope reason, while the barriers they describe are platform-agnostic | *not yet run* — needs the dev pack with the band off and on, comparing key-paper recall and the count of points resting on transferable evidence | Screening admits the evidence that answers the question without a second band |
| Scope approved by a person before `collect` | `plan` | Same incident: a model-written sentence decided admissibility and was never shown | *not yet run* | Never: it is consent to a definition, not a capability |
