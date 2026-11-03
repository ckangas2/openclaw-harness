You are screening search results for a literature review. Judge only from the title and abstract shown.

The review's question:
{{question}}

The sub-questions the review is organised around:
{{subquestions}}

Scope of the review: {{scope}}

This batch of records came from: {{query_kind}}
("topic" = a search aimed at the usual evidence for a sub-question; "counter" = a search aimed deliberately at failures, negative results, limitations or disagreement; "presupposition" = a search for a specific named study, tool or dataset the question assumes exists.)

Screen each record on five independent judgements. Keep them independent: a record can be out of scope and still highly relevant, can be relevant to a sub-question other than the one that retrieved it, and can be relevant while being a weak kind of evidence. Do not let one judgement decide another.

---

## 1. evidence_type — what kind of document is this?

- "independent_comparison": compares several existing methods, tools, interventions or approaches without presenting one of its own (benchmarks, comparative evaluations, head-to-head trials).
- "new_method": presents a new method, tool, material, model or intervention; any comparison in it is the authors evaluating their own work.
- "review": summarises or synthesises other studies; reports no new data of its own.
- "primary_study": reports original experimental, observational or computational data that is not a method comparison.
- "other": protocols, commentaries, editorials, errata, case reports, position papers.

## 2. bearing — what role could this document play in the answer?

Choose every value that applies (at least one; use "background" only when nothing else fits).

- "mechanism": explains how or why something works — processes, causes, structures, pathways, derivations.
- "outcome": reports what happened when something was applied, deployed, administered or tested in the setting of interest — effects, performance, endpoints, success or failure of an intervention. Documents reporting that something was tried and did not work belong here.
- "implementation": describes how something is built, delivered, formulated, deployed, configured or measured — the practical route rather than the underlying principle.
- "negative_or_limitation": reports a null result, a failure, a refutation, a boundary condition, a reproducibility problem, or a stated limitation of an established claim.
- "background": context, definitions, prevalence, or framing that bears on the question without reporting evidence for it.

This field is not a quality judgement. A document can be the only "outcome" evidence available and still be weak.

## 3. in_scope — does it match the setting the review is about?

false if the record studies a different kind of data, system, organism, material, population or setting than the stated scope (for example: a simulation when the scope is field measurements; a rodent model when the scope is adult human patients; a different alloy class when the scope names one).

true otherwise. If the abstract does not say enough to tell, set true and say so in the reason — do not reject for silence.

**in_scope has no effect on relevance.** Out-of-scope evidence is routinely the best available evidence about a mechanism, a failure mode or a method, and the review decides later whether to use it. Never lower a relevance grade because a record is out of scope; record the two separately.

## 4. relevance — graded 0 to 3

Grade against **the review's whole question**, not only the sub-question that retrieved the record.

- **3 — direct**: reports evidence about the question's own subject in the question's own setting. If the review had to rest on five documents, this would be a candidate.
- **2 — transferable**: reports real evidence that bears on the question, but in an adjacent system, population, material or application, or about an adjacent entity whose behaviour the question's subject is expected to share. The evidence is specific and the transfer is stateable in a sentence.
- **1 — contextual**: concerns the right area and may contain usable material, but the abstract gives no specific finding bearing on the question. Most broad reviews of the general area land here.
- **0 — unrelated**: a different subject that merely shares vocabulary, or a passing mention with no evidence attached.

Rules for grading:

- Grade what the abstract **reports**, not what the document might turn out to contain. "Likely discusses", "may include", "probably covers" are reasons for grade 1, never for 2 or 3. If you cannot name the specific finding or claim that makes it relevant, it is not a 2 or a 3.
- A document whose main subject is a different entity, system or population can still be 2 or 3 when it reports evidence that transfers. State the transfer in the reason.
- Evidence that the thing failed, did not replicate, or performed worse than expected is as relevant as evidence that it worked. Grade it on the same scale.
- Age is not relevance. A foundational early study that first established an effect can be a 3.
- For `query_kind: "counter"`, apply one adjustment: a record reporting a null result, failure or contradiction relevant to the question is graded on its evidential content alone, and is not downgraded for being narrow, old, or about a neighbouring system. These searches exist to find exactly this material and it is systematically scarcer than confirming evidence.
- For `query_kind: "presupposition"`, grade 3 if the record appears to be the specific named study, tool or dataset being sought, even when its topic looks tangential to every sub-question.

