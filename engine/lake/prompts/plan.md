You are planning a literature search in Europe PMC. You do not answer the question here; you only decide what to search for.

Question:
{{question}}

First state the "scope": one sentence naming the kind of data, technology, organism or setting the answer must be about, as implied by the question (e.g. "blood samples from adult humans measured by flow cytometry"). Describe the data and setting only, never the type of study (not "benchmarks of ...", "reviews of ..."). Evidence outside it will be set aside later, so include only limits the question actually implies.

Then list under "presupposes" every specific paper, author group, tool, dataset or result the question assumes exists (e.g. a named study or a named method), each as {"name": "<short name>", "query": "<Europe PMC search string that would find it>"}. Use an empty list if the question names nothing specific.

Then split the question into 3 to 5 sub-questions that together answer it. For each sub-question write:
- "text": the sub-question, phrased neutrally as an investigation of what is known, not as a claim to confirm.
- "why": one sentence on why the answer depends on it.
- "queries": 2 Europe PMC search strings that would find primary studies, benchmarks or reviews on it. Each query joins 2 or 3 concepts with AND; within a concept, put synonyms in parentheses joined with OR. Quote only established multi-word terms; never make generic words (method, tool, analysis, few, statistical) required. Use the terms authors in the field would use, including the names of key concepts and well-known approaches. Never use field tags; a separate step adds those.
- "asks_about_outcomes": true if this sub-question asks what happened when something was administered, applied, deployed or tested -- effects, endpoints, survival, response, safety, success or failure of an intervention in the setting of interest. False for sub-questions about mechanisms, definitions, how something is built, or what is known in principle. When true, code adds a further query restricted to clinical-trial reports: relevance ranking buries individual trial reports under the reviews that discuss them, and that restriction is the only reliable way to reach them. Judge the sub-question, not whether you expect trials to exist.
- "counter_query": 1 search string aimed at evidence that would complicate or contradict the most likely answer (limitations, failures, disagreements, negative results).

Example of the style, for an unrelated question ("Do wildlife corridors increase gene flow between fragmented populations?"):
{"id": "S1", "text": "What do empirical studies measure about gene flow across corridors compared with unconnected fragments?", "why": "The question turns on measured gene flow, not on corridor use alone.", "asks_about_outcomes": true, "queries": ["\"habitat corridor\" AND \"gene flow\" AND (fragmentation OR fragmented)", "\"landscape genetics\" AND corridor AND connectivity"], "counter_query": "corridor AND \"gene flow\" AND (\"no effect\" OR ineffective OR limitation)"}

Return JSON only, in the given schema: {"scope": "...", "presupposes": [...], "subquestions": [...]}, with ids S1, S2, ...
