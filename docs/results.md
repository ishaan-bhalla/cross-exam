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

### Known failure: p06

"Did your own department rely on the same IT as sub-postmasters in its day-to-day operations?" should be answered by paragraph 28 ("My teams primarily worked using a central SAP finance system, not Horizon…"). Dense ranks paragraph 28 21st, so the answer step never sees it and the witness says the point is not covered. This one question accounts for the paraphrased para accuracy of 88%, the refusal precision of 88% and the overall para accuracy of 96%.

## Held-out results (counsel questions)

`eval/ismay_heldout.jsonl`: 20 of counsel's questions from Mr Ismay's oral evidence on 11 May 2023 (transcript INQ00001063), labelled against the first written statement. Not yet run.

_To be filled in._
