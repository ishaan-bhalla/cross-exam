# Deploying to Render (free plan)

The app runs as one Docker web service on Render's free plan (512 MB of memory, a fraction of a
CPU, sleeps after 15 minutes without traffic). Everything is in the repo:

| File | What it is |
|---|---|
| `Dockerfile` | Python 3.11 slim with Tesseract. The embedding model and the example witness's index are built into the image; fixed phrases are synthesised at startup (that needs the key). Runs as a non-root user on `$PORT`. |
| `.dockerignore` | Keeps `venv/`, `outputs/`, `.env`, `data/` (except the example statement), docs, evals and tests out of the image. |
| `deploy/render.env` | Every setting except the key, tuned for the free plan. The image reads it (`CROSSEXAM_ENV_FILE`); real environment variables override it. |
| `render.yaml` | Blueprint: one free Docker web service, health check `/healthz`, `ASSEMBLYAI_API_KEY` as a secret to fill in. |

The only secret is `ASSEMBLYAI_API_KEY`, set in Render's dashboard. `tests/test_deploy.py` checks that `deploy/render.env` plus the key satisfies the settings and that the file contains no key.

## Measured before deploying

Built and run locally (OrbStack, Apple M3 Pro) with `--memory 512m` and only the key set, on 24 October 2026.

| | |
|---|---|
| Image size | 861 MB on disk, 238 MB compressed |
| Cold start (container start to `/healthz` answering) | 8.3–9.5 s locally; Render's free CPU is much slower, so expect longer |
| Memory: started, before the first question | 82 MB |
| Memory: after a question to the example witness (embedding model loaded) | 178 MB |
| Memory: the largest test statement uploading (168 pages, 294 units, 29 MB) | 389 MB peak |
| Memory: two visitors uploading at once (that statement and a mostly scanned one), OCR at 300 DPI | 455–464 MB peak |
| Memory: the same, OCR at 200 DPI (`OCR_DPI=200`, used on Render) | 381 MB peak |
| Memory: full click-through (front page, typed and spoken questions, an upload) | 280 MB peak |
| Killed for running out of memory | never |

Memory is the container's anonymous memory (`anon` in its cgroup's `memory.stat`, sampled every 0.1 s). The cgroup's total reaches 512 MB because it includes file cache, which the kernel drops under pressure.

What it took to fit:
- **`EMBEDDING_BATCH_SIZE=1`.** fastembed's default batch of 256 statement units, each padded to 512 tokens, peaked at 5.4 GB on the largest statement; one at a time stays under 400 MB and is also the fastest (5 s instead of 20 s). This is also the local default now.
- **`OCR_DPI=200`** on Render (300 locally): Tesseract needs 88 MB instead of 154 MB per page. The upload dev set scored the same at 200 as at 300 (10 of 14 right units, every answer grounded, the same 6 OCR-corrected quotes; [result](../eval/results/run_20261024_upload_dev_ocr_dpi_200.json)).
- **`MAX_OPEN_UPLOADS=2`** across all visitors, and **`UPLOAD_WORKERS=1`** (one statement parsed at a time). A third upload gets "The demo is busy, please try again in a few minutes." An upload frees its place when it is removed or after `UPLOAD_TTL_MINUTES` (30) without use. Refusing was chosen over evicting someone's idle upload, so nobody loses a witness mid-examination.
- **`UPLOAD_MAX_MB=30`**: the largest statement tested (29 MB) fits. A smaller limit would turn away many Inquiry statements (57 of the 135 tested are over 10 MB).
- `MALLOC_ARENA_MAX=2` in the image, so threads don't each hold their own memory arena.

## Dashboard steps

