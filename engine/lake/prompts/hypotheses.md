You are reviewing the evidence collected for one research question. Instead of writing a single account, set out the competing interpretations that this evidence allows.

Question:
{{question}}

Sub-questions the evidence was collected for:
{{subquestions}}

Evidence (id, kind of source, first author and year, claim, supporting quote):
{{evidence}}

Give between one and three interpretations. An interpretation is a distinct answer to the question, not the same answer with a different emphasis. Give more than one only if the evidence genuinely supports more than one. If it points one way, give one, set "only_one_supported" to true and say why in "note". Never invent an interpretation to fill a slot.

For each interpretation:
- "label": a few words naming it.
- "statement": one or two sentences stating it, with "eids" for the items it rests on most directly.
- "supporting": up to 4 points, one or two sentences each, each with "eids", saying what evidence favours it. Say only what the cited items say; copy numbers exactly; say who found it (an independent comparison, a method's own authors, a review, or a study citing another study).
- "unexplained": up to 3 points, each with "eids", naming evidence in the list that this interpretation does not explain or that cuts against it. Leave it empty only if there is none.
- "would_distinguish": one sentence naming an observation or experiment whose result would separate this interpretation from the others. This is a proposal, not a finding, and cites nothing.

Cite only ids from the evidence list. Return JSON only, in the given schema.
