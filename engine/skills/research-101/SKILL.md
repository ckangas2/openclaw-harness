---
name: research-101
description: How to choose and use sources before answering a question, writing a research brief, or citing anything. Read it before you start searching.
---

# research-101: choosing sources before you answer

1. Classify the question first.
   - "How do I do X with tool Y" → Y's own documentation and release notes for the version in use, then the method's paper.
   - "Does X work / which is best" → systematic reviews and independent benchmarks, then primary studies.
   - "What is known about X" → recent reviews, then the primary papers they cite.
   - Calculations → work them out step by step and show the arithmetic; source any constant you did not derive.
2. Search for sources, not answers. Name the tool, method, paper, dataset or document you need. Try 2–3 phrasings.
   Frame searches neutrally: "relationship between X and Y", not "evidence that X causes Y".
   Look for contradicting evidence and for silence (nobody has tested it) as hard as for support, and report all three.
3. Follow citations: backward (the references of a good paper) and forward (newer papers that cite it).
4. Judge each study, not just where it appeared: Is it directly about this question? Do other studies agree?
   How large and precise is the effect? Could its design mislead (sample size, unit of replication, confounds)?
   Peer review counts in its favor only when the evidence is otherwise equal.
5. Prefer the original. A blog, news item or review repeating a claim points to the source; it is not the source.
5b. If a publisher page is blocked or paywalled, look for an open full-text copy instead of relying on the snippet:
   for life-science papers, Europe PMC's API (https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=<terms>&format=json,
   then .../rest/<PMCID>/fullTextXML for open-access full text); otherwise the preprint (arXiv, bioRxiv) or the authors' copy.
6. Check it still stands: note dates and versions. For a paper with a DOI, check for retractions or corrections:
   https://api.crossref.org/v1/works/<DOI> lists them under "update-to" (type "retraction" or "correction").
7. Every claim needs a passage. Cite only what a page you opened actually says. Mark your own reasoning as inference.
   Numbers need a primary source, or the label "starting point, adjust for your case".
8. Two independent primary sources make a finding; one is a lead, so say so.
9. If you cannot find a source for something the user asked about (a paper, a tool, a result), say that plainly.
   Do not reconstruct what it probably says.
10. When sources disagree, report the disagreement and say which you followed and why.
11. Stop when new sources stop changing the picture.
