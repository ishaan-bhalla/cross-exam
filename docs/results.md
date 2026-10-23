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

**Finding: a small model sometimes returns two answers for a two-part question.** Quality metrics at 60 words matched the final table above in every run but one. In run 3, m03's reply contained two JSON objects, one per paragraph (¶35 and ¶36), each answering one half of the question. The parser's `\{.*\}` regex spanned both objects and failed, so the answer fell back to the first sentence of the top paragraph and was marked unverified. Grounding was 92% for multi and 99% overall in that run, and 100% in the other two.

**Fix.** LLM replies are now read by `crossexam.llm_json.parse_json_object`. It tries `json.JSONDecoder().raw_decode` at each `{` in turn and takes the first object that parses and has the fields the caller needs, ignoring any further objects, code fences and surrounding text. The answer step, quote selection and the relevance check all use it. For m03 that means answering from the first object (¶35); answering both halves would need answers that quote more than one paragraph (see "Known limitation" below).

Dev eval after the fix (3 repeats, `outputs/eval/20261022-210024/`): **grounding 100% in all 3 runs**, no fallbacks; hit@1 89%, para accuracy 96% and refusal precision 88% (86–88%), as in the final table. m03 returned a single object in all three runs, so the eval did not exercise the two-object case; `tests/test_llm_json.py` covers it. Refusal recall was 100% in runs 2 and 3 but 83% for traps (86% overall) in run 1: the model answered the Castleton trap (t06) from ¶65 ("I didn't have input into prosecution cases, except in so far as my teams may be providing data in the routine support of a case"). That reply was a single valid object, which the old parser would have read the same way. It is run-to-run variation in the model at temperature 0, not the parser.

### Known failure: p06

"Did your own department rely on the same IT as sub-postmasters in its day-to-day operations?" should be answered by paragraph 28 ("My teams primarily worked using a central SAP finance system, not Horizon…"). Dense ranks paragraph 28 21st, so the answer step never sees it and the witness says the point is not covered. This one question accounts for the paraphrased para accuracy of 88%, the refusal precision of 88% and the overall para accuracy of 96%.

### Known limitation: answers quote one paragraph (m03)

"Why did branches use the other postage button, and how did the stock adjustment project respond?" has two parts, answered in different paragraphs: ¶35 (why branches used "other postage") and ¶37 (how the stock adjustment project responded). An answer quotes a single paragraph, so the witness answers only the first part:

> My recollection is that branches would sometimes select "other postage" as a fast way to process a more complex transaction, with the expectation that later in the day they would reverse the "other postage" transaction and put the correct transaction through.

The eval counts this as correct, because ¶35 is one of the expected paragraphs; the metrics don't check that every part of a question is answered. The same applies to any question whose answer spans paragraphs (the multi category). The model has also tried to answer such a question with two JSON objects, one per paragraph (see "Quote length" above).

## Uploaded statements (spot check)

**Now a development set for uploads.** These 18 questions were used to decide the reranker experiment below, so they are no longer a clean test; the upload result is the held-out set further down.

**A small spot check, not a benchmark:** 18 questions on 3 uploaded statements, 3 repeats at temperature 0 (54 answers). With 4 answerable questions per statement, one question moves a statement's accuracy by 25 points. No retrieval or prompt settings were changed because of it; in particular, `DENSE_MIN_SCORE=0.56` is still the value tuned on Ismay alone.

**Statements** (from the parser test set):
- `witn09180100`: Jeffrey Roper, auditor. Mostly OCR: 15 of 21 pages re-read.
- `witn06700100`: John Breeden. Sub-paragraphs such as 2.6.1 and 103.9.
- `witn09830100`: Patrick Bourke. Long and clean: 168 pages, 294 units.

**Questions:** `eval/upload_spotcheck.jsonl`, written before anything was run, each with the expected unit(s) and why. Per statement:
- 3 answerable: direct, paraphrased, and a third (for Breeden, one whose answer is only in a sub-unit)
- 1 document reference
- 2 traps: one off-topic, and one on-topic for the Inquiry that the witness doesn't cover

**Method:** each PDF went through `POST /api/witnesses/upload` and was polled until ready (16.6 s, 19.0 s and 22.0 s). The questions then went to the session witness's retriever and the normal answer pipeline, the one the examination WebSocket calls (`scripts/run_upload_spotcheck.py`). Results are in `outputs/eval/upload_spotcheck/20261023-134735/`.

