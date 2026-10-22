# Results log

Witness: Rod Ismay, first witness statement (WITN04630100), 78 paragraphs.
Pipeline: retrieve top 5 paragraphs → LLM picks a paragraph, copies a quote and a lead-in id → quote verified against the paragraph text.

## Dev set

`eval/ismay_questions.jsonl`, 35 questions written against the statement:

| Category | n | What it tests |
|---|---|---|
| direct | 12 | answerable from one paragraph, using its wording |
| paraphrased | 8 | answerable, worded to avoid the paragraph's key words |
| multi | 4 | answer spans 2+ paragraphs |
| docref | 5 | cites a document ID the way counsel would; 1 of the 5 cites an ID that is not in the statement |
| trap | 6 | not covered: 3 off-topic, 3 plausible Inquiry topics the statement does not address |

The docref category was added after the first ablation, so tables marked n=30 predate it.

Metrics: hit@1 / hit@k = an expected paragraph is first / in the top 5. Para accuracy = the answer cites an expected paragraph (answerable questions only). Grounded = non-refused answers whose quote verified without falling back. Refusal precision / recall = traps among refusals / refusals among traps.

**Caveat:** this set was used to make every choice below, including `DENSE_MIN_SCORE=0.56`, which was picked by looking at these questions' top dense scores. Dev-set numbers are therefore optimistic; the held-out set below is the honest measure.

### Retrieval ablation (retrieval only, n=35)

| Variant | direct @1 | @k | paraphrased @1 | @k | multi @1 | @k | docref @1 | @k | all @1 | all @k |
|---|---|---|---|---|---|---|---|---|---|---|
| bm25 | 100% | 100% | 0% | 0% | 75% | 100% | 100% | 100% | 68% | 71% |
| dense (bge-small-en-v1.5) | 100% | 100% | 75% | 88% | 75% | 100% | 50% | 75% | 82% | 93% |
| hybrid, dense weight 0.5 | 100% | 100% | 0% | 25% | 75% | 100% | 75% | 100% | 64% | 79% |
| hybrid, dense weight 0.6 | 100% | 100% | 0% | 25% | 75% | 100% | 75% | 100% | 64% | 79% |
| hybrid, dense weight 0.75 | 100% | 100% | 12% | 88% | 75% | 100% | 75% | 100% | 68% | 96% |
| hybrid, dense weight 0.9 | 100% | 100% | 50% | 88% | 75% | 100% | 75% | 75% | 79% | 93% |

HyDE (LLM writes a hypothetical statement passage, which is searched instead of the question), n=30:

| Variant | direct @1 | @k | paraphrased @1 | @k | multi @1 | @k | all @1 | all @k |
|---|---|---|---|---|---|---|---|---|
| hybrid + HyDE | 58% | 92% | 25% | 100% | 100% | 100% | 54% | 96% |
| dense + HyDE | 58% | 83% | 38% | 62% | 100% | 100% | 58% | 79% |

What it showed:
- BM25 fails completely on paraphrase: once the question avoids the paragraph's words, the right paragraph ranks 31st–57th.
- Dense fixes paraphrase (88% @k) but is weak on document IDs, which embeddings do not match exactly.
- Equal-weight fusion (RRF, k=60) throws dense's advantage away: a paragraph that is dense rank 1 but BM25 rank 20 loses to one that is mediocre on both. Weighting dense at 0.75 recovers @k but not @1.
- HyDE hurts with this LLM (qwen3.5-4b): rewrites refused ("I am an artificial intelligence"), invented facts ("31 March 2024") or referred to "Question 8"; trap rewrites read like statement boilerplate and scored higher than real answers, which breaks the refusal gate. Rewrites also differ run to run. HyDE stays off.
- Chosen: dense retrieval, with document-ID questions routed separately (below).

### Relevance check (dense, n=35, single run, before document-ID routing)

An extra LLM call asked whether any retrieved paragraph directly answers the question; "no" meant refusing.

| | para acc | grounded | refused | refusal precision | refusal recall |
|---|---|---|---|---|---|
| without check | 93% | 96% | 8 | 88% | 100% |
| with check | 75% | 100% | 14 | 50% | 100% |

The check rejected 11 questions. 5 of them (p03, p05, p08, m01, m03) had the right paragraph retrieved and were answered correctly without the check; it saved one wrong answer (r02). The 4 traps it rejected were already being refused by the answer step, so it added no trap recall. It costs an extra ~0.6 s and an LLM call per question. `RELEVANCE_CHECK_ENABLED=false`.

### Document-ID routing

Questions citing an ID matching `DOC_ID_PATTERN` (`\b[A-Z]{3,4}\d{8}\b`) are answered from the paragraphs that mention it, ordered by dense score; an ID not in the statement gets a fixed "I don't recognise that document reference" answer without an LLM call. Docref went from 50% / 75% (@1 / @k) with dense alone to 100% / 100%.

### Final dev-set result

Dense + routing, `DENSE_MIN_SCORE=0.56`, relevance check off, HyDE off, `LLM_TEMPERATURE=0`, 3 repeats (`outputs/eval/20261019-110136/`). Every quality metric was identical across the 3 runs, and so was every per-question answer; only latency varied.

