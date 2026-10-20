# Streaming TTS API: feedback from building a live cross-examination app

Context: a web app that answers a barrister's questions in a witness's voice, quoting the witness statement, with the quoted words highlighted as they are spoken. It uses the streaming TTS WebSocket (`wss://streaming-tts.assemblyai.com/v1/ws`, voice `charles`, 24 kHz PCM16). All numbers below were measured on 19–20 October 2026 from a client in the UK; counts are small and should be read as indicative.

## 1. The API key is accepted as a `?token=` URL parameter

**What happened.** Connecting with the account API key in the query string (`?voice=charles&token=<key>`) and no `Authorization` header was accepted: the server replied with `Begin`.

**Why it matters.** URLs are written to server and proxy access logs, browser history and monitoring tools. Accepting a long-lived account key in the URL makes it easy to leak, and invites putting the key in browser code, where anyone can read it.

**Suggestion.** Accept the account key only in the `Authorization` header. If browsers need to connect directly, accept only short-lived, single-use tokens in the URL.

## 2. Undocumented limit of about 5 session opens per 30 s

**What happened.** Opening and properly closing sessions one after another (send `Terminate`, receive `Termination`, close the socket; each open-to-close cycle took 1.4–1.5 s):

- sessions 1–5 opened normally;
- every open from the 6th failed with error `3009: Too many concurrent sessions for this account; try again later`, although no other session was open;
- a new session opened successfully about 30 s after the first one.

**Why it matters.** Any design that opens one session per utterance hits this quickly: our app's startup pre-synthesis of 7 short phrases (one session each) failed on the 6th, and back-to-back questions would too. The message says *concurrent* sessions, which sent us looking for leaked connections; the limit behaves like a rate limit on opens, or like closed sessions still counting for about 30 s.

**Suggestion.** Document the limit. Make the error say which limit was hit (rate of opens versus concurrency), and include a retry-after value. If closed sessions are being held for ~30 s, release them on `Termination`.

**What we do now.** One session per examination, reused for every answer (see "What worked well"), and all fixed phrases synthesised in a single session, one flush each. Six questions asked back to back then used one session open and produced no 3009 errors.

## 3. Word timings arrive only after the whole flush, and sometimes not at all

**What happened.**

- `WordBoundaries` for a flush is sent only after all of that flush's audio and its `FlushDone`, never during the audio.
- When the audio starts playing as soon as it arrives, the timings for a one-sentence quote (9–24 words, 4–11 s of audio) arrived 156–935 ms *after* the quote had started playing, in 3 of 4 measured answers.
- For a long flush (89 words, about 32 s of audio), `FlushDone` and all the audio arrived but `WordBoundaries` never did. This happened in 3 of 5 direct attempts and in all 3 attempts through the app. The other 2 direct attempts received timings normally.

**Why it matters.** Highlighting the words in sync with the voice needs the timings before each word is spoken. As it is, the client must either delay the audio (our app holds the quote for up to 800 ms, and once hit that cap with the timings arriving 211 ms after it) or start the highlight late. When the timings never arrive, a client that waits for them waits forever. Ours did until we added a timeout.

**Missing-timings rate, before and after splitting.** Same 89-word quote, asked 5 times through the app on one reused session:

| How the quote was sent | Runs with no `WordBoundaries` |
|---|---|
| One flush (89 words, ~32 s of audio) | 4 of 5 |
| Six flushes, split at clause punctuation (6, 17, 12, 8, 10 and 36 words) | 0 of 5 (0 of 30 flushes) |

Splitting also lets the first chunk's timings arrive while later chunks are still being generated. The cost: the text has to be split at punctuation, each chunk is synthesised without its neighbours as context, and the natural pause at a clause break must be rebuilt by the client. With the split, a comma pause that was 410 ms in the single-flush audio became the 150 ms our client allows between chunks.

**Suggestion.** Stream word boundaries incrementally with the audio (per chunk or per sentence), so that each word's timing arrives before or with the audio that contains it. Always send `WordBoundaries` for every flush (empty, or with an error, if it cannot be computed), before or together with `FlushDone`. Until then, document a recommended maximum flush length.

