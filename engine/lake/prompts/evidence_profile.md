You are classifying pieces of evidence for a literature review: for each one, in what system it was observed and by what kind of method. Judge only from the claim and quote shown.

The review's question:
{{question}}

Scope of the review (the system the question is about): {{scope}}

For each item, return:

- "system": where the finding was observed. One of:
  - "human_patients": people, patients, cohorts, clinical samples studied as patients
  - "human_primary_cells": cells or tissue taken from people and studied outside the body
  - "human_cell_line": established human cell lines
  - "animal_in_vivo": living animals (mice, rats, primates, ...)
  - "animal_cells": cells or tissue from animals, studied outside the body
  - "in_vitro_biochemical": purified proteins, cell-free assays, structures
  - "computational": models, simulations, database or sequence analysis only
  - "unclear": the claim and quote do not say
- "system_words": the exact words from the claim or quote that name the system (for example "patients", "C57BL/6 mice", "HeLa cells"). Copy them exactly. Leave empty only if "system" is "unclear".
- "method": how the finding was obtained. One of:
  - "human_genetic_variant": a natural mutation, polymorphism or inherited deficiency in people
  - "genetic_knockout_or_knockdown": deleting, silencing or editing a gene on purpose
  - "blocking_antibody_or_receptor_blockade": antibodies or decoys that block a molecule or its receptor
  - "drug_or_inhibitor": a small molecule or drug that inhibits or activates a target
  - "overexpression_or_recombinant": adding the molecule (recombinant protein, overexpression, agonist)
  - "intervention_trial": a controlled trial in people or animals of a treatment
  - "observational_correlation": association, expression levels, correlation, survival analysis without an intervention
  - "computational_prediction": modelling, docking, inference from data analysis
  - "descriptive_other": description without any of the above, or the quote does not say
- "method_words": the exact words from the claim or quote that show the method (for example "deficient mice", "neutralizing antibody", "correlated with"). Copy them exactly. Leave empty only for "descriptive_other".
- "directness": "direct" if the system is the one the question is about (same organism and setting as the scope), "adjacent" if it is a different organism, cell type, disease or setting, "unclear" if you cannot tell.
- "subject_words": the exact words from the claim or quote naming what the finding is ABOUT - the intervention, method, material, organism or entity under study (for example "ONYX-015", "scDblFinder", "habitat corridors"). Copy them exactly. Leave empty only if the text does not name a subject.
- "setting_words": the exact words naming the conditions it was observed under - how it was administered or applied, on what platform, in which population or material (for example "intratumoral", "systemically", "droplet scRNA-seq", "fragmented populations"). Copy them exactly. Leave empty if the text does not say.
- "scope_match": whether the SUBJECT and SETTING are the ones the review's scope names.
  - "direct": the claim is about the scope's own subject, in the scope's own setting.
  - "adjacent": the claim is about a neighbouring subject, or the same subject in a different setting - a different product or method of the same family, a different route of administration, a different platform, population or material. The finding may still be informative, but it is not about what the scope names.
  - "unclear": the text does not say enough to tell.
  A document can be squarely in scope while a passage inside it is adjacent, which is the usual case for a review: judge this claim, not the paper it came from.

Examples of "adjacent", from three different fields, to show what the judgement is:
- scope names systemic delivery of one named virus; the claim reports a trial of a different virus -> adjacent (different subject).
- scope names systemic delivery; the claim reports a trial of the right agent given by direct injection -> adjacent (different setting).
- scope names droplet single-cell data; the claim reports a benchmark run on a plate-based protocol -> adjacent (different setting).

Evidence items:
{{evidence}}

Return JSON only, in the given schema, with one item per evidence id.
