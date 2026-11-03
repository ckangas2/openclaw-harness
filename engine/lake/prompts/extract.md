You are extracting evidence from one paper for one sub-question of a literature review. Use only the passages shown below; do not add anything you know from elsewhere.

Sub-question:
{{subquestion}}

Paper: {{paper}}

Passages (each starts with its id in square brackets):
{{passages}}

Return up to 6 claims from these passages that bear on the sub-question. For each claim:
- "quote": one or two consecutive sentences copied character for character from a single passage. Do not shorten, merge or reword them; the quote is checked against the text.
- "passage": the id of the passage the quote comes from, e.g. "s3p2".
- "claim": one sentence saying what the quote shows, in your words, saying no more than the quote does. Keep any numbers exactly as written. If this paper presents its own method and the quote is about that method, say so (e.g. "The authors report that their method ..."); do not call other methods theirs.
- "source": "this_paper" if the quote reports what this paper itself did or found; "cited_work" if it reports what another study found (e.g. "Xi and Li showed ...", "previous benchmarks reported ...").
- "kind": "finding" (a result), "limitation" (a weakness, failure or caveat), "method_detail" (how something was done, e.g. datasets or metrics), or "context" (background the authors state).

Prefer results with numbers, direct comparisons between methods, and stated limitations. If no passage bears on the sub-question, return {"claims": []}. Return JSON only, in the given schema.