## 4. Word end times look truncated; confidence is not obviously lower on short words

**What happened.** Across 99 words from 9 utterances:

- Durations (end − start) ranged from 16 ms to 889 ms, median 116 ms. 17 words were 33 ms or shorter (for example "I" at 16 ms). The values cluster at multiples of about 16.7 ms (16, 17, 32, 33 ms).
- In 61 of 90 cases there is a gap of 50 ms or more between a word's end and the next word's start (median gap 65 ms), even within continuous speech. This suggests that end times stop short of the end of the word's audio.
- Confidence: median 0.96 for 1–2-letter words, 0.78 for 3–4-letter words and 0.75 for words of 5+ letters. Low values occurred on assorted words: "I…" 0.01, "don't" 0.26, "and" 0.36, "in" 0.38, "this:" 0.39. So we did **not** find confidence to be systematically lower on short words; it is low on a scattering of words of all lengths.

**Why it matters.** End times that stop short can't be used to decide how long to keep a word highlighted. We use each word's start until the next word's start and ignore end times. It's not clear what confidence means for synthesis, where the text is known in advance, so clients can't tell whether a low value matters.

**Suggestion.** Report end times that extend to the end of the word's audio, or document that they are approximate. Document what `confidence` measures for TTS, or omit it.

## 5. Audio arrives in bursts: 160 ms, then a gap of about 300 ms

**What happened.** Audio comes in 80 ms chunks. At the start of every flush, 2 chunks (160 ms of audio) arrive together, then nothing for 273–320 ms (5 flushes measured), then a larger burst. To play without a gap, the client needed to buffer 113–160 ms before starting (measured per utterance: 113, 120, 121, 130 and 160 ms). With a 150 ms buffer, playback ran dry in 1 of 5 simulated utterances, and once in the browser on a refusal.

**Why it matters.** Every client has to discover this pattern and add a start-up buffer, which adds directly to the time to first audio.

**Suggestion.** Send a larger first burst, or pace chunks evenly. Otherwise document the recommended client buffer.

## 6. Every flush starts with several hundred ms of silence

**What happened.** The first word of a flush starts 357–1015 ms into its audio (7 fixed phrases measured). The silence is present on every flush, including the second and later flushes of a session.

**Why it matters.** It adds directly to the time until the first word is heard, and puts long pauses between consecutive flushes. We trim it on the client: with trimming, phrases start 27–98 ms before their first word.

**Suggestion.** Remove the leading silence, or make it configurable per flush.

## 7. No public streaming TTS documentation, but the error messages are very clear

**What happened.** We found no public documentation for the streaming TTS WebSocket. We worked out the protocol by probing: the `Generate`, `Flush`, `Terminate` and `KeepAlive` messages; the `Begin`, `Audio`, `FlushDone`, `WordBoundaries`, `Termination` and `Error` replies; the voice as a query parameter. The error messages helped a great deal, for example: `3006: Unsupported preset voice 'not_a_voice'. Available on this connection: ['alba', 'anna', 'charles', ...]`.

**Why it matters.** Several behaviours we rely on had to be found by experiment, and could change without notice:
- flush IDs (we saw them start at 0, but don't assume it)
- word times being absolute within the session rather than relative to the flush
- `KeepAlive` being accepted silently
- the session limit

**Suggestion.** Publish protocol documentation that covers:
- message types and fields
- flush ID semantics
- the time base for word timings
- KeepAlive and `expires_at`
- limits

Keep the error messages as they are.

## What worked well

- Error messages (above).
- A session can be reused for many utterances. `KeepAlive` is accepted silently, and a session stayed usable after 40 s idle with a `KeepAlive` every 10 s.
- Word times are consistent within a session: each flush's `audio_start_ms` equals the sum of the earlier flushes' `FlushDone` durations (for example 1520 ms after a 1520 ms flush).
- Time from sending `Generate` to the first audio chunk was consistently 282–335 ms.
