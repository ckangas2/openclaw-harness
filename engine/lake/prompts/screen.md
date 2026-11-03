You are screening search results for one sub-question of a literature review. Judge only from the title and abstract shown.

The review's question:
{{question}}

This sub-question:
{{subquestion}}

Scope of the review: {{scope}}

For each record, first classify what kind of evidence it is ("evidence_type"):
- "independent_comparison": compares several existing methods, tools or approaches without presenting a new one of its own (benchmarks, comparative evaluations).
- "new_method": presents a new method or tool; any comparison in it is the authors evaluating their own method.
- "review": summarizes or synthesizes other studies.
- "primary_study": reports original experimental or observational data that is not a method comparison.
- "other": anything else (protocols, commentaries, unrelated work).

Then decide "in_scope": false if the record studies a different kind of data, technology, organism or setting than the scope (e.g. a mouse study when the scope is adult human patients, or an imaging method when the scope is sequencing), true otherwise.

Then decide "relevant": true if the abstract reports evidence that bears directly on the sub-question as part of the review's question (same topic, not just similar methods or words), whether it supports or contradicts the likely answer; false if it only mentions the topic in passing or studies a different question that shares keywords. The evidence type does not decide relevance, but keep it accurate: a new method's comparison to others is the authors' own claim, not an independent one.

Records:
{{records}}

For every record number above, return {"n": <number>, "evidence_type": <one of the types>, "in_scope": true or false, "relevant": true or false, "reason": "<one short sentence naming what in the abstract decided it>"}. Return JSON only, in the given schema, with one item per record.