| | hit@1 | hit@k | unit accuracy | grounded | refusal precision | refusal recall | fallbacks | quote-shortening calls | gate refused answerable | mean / p95 latency |
|---|---|---|---|---|---|---|---|---|---|---|
| Roper (OCR) | 75% | 75% | 75% | 100% | 100% | 100% | 0 | 0 | 0 | 988 / 1149 ms |
| Breeden (sub-paragraphs) | 25% | 50% | 50% | 100% | 100% | 50% | 0 | 0 | 0 | 1051 / 1921 ms |
| Bourke (long, clean) | 50% | 75% | 75% | 100% | 100% | 67% | 0 | 3 | 0 | 1072 / 2212 ms |
| **Overall** | 50% | 67% | 67% | 100% | 100% | 72% | 0 | 3 | 0 | 1037 / 2163 ms |

Answers were identical in all 3 runs except u2-sub (different quote, same wrong unit), u3-answer (¶235 twice, then ¶240) and u3-trap-on (refused once, answered twice). Latency excludes 156 s of rate-limit waiting.

**One labelling error, mine.** u2-trap-on ("What did you know about Fujitsu's ability to access branch accounts remotely?") was meant as a trap, chosen because "remote access" does not appear in Breeden's statement. But ¶161 covers it in other words: "I was not aware that Fujitsu could put an entry into a branch account remotely via Horizon". The system answered from ¶161, correctly. Counting it as answerable with ¶161 expected, the overall figures would be: unit accuracy 69% (9 of 13 answerable), refusal recall 87% (13 of 15 trap answers), and Breeden's refusal recall 100%. The table above keeps the labels as they were written before the run.

### Each question

| | Question | Expected | Answered from (3 runs) | Quote (shortened here) | Right? |
|---|---|---|---|---|---|
| u1-direct | During any audit you conducted, were errors ever attributed to the Horizon IT system? | 56, 68 | 56 ×3 | "At no point during any audit conducted by me … were errors attributed to the Horizon IT System." | yes |
| u1-para | How did the business decide which branches you would visit, and how often…? | 23, 27 | 28 ×3 | "Crown branches were generally audited every 2 years…" | no: 23 and 27 not retrieved; ¶28 is a related detail |
| u1-answer | When did you first learn that Ms Hall had been prosecuted? | 66 | 66 ×3 | "Until this request for a statement by the Inquiry, I had no knowledge that Ms Hall had been prosecuted." | yes |
| u1-docref | Turning to POL00091046, can you tell us what that document is? | 44(i), 58 | 44(i) ×3 | "audit report of 3 September 2010" | yes (routed; cited at sub-unit level) |
| u1-trap-off | Which football team do you support? | – | refused ×3 (dense gate) | – | yes |
| u1-trap-on | What was your involvement in the … Mediation Scheme? | – | refused ×3 (answer step) | – | yes |
| u2-direct | Was there any right of appeal against a decision to suspend a subpostmaster? | 127 | 127 ×3 | "There was no appeal against the decision to suspend a subpostmaster." | yes |
| u2-para | Before 2014, who had the final say on stopping a postmaster from running their branch…? | 99 | 106 ×3 | "The decision to suspend a subpostmaster or not was based on the discussion held by me with the Contract Advisor." | no: ¶99 not retrieved; ¶106 describes the later, two-tier process |
| u2-sub | Did the auditor's own view … play any part in whether a subpostmaster was suspended? | 103.9 | 152.2 ×3 | "…when further information came to light on the day of the audit, this decision was reversed." (a different case) | no: 103.9 not retrieved (103.5 was) |
| u2-docref | You refer to POL00088579 … Why did you come to write it? | 47, 77 | 47 ×3 | "…the previous owner and author were no longer with POL and the document needed to be updated…" | yes, despite the OCR-damaged ID in ¶47 |
| u2-trap-off | What do you do with your spare time now that you have retired? | – | refused ×3 (dense gate, 0.555 vs 0.56) | – | yes |
| u2-trap-on | What did you know about Fujitsu's ability to access branch accounts remotely? | – (mislabelled) | 161 ×3 | "…I was not aware that Fujitsu could put an entry into a branch account remotely via Horizon…" | counted wrong; actually right (see above) |
| u3-direct | Were you given any specific training on Horizon when you joined…? | 21 | 21 ×3 | "I was not provided with any specific training relating to Horizon." | yes |
| u3-para | How did you come to be brought into the organisation in the first place? | 7, 6 | 7 ×3 | "I was recruited to POL by a former colleague, Belinda Crowe, … she contacted me on Linkedln…" | yes (quote shortened by clause selection) |
| u3-answer | Did you read Deloitte's Project Bramble reports when they were produced? | 238 | 235, 235, 240 | "…I would have been aware of Project Bramble in the background…" / "I did not give the reports … to anyone…" | no: ¶238 ranked 15th by dense score |
| u3-docref | Can you help us with POL00149601? What was your part in it? | 41 | 41 ×3 | "I believe that I drafted the instructions to Tom Weisselberg KC [POL00149601]." | yes |
| u3-trap-off | Where do you like to go on holiday? | – | refused ×3 (dense gate) | – | yes |
| u3-trap-on | What was your involvement in the prosecution of Seema Misra? | – | refused, 178, 178 | "I was aware that there had been an influx of referrals made to the CCRC in March 2015, but I had very little involvement…" | no (2 of 3 runs): plausible trap answered from a related paragraph |

