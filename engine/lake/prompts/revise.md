A point in an evidence review failed an automatic check. Rewrite it so that it says only what the evidence supports, or drop it.

Point:
{{point}}

Evidence ids it cites: {{cited}}

Problems found:
{{problems}}

Evidence you may cite (only these):
{{evidence}}

Fix every problem: cite existing ids that actually support the statement, copy numbers exactly as they appear in the cited items, attribute a method's performance to its own authors when only they evaluated it, and mark secondhand findings as reported by the cited study. If the point cannot be supported by this evidence, set "drop" to true.

Return JSON only: {"drop": true or false, "text": "<the rewritten point>", "eids": ["E..", ...]}.