| | direct | paraphrased | multi | docref | trap | overall |
|---|---|---|---|---|---|---|
| n | 12 | 8 | 4 | 5 | 6 | 35 |
| hit@1 | 100% | 75% | 75% | 100% | – | 89% |
| hit@k | 100% | 88% | 100% | 100% | – | 96% |
| para accuracy | 100% | 88% | 100% | 100% | – | 96% |
| grounded | 100% | 100% | 100% | 100% | – | 100% |
| para corrected | 0 | 0 | 1 | 0 | 0 | 1 |
| refused | 0 | 1 | 0 | 1 | 6 | 8 |
| refusal precision | – | 0% | – | 100% | 100% | 88% |
| refusal recall | – | – | – | 100% | 100% | 100% |
| mean latency, ms (min–max over runs) | 1124 (1036–1274) | 1268 (1052–1690) | 1316 (1160–1629) | 1061 (920–1299) | 504 (498–516) | 1064 (1008–1100) |

Latency excludes time spent waiting on LLM gateway rate limits (90–186 s per run, recorded separately). The 3 off-topic traps and the unknown-ID question are refused without an LLM call; the 4 plausible traps are refused by the answer step.

### Quote length

Long quotes made long answers: the ¶64 answer read the whole 89-word paragraph, 35 s of audio. The answer prompt now asks for only the sentence(s) that answer the question, within `QUOTE_MAX_WORDS`. The model often ignores this, so when its quote is over the limit, a follow-up LLM call (`prompts/quote_select.txt`) sees the quote split into numbered clauses and picks the ones that answer the question. The chosen clauses are kept in their original order while they fit the limit, joined with "…" where clauses are skipped, so the result still passes quote verification. If the reply is unusable, the quote is trimmed to whole sentences instead.

Answers that needed the follow-up call (3 repeats each, 31 LLM answers per run). Latency excludes rate-limit waits; medians are shown because run 3 at 60 words had two slow gateway calls (10.9 s and 15.0 s) that inflate the means.

| `QUOTE_MAX_WORDS` | Follow-up calls per run | Questions | Latency with follow-up, median (mean) | Latency without, median (mean) |
|---|---|---|---|---|
| 40 (`outputs/eval/20261021-234004/`) | 9, 7, 8 | d10, m01, m02, m03, m04, p04, r01, r02, r04 (varied by run) | 2073 ms (2233) | 1038 ms (1158) |
| 60 (`outputs/eval/20261022-204728/`) | 3, 3, 3 | m02, p04, r01 | 2352 ms (2538) | 1091 ms (1553) |

The follow-up call itself took 0.9–1.2 s. At 60 words, r02 and m04 fit without one and answer the whole question.

The three answers still over 60 words:

| | Question | Before: trimmed to whole sentences (40 words) | After: clause selection (60 words) |
|---|---|---|---|
| r01 (¶64, 89 words) | What did you make of POL00055418? | Regarding POL00055418 (Emails: The Horizon Trial), given that the vast majority of post offices were understood not to have significant concerns about Horizon, and because of reassurance from audit risk modelling, feedback from audit, training | I did not have serious concerns about the system myself, but I would have been equally keen to be aware of new concerns arising in case this would enable me and my team to positively support the resolution of something if it became a confirmed issue. |
| m02 (¶43, 74–105 words) | Looking back, what would you have done differently with the 2010 report? | In hindsight | With the benefit of hindsight I would therefore have summarized the same lists of controls, but would not have made such an assertive executive summary. I should simply have stated that these were the mitigating factors noted from discussions with the report addressees or with their representatives, and allowed those to be considered by the recipients. |
| p04 (¶56, 76 words) | Did you yourself go and examine the money and goods held at individual outlets? | I did not personally carry out audits. I was, however, responsible early in my time at Post Office Ltd for the Branch Audit Team whose members carried out audits at branches. | I did not personally carry out audits. |

Quality metrics at 60 words matched the final table above in every run but one. In run 3, m03's reply contained two JSON objects (one per paragraph, ¶35 and ¶36, trying to answer both halves of the question). The parser's `\{.*\}` regex spanned both and failed, so the answer fell back to the first sentence of the top paragraph and was marked unverified: grounding was 92% for multi and 99% overall in that run, 100% in the other two. Parsing only the first JSON object would fix it; not yet done.

### Known failure: p06

"Did your own department rely on the same IT as sub-postmasters in its day-to-day operations?" should be answered by paragraph 28 ("My teams primarily worked using a central SAP finance system, not Horizon…"). Dense ranks paragraph 28 21st, so the answer step never sees it and the witness says the point is not covered. This one question accounts for the paraphrased para accuracy of 88%, the refusal precision of 88% and the overall para accuracy of 96%.

### Known limitation: answers quote one paragraph (m03)

"Why did branches use the other postage button, and how did the stock adjustment project respond?" has two parts, answered in different paragraphs: ¶35 (why branches used "other postage") and ¶37 (how the stock adjustment project responded). An answer quotes a single paragraph, so the witness answers only the first part:

> My recollection is that branches would sometimes select "other postage" as a fast way to process a more complex transaction, with the expectation that later in the day they would reverse the "other postage" transaction and put the correct transaction through.

The eval counts this as correct, because ¶35 is one of the expected paragraphs; the metrics don't check that every part of a question is answered. The same applies to any question whose answer spans paragraphs (the multi category). The model has also tried to answer such a question with two JSON objects, one per paragraph (see "Quote length" above).

## Held-out results (counsel questions)

`eval/ismay_heldout.jsonl`: 20 of counsel's questions from Mr Ismay's oral evidence on 11 May 2023 (transcript INQ00001063), labelled against the first written statement. Not yet run.

_To be filled in._