### What it shows
- **Retrieval is the weak point, not grounding.** Every quote verified, and no answer fell back. All four wrong answers to answerable questions are retrieval misses: the expected unit wasn't in the top 5, and the answer step then quoted a neighbouring paragraph rather than refusing.
- **Paraphrase:** 2 of 3 paraphrased questions missed. That's a weaker showing than on the Ismay dev set (88%), but there are only 3 questions here.
- **Sub-unit retrieval:** one sub-unit question, and it missed. "103.9 The Auditor's opinion;" is three words, and even with its parent's lead-in prefixed ("Factors considered would include the following") a sibling, 103.5, ranked higher. A lettered sub-unit was cited correctly through document-ID routing (u1-docref, 44(i)).
- **Document references:** 3 of 3, every run. In u2-docref the ID in ¶47 is OCR-damaged ("POLO0088579"), so routing found only ¶77 and ¶74. Dense retrieval still brought ¶47 into the top 5, and the answer came from it.
- **Plausible traps:** 1 of 2 genuine ones leaked in some runs (Misra → CCRC ¶178, 2 of 3), the same pattern as on the Ismay dev set (Castleton → ¶65).

### OCR damage
- **Verification:** no quote failed it on these statements, including Roper's, which was mostly OCR.
- **OCR errors reach the spoken answer anyway,** because quotes are verbatim from the text layer: "Linkedln" (for LinkedIn, u3-para), "UKGl" (UKGI, u3-answer run 3), "POQL00091384" (POL00091384, u2-trap-on), and the "POLO" ID above. My OCR-junk check on quotes flagged none of these, because they look like ordinary words or IDs. So "0 quotes with OCR marks" in the summary understates it. "Lid" and "to►d" didn't come up in these answers.

### The dense gate (`DENSE_MIN_SCORE=0.56`, tuned on Ismay only)
- **Answerable questions:** it refused none. Their top dense scores were 0.629–0.857.
- **Off-topic traps:** it refused all three, at 0.499, 0.521 and 0.555. The last is 0.005 below the threshold.
- **Margin:** the lowest answerable score (0.629) and the highest off-topic score (0.555) are 0.07 apart. That's a thin margin on 9 questions. A threshold tuned on one witness may not carry over, and this check is too small to retune it.

### Reranker experiment (not enabled)

**Setup:** optional cross-encoder reranking (`RERANKER_ENABLED`, `RERANKER_MODEL`, `RERANK_CANDIDATES=20`), using fastembed's `TextCrossEncoder`. The top 20 units by dense score are reordered by the cross-encoder, and the top `RETRIEVAL_TOP_K` are kept. The refusal gate still uses the top dense score, and questions routed by document ID are not reranked. Each unit is scored on the same text the dense index embeds, so a sub-unit carries its section and its parent's lead-in. Two models were tried: `Xenova/ms-marco-MiniLM-L-6-v2` (0.08 GB) and `BAAI/bge-reranker-base` (1.04 GB). Everything ran on CPU, with 3 repeats per set and the other settings unchanged.

**Ismay dev set** (n=35 × 3; `outputs/eval/20261023-135958`, `-141303`, `-142604`):

| | hit@1 | hit@k | para accuracy | grounded | refusal precision | refusal recall | mean / p95 latency |
|---|---|---|---|---|---|---|---|
| off | 89% | 96% | 96% | 100% | 86% (86–88) | 90% (86–100) | 1135 / 2236 ms |
| MiniLM-L-6 | 75% | 93% | 93% | 100% | 75% | 86% | 1240 / 2187 ms |
| bge-reranker-base | 82% | 89% | 89% | 100% | 67% | 86% | 2154 / 2710 ms |