1. Push the repo to GitHub (Render deploys from a Git repository).
2. In the [Render dashboard](https://dashboard.render.com/), choose **New → Blueprint**, connect the repository, and select it. Render reads `render.yaml` and proposes one web service, `cross-examine`, on the **Free** plan.
3. When asked for `ASSEMBLYAI_API_KEY`, paste the key. It is stored as a secret and never shown in the repo or the image.
4. Choose **Apply**. The first build takes a few minutes (it installs the dependencies and downloads the embedding model).
5. When the service shows **Live**, open its URL (`https://cross-examine-….onrender.com`). Check:
   - `/healthz` answers `{"status": "ok"}`;
   - the front page loads, and the example witness answers a typed question;
   - `/api/limits` shows your own IP under `"ip"`. Then confirm the IP can't be spoofed: run `curl -s -H "X-Forwarded-For: 1.2.3.4" https://<your-url>/api/limits` and check `"ip"` is your real address, not 1.2.3.4. If it shows 1.2.3.4, set `CLIENT_IP_POSITION=last` (step 7) and check again.
6. **Keep it awake** (next section).
7. To change a setting, open the service's **Environment** tab and add it as an environment variable (for example `LIMIT_QUESTIONS_PER_DAY=50`). Environment variables override `deploy/render.env`. Saving restarts the service.

`autoDeploy` is off in `render.yaml`: a push does not redeploy. To deploy a new commit, use **Manual Deploy → Deploy latest commit**.

## Keep the service awake

On the free plan, the service sleeps after 15 minutes without traffic, and waking starts a new container. That matters twice over: the first visitor waits for the cold start, and **the daily budget counters reset**, because they live in the container (`BUDGET_STATE_FILE=data/budget_state.json`, which survives a process restart but not a new container).

So ping `/healthz` every 10 minutes with an uptime monitor (for example UptimeRobot or cron-job.org, both free): an HTTP(S) monitor on `https://<your-url>/healthz` at a 10-minute interval. Then the container only restarts when you redeploy, the counters survive the day, and visitors don't hit cold starts.

`/healthz` answers GET and HEAD (UptimeRobot's free plan checks with HEAD) with 200. Its requests set no cookie and count towards no limit and no budget. They do expire idle uploads, so memory is freed even when nobody else is visiting.

**Rule: after every redeploy, check [AssemblyAI's usage dashboard](https://www.assemblyai.com/dashboard) that day.** A redeploy starts a new container, so that day's counters start again from zero.

The free plan includes 750 instance hours a month, which covers one service kept awake all month (about 744 hours).

## Protecting the credits

The account has $150 of AssemblyAI credit, so the public demo is held to about $5 a day.

### The global daily budget (all visitors, per UTC day)

| Setting | Render value | What happens when it's reached |
|---|---|---|
| `BUDGET_LLM_CALLS_PER_DAY` | 1000 | Demo-limit mode |
| `BUDGET_TTS_CHARS_PER_DAY` | 25000 | Demo-limit mode |
| `BUDGET_STT_SESSIONS_PER_DAY` | 100 | Spoken questions stop; typing still works |
| `BUDGET_STT_SECONDS_PER_DAY` | 3600 | Spoken questions stop; typing still works |
| `BUDGET_COST_USD_PER_DAY` | 5 | Demo-limit mode, once all `COST_*` rates are set |
| `DEMO_PAUSED` | false | `true` switches to demo-limit mode at once |

**Demo-limit mode** shows "The demo has reached its limit for today. Please try again tomorrow. The statement can still be read." with a link to the video if `DEMO_VIDEO_URL` is set. The front page and the example witness's statement still work; asking, uploading, replaying and the microphone are switched off.

Usage is recorded where it is spent: every LLM call (including profile drafts for uploads), every character sent to TTS (including the phrases synthesised at startup and for a new voice), and every STT session issued plus the audio seconds each spoken question reports.

### How the limits were chosen

The `COST_*` rates are empty because AssemblyAI's rates for these products weren't known when this was written, so the estimated cost is "not measured" and `BUDGET_COST_USD_PER_DAY` does not apply yet. The unit budgets above were set from these **assumed, deliberately high** rates:

| | Assumed rate | Budget | Worst case per day |
|---|---|---|---|
| LLM (qwen3.5-4b, about 815 tokens in and 52 out per answer, measured) | $0.0015 per call (about $1 per million tokens in, $4 per million out, plus margin) | 1000 calls | $1.50 |
| TTS | $100 per million characters | 25,000 characters | $2.50 |
| STT | $0.60 per hour | 100 sessions × 60 s (`STT_MAX_SESSION_SECONDS`) | $1.00 |
| **Total** | | | **$5.00** |

In practice an answer uses about 1 LLM call and 129 TTS characters ([measured](../eval/results/run_20261024_ismay_live.json)), so 25,000 characters is roughly 190 spoken answers a day; startup costs 316 characters each time the container starts. A spoken question streams about 5 s of audio.

**After the first day,** look up the real usage on AssemblyAI's dashboard and the real rates, then either set the `COST_*` rates (so `BUDGET_COST_USD_PER_DAY=5` applies to the estimated cost) or adjust the unit budgets.

### Per visitor (cookie) and per IP

| Setting | Render value | Message the visitor sees |
|---|---|---|
| `LIMIT_QUESTIONS_PER_MINUTE` | 5 | "You have asked 5 questions in the last minute, the most this demo allows. Please wait a moment, then ask again." |
| `LIMIT_QUESTIONS_PER_DAY` | 30 | "You have asked 30 questions today, the most this demo allows. Please come back tomorrow." |
| `LIMIT_SPOKEN_SECONDS_PER_DAY` | 300 | "You have used this demo's 5 minutes of spoken questions for today. You can still type your questions." |
| `LIMIT_STT_SESSIONS_PER_DAY` | 20 | "You have started the microphone as often as this demo allows today. You can still type your questions." |
| `LIMIT_UPLOADS_PER_DAY` | 2 | "You have uploaded 2 statements today, the most this demo allows. Please come back tomorrow, or try the example witness." |
| `LIMIT_IP_FACTOR` | 3 | The same limits apply per IP at 3 times these values ("Your network has …"), so clearing cookies doesn't reset them, while people sharing a network (an office, a conference) still get several visitors' worth. |
| `UPLOADS_PER_CLIENT` | 2 | Open at once per visitor |
| `MAX_OPEN_UPLOADS` | 2 | Open at once across everyone: "The demo is busy, please try again in a few minutes." |

The client IP comes from `CLIENT_IP_HEADER=x-forwarded-for`, first entry (`CLIENT_IP_POSITION`); check it can't be spoofed after deploying (dashboard step 5).

A visitor can go over their spoken quota by at most one session (60 s): the page keeps one microphone token ready so the mic starts at once, and a token issued before the quota ran out can't be revoked. The next token is refused, and the global session budget counts every token issued.

A microphone token is fetched when the examination page opens and replaced every 9.5 minutes while the tab is visible (tokens last `STT_TOKEN_EXPIRES_SECONDS=600`), so each open page uses a few of the 20 daily sessions. A token's session can run at most 60 s.

### Raising or lowering the limits

Add the setting as an environment variable in the service's **Environment** tab; it overrides `deploy/render.env` and takes effect when the service restarts (saving restarts it). For a permanent change, edit `deploy/render.env` and redeploy. Examples:

| To | Set |
|---|---|
| Halve the daily spend | `BUDGET_LLM_CALLS_PER_DAY=500`, `BUDGET_TTS_CHARS_PER_DAY=12500`, `BUDGET_STT_SESSIONS_PER_DAY=50` |
| Stop all spending now (for example, if usage looks wrong) | `DEMO_PAUSED=true` |
| Let each visitor ask more | `LIMIT_QUESTIONS_PER_DAY=60` |
| Turn a limit off | set it to `0` |
| Use the real rates for the dollar cap | `COST_LLM_INPUT_PER_MILLION_TOKENS`, `COST_LLM_OUTPUT_PER_MILLION_TOKENS`, `COST_TTS_PER_MILLION_CHARS`, `COST_STT_PER_HOUR` (all four), then `BUDGET_COST_USD_PER_DAY` |

Today's usage against every limit, and your own, is at `/api/limits`.

## When the LLM gateway is busy

If the gateway answers 429 (rate limited), the witness shows a spinner and "The witness is gathering their thoughts: the service is busy." The server keeps retrying with backoff (`LLM_RETRY_BACKOFF_SECONDS`, up to `LLM_MAX_RETRIES=8`) and gives up after `LLM_MAX_WAIT_SECONDS=45` of waiting with "The service is still busy after 45 seconds of waiting. Please ask again in a minute."

## Cold starts

If the first API request takes over 1.5 s, the page says "Starting up the server. On the free plan this can take up to a minute after a quiet spell; the page will be ready shortly." With the uptime pinger running, visitors shouldn't see it. When the service is fully asleep, Render holds the first page load itself until the container is up.