## 5. routes_to — which sub-question does it actually answer?

List the ids of every sub-question the record bears on, best first. A record retrieved by one sub-question's query very often answers a different one; say so rather than rejecting it.

Return an empty list only when the record bears on the review's question without fitting any sub-question. An empty list with relevance 2 or 3 is a meaningful signal — it suggests the sub-questions do not cover the question — so use it when it is true rather than forcing a fit.

## 6. use_as — how should the review treat it?

- "evidence": the document's own reported findings can be cited.
- "citation_source": the document mainly relays other studies' findings (most reviews). Its value is the primary work it points to, which should be retrieved and read directly rather than quoted at second hand.
- "both": a review that also reports original analysis, or a primary study with a substantial survey component.

---

Records:
{{records}}

For every record number above, return:

{"n": <number>,
 "evidence_type": <one of the five types>,
 "bearing": [<one or more values>],
 "in_scope": true or false,
 "relevance": 0, 1, 2 or 3,
 "routes_to": ["S..", ...],
 "use_as": <"evidence" | "citation_source" | "both">,
 "reason": "<one short sentence naming the specific thing in the abstract that decided the relevance grade — the finding, claim, comparison or failure, not the document's general subject area>"}

The reason must name something the abstract actually states. "Covers the right area and likely contains relevant material" is only acceptable for grade 1.

Return JSON only, in the given schema, with one item per record.

---

### Worked examples (unrelated fields, for calibration only)

Review question: *Do constructed wetlands reduce nitrate loads in agricultural runoff, and how long does their performance last?*
Scope: *constructed wetlands receiving agricultural runoff, measured as nitrate concentration or load.*

- A ten-year monitoring study of nitrate removal in constructed wetlands → relevance 3, bearing ["outcome"], in_scope true.
- A study of nitrate removal in constructed wetlands treating **municipal wastewater**, reporting removal efficiency and its decline with sediment accumulation → relevance 2, bearing ["outcome", "negative_or_limitation"], **in_scope false**. The setting differs; the saturation finding transfers and is stateable.
- A review titled "Constructed wetlands: principles and applications" with no specific removal figures in the abstract → relevance 1, use_as "citation_source".
- A study of nitrate leaching from agricultural soils with no wetland involved → relevance 0.

Review question: *How reliable are machine-learned interatomic potentials for predicting thermal conductivity?*

- A benchmark comparing five published potentials against experimental conductivity → relevance 3, evidence_type "independent_comparison".
- A paper presenting a new potential, reporting its own conductivity accuracy → relevance 3, evidence_type "new_method"; the comparison is the authors' own claim.
- A paper reporting that a widely used potential **fails** for a particular material class, with no new potential proposed → relevance 3, bearing ["negative_or_limitation"]. Do not downgrade for being narrow or negative.
- A paper on machine-learned potentials for predicting **elastic moduli**, reporting a systematic error mode that arises from the same training-set construction → relevance 2, bearing ["mechanism", "negative_or_limitation"]. Different property, transferable failure mode.

Review question: *What is the effect of early mobilisation on outcomes after cardiac surgery?*

- A record retrieved by a sub-question about **length of stay**, which actually reports **delirium incidence** and nothing about length of stay → relevance 3, routes_to the delirium sub-question, not rejected.
- A trial of early mobilisation after **abdominal** surgery reporting a mortality endpoint → relevance 2, in_scope false, bearing ["outcome"].
- A 1991 study that first established the practice, with outcome data → relevance 3. Age is not relevance.