**Upload spot check** (n=18 × 3; `outputs/eval/upload_spotcheck/20261023-141302`, `-142603`, `-143545`):

| | hit@1 | hit@k | unit accuracy | grounded | refusal precision | refusal recall | mean / p95 latency |
|---|---|---|---|---|---|---|---|
| off | 50% | 67% | 67% | 100% | 100% | 78% | 1225 / 2701 ms |
| MiniLM-L-6 | 58% | 67% | 58% | 100% | 83% | 83% | 1265 / 2139 ms |
| bge-reranker-base | 50% | 67% | 67% | 100% | 83% | 83% | 2985 / 4899 ms |

**Where the expected unit ranks** (full ranking, retrieval only):

| | u1-para | u2-para | u2-sub | u3-answer | Ismay p06 |
|---|---|---|---|---|---|
| off (dense) | 8 | 119 | 67 | 15 | 21 |
| MiniLM-L-6 | 5 | 119 | 67 | 8 | 21 |
| bge-reranker-base | 11 | 119 | 67 | 5 | 21 |

Rerank latency was measured separately with `retrieve()` alone over the 53 spot-check and Ismay questions. Dense alone took 3.9 ms (mean) / 4.9 ms (p95). With MiniLM it was 213 / 506 ms, and with bge-reranker-base 1476 / 3117 ms.

**What happened:**
- **Out of reach:** three of the five misses are outside the 20 candidates (u2-para at 119, u2-sub at 67, p06 at 21), so no reranker can reach them. These are failures of dense recall, not of ordering.
- **MiniLM** lifted u1-para into the top 5, but the answer step still quoted ¶28, so unit accuracy didn't change. It lifted u3-answer from 15th to 8th, still outside the top 5. It pushed u3-para (¶7) out of the top 5. On Ismay it lost p03, which was then refused.
- **bge-reranker-base** fixed u3-answer (rank 5, answered from ¶238), but lost u3-para. On Ismay it lost p03 and p04, both then refused. That is the refusal precision drop: answerable questions refused because the right paragraph was no longer in the top 5.
- **The leaked Ismay trap** in the off row is t06, in 1 of 3 runs, in every configuration. Earlier runs refused it every time, so this is answer-step variation, not a reranker effect.

**Decision: off.** The rule was: turn it on only if it improves the spot check without lowering Ismay's paragraph accuracy, and adds under about 300 ms. Neither model passes. Both lower Ismay's paragraph accuracy (96% → 93% and 89%), neither improves spot-check unit accuracy (67% → 58% and 67%), and bge-reranker-base adds about 1.5 s per question. MiniLM's added latency (213 ms mean) would have fit, but its accuracy didn't. `RERANKER_ENABLED=false` stays the default. The code is kept so other models can be tried.

### Referrals instead of the first-sentence fallback

After the held-out upload run (below), a quote that fails verification is no longer replaced by the top paragraph's first sentence. The witness refers to a paragraph instead ("I'd refer you to paragraph {N} of my statement."; `REFERRAL` in `prompts/witness_answer.txt`). The result has `referral: true`, `verified: false`, `quote: null` and `para_no` set to the referred unit. `fallback` was removed.

**Metric changes:**
- **Grounded** now means no unverified words were spoken, so a referral counts as grounded.
- **Referrals** are reported as a count, plus a rate over non-refused answers.
- **Unit accuracy:** a referral to an expected unit counts as correct; a referral to any other unit does not.

Tables above this section were produced before the change. Their "fallbacks" column is the old fallback, and all of them show 0 except the held-out row.

**Ismay dev set rerun** (3 repeats, `outputs/eval/20261023-145042`): hit@1 89%, hit@k 96%, para accuracy 96%, grounded 100%, 0 referrals, refusal precision 87% (86–88), refusal recall 95% (86–100), mean / p95 1103 / 2140 ms. Retrieval, para accuracy and grounding are unchanged, as expected with 0 fallbacks before. As in the reranker runs, refusal recall varies because trap t06 was answered in 1 of 3 runs.

## Uploaded statements (held-out result)

**This is the upload result.** 12 new questions on 2 statements not used before (`eval/upload_heldout.jsonl`), written and checked before anything was run on them: expected units, plus a grep of each trap's key terms and synonyms. The question mix is the same as the spot check: per statement, 3 answerable (direct, paraphrased, and a third, a sub-unit question for the first statement), 1 document reference, 1 off-topic trap and 1 on-topic trap. Final settings (dense, `DENSE_MIN_SCORE=0.56`, reranker off), one run, via the upload endpoint. Results are in `outputs/eval/upload_spotcheck/20261023-144035/`.

- `witn10660100_0`: chairman of POL 2003–07. 111 units with sub-units to three levels (34.8.1); 1 page re-read by OCR; ready in 6.6 s.
- `witn09910100_1`: Programme Director of the Mediation Scheme. 109 units, no OCR; ready in 8.8 s.

| | hit@1 | hit@k | unit accuracy | grounded | refusal precision | refusal recall | fallbacks | mean / p95 latency |
|---|---|---|---|---|---|---|---|---|
| witn10660100_0 | 75% | 75% | 75% | 75% | 100% | 100% | 1 | 1114 / 2344 ms |
| witn09910100_1 | 75% | 100% | 100% | 100% | 100% | 100% | 0 | 1364 / 2606 ms |
| **Overall** | 75% | 88% | 88% | 88% | 100% | 100% | 1 | 1239 / 2344 ms |

With 8 answerable questions and 4 traps, one question moves the overall figures by 12 to 25 points.

| | Question | Expected | Answered from | Right? |
|---|---|---|---|---|
| u4-direct | While you were chairman, were you aware of any bugs, errors or defects in Horizon? | 48, 50, 54 | 48 ("I was not.") | yes |
| u4-para | When did anyone first suggest to you that the computer system itself might be to blame for money going missing? | 54 | 52 | no: ¶54 not in the top 5; ¶52 (the 2003 IT briefing) quoted instead |
| u4-sub | What did you do in the brewing business before you moved into airports? | 6.2 | 6.2 | yes (sub-unit ranked 1st) |
| u4-docref | Turning to POL00021482, why did you want to learn more about Horizon? | 51 | 51, fallback | right unit, not grounded: the model reworded the start of its quote ("I wanted to learn more about Horizon because…" for "I was interested in the capabilities of Horizon because…"), verification failed, and the fallback quoted ¶51's first sentence, which doesn't give the reason |
| u4-trap-off | Do you play golf? | – | refused (dense gate, 0.549) | yes |
| u4-trap-on | What did you make of Second Sight's interim report? | – | refused (answer step; gate 0.621 passed) | yes |
| u5-direct | Did you receive any training on the Horizon IT System during your time at POL? | 16 | 16 | yes |
| u5-para | Did anyone at Fujitsu tell you there was a back door … in an emergency? | 21, 22 | 21 | yes |
| u5-answer | How many people were in your team on the Scheme at its largest? | 44 | 44 ("my team comprised 7 people at its largest") | yes |
| u5-docref | You've been shown POL00108521. What was your position in that email chain? | 71 | 71 (routed; ¶28's document list ranked first, ¶71 second) | yes |
| u5-trap-off | Which football team do you support? | – | refused (dense gate, 0.522) | yes |
| u5-trap-on | What did you know about the Capture software…? | – | refused (answer step; gate 0.702 passed) | yes |

**The fallback was changed after this run, because of u4-docref.** When a quote failed verification, the witness used to quote the first sentence of the top paragraph. Here that sentence ("When I first became the chairman of POL, I did request to further my understanding of the Horizon system.") was verbatim but did not give the reason asked for. Now nothing is quoted: the witness says "I'd refer you to paragraph {N} of my statement.", naming the model's chosen unit if it was retrieved, else the top retrieved unit. The UI bands the whole unit and shows "Referred, not quoted". Replaying the recorded model reply for u4-docref (`tests/test_referral.py`) now gives "I'd refer you to paragraph 51 of my statement.". Under the new metrics that counts as grounded (no unverified words spoken) and as unit-correct (¶51 is expected), so this run would read grounded 100%, unit accuracy 88% (unchanged), 1 referral. The held-out set was not rerun: it has been used once, and these figures come from replaying the recorded reply, not from a new run.

**Compared with the spot check:**
- **Retrieval:** one miss (u4-para, paraphrased), the same kind as there.
- **Sub-units:** the sub-unit question was answered correctly this time.
- **Grounding:** the new failure is a fallback, the first on uploads. The quote shown is still verbatim, but it is not the answer.
- **Traps:** none leaked. Both on-topic traps passed the dense gate (0.621, 0.702), so they were refused by the answer step, not the gate.

## Held-out results (counsel questions)

`eval/ismay_heldout.jsonl`: 20 of counsel's questions from Mr Ismay's oral evidence on 11 May 2023 (transcript INQ00001063), labelled against the first written statement. Not yet run.

_To be filled in._
