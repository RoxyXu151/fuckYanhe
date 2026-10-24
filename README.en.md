# fuckYanhe

English | [简体中文](README.md)

Find attendance checks, homework, exams, grading rules and schedule changes in lecture recordings or local media you are authorized to access. Every candidate notice includes original evidence and a replayable timestamp.

This is a local web workspace, not an official university product. Model results require human review. A completed analysis or an empty result does not guarantee that nothing was missed.

## One report per course

Each official course has one stable report containing all its recordings. Retries reuse the latest completed result per recording and preserve existing complete content. Home, reports, and the study picker group entries by course. Export the combined report as Markdown; original evidence, subtitles, reviews, and caches remain available. Different official course IDs stay separate even when their titles match.

## v0.9.0 · shadcn/ui

React and official shadcn/ui components power the dashboard, with a white, green, black and red palette. Existing course, report, study and model workflows, bilingual UI and themes are retained. See [frontend development](ui/README.md).

## v0.8.1 · Bento Grid

A Bento dashboard and unified course, report and study surfaces. Existing workflows, bilingual UI and light/dark themes are retained, with responsive narrow-screen layouts.

## v0.8.0

- Separate download and analysis queues, parallel downloads, successful-chunk reuse and restart recovery.
- Windows Credential Manager session storage with official browser persistence and automatic validation; no password storage.
- Adaptive Qwen alignment repair, proxy-free loopback model requests and bounded failed-chunk recovery.
- Course-aware return navigation across reports, study views and page reloads.
- Optional Windows sign-in startup and connection watchdog with configurable server, ports and data directory.
- Renamed to **fuckYanhe**. The `yanhe_sentinel` module and `yanhe-sentinel` command remain compatible.

## Features

- **Overview and readiness:** onboarding steps, media tools, storage status and read-only model checks. Optionally remember model endpoints and names without storing keys.

- **Course library:** automatically retain browsed courses and processed lectures; view saved reports offline, search, add notes, star, archive and restore courses.
- **Batch analysis:** select multiple courses, refresh their recordings and queue unfinished lectures. Completed and already queued lectures are skipped by default. Access failures are reported per course.
- **Task queue:** view stages and progress, stop tasks or resume interrupted processing while keeping successful cached results.
- **Study workspace:** search existing transcripts, seek by timestamp and change playback speed. Generate topical chapters, summaries and study points with original evidence using the configured analysis model. Supports Chinese/English notes, cached resume and Markdown export.
- **Reports:** filter by course, status or keyword; replay original recordings and clips, mark manual reviews and export a ZIP.
- **Self-hosted models and online APIs:** choose each independently for transcription and notice analysis. Mix self-hosted transcription with online analysis, or the reverse. Compatible APIs and Gemini, Groq and DeepSeek presets are supported.
- **Chinese and English UI:** switch without reloading. Course titles, notes, subtitles and evidence stay in their original language; changing UI language makes no model calls.
- **Appearance:** light, dark or system theme. Only language and theme preferences are saved in browser storage; controls still work when storage is blocked.
- **Explicit coverage:** separate audio transcription progress from subtitle analysis coverage, with failed chunks, audio offsets, timestamp provenance and quality notes.

Course totals reflect locally saved or last-synced lectures. Batch runs prefer screen recordings, use original video audio and process up to 100 lectures per request. Open a course to choose separate microphone audio or frame analysis.

## Quick start

Python 3.11+ is required. On Windows, double-click `start_webui.cmd`, or use:

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[web,media,dev]"
python -m yanhe_sentinel webui
```

Open [the local workspace](http://127.0.0.1:8765). Sign in through the official school window to browse recordings, then configure services in **Model settings**. You can upload your own media or subtitles and view saved reports without a school login.

The workspace binds only to loopback. It is designed for one local user; do not expose its port directly to the public internet.

## Configure models and APIs

In **Model settings**, choose **Online API** or **Self-hosted open-source model** separately for transcription and notice analysis. Supply the endpoint and exact model name supported by your service.

| Service | API contract | Authentication |
| --- | --- | --- |
| Compatible transcription | `/audio/transcriptions`, returning timestamped `segments` | Required for online APIs; optional for self-hosted services without authentication |
| Compatible notice analysis | `/chat/completions` with structured JSON output | Same rule |
| Gemini / Groq transcription | Provider-specific preset | API key required |
| DeepSeek analysis | Provider-specific preset | API key required |
| Optional vision | Image-capable compatible chat endpoint | Uses the analysis service by default; separate configuration is available |

Self-hosted services with authentication still need their own key. Remote endpoints require HTTPS; use an SSH tunnel to loopback for remote HTTP servers. API pricing, quotas and available models depend on your provider and account. Failed requests never trigger an automatic switch to another provider or paid plan.

Opt in to remember non-secret endpoints and model names locally; uncheck and save to remove these preferences. Keys remain in the current server session only. A blank password field preserves a key only for the **same service**. Changing an endpoint, provider or deployment type clears its previous key before saving, so credentials are not forwarded to another service. Browser preferences never contain model keys.

Changing settings does not rerun completed tasks. Explicitly resume or reanalyze a report to invoke models again.

## Self-hosted deployment

The supplied service scripts support Qwen3-ASR-1.7B with Qwen3-ForcedAligner-0.6B and a compatible Qwen text model. Reuse an existing compatible text service if available. Model weights, runtime dependencies, caches and GPU memory are additional to this source repository.

See [remote model deployment](docs/REMOTE_MODELS.md) and the configurable `scripts/start_remote_workbench.ps1`. Self-hosted inference uses your own server resources and avoids third-party per-request API charges.

## Workflow

Select stopped or failed tasks in the queue and resume them together, reusing successful caches. Completed or active tasks are skipped; school tasks that require reconnection receive a specific prompt.

1. Connect in **Add courses** and browse lectures. Course metadata is retained in the library.
2. Select lectures within a course, or select courses in the library and choose **Analyze selected courses**.
3. Check the queue. Incomplete model configuration is rejected before batch tasks are queued.
4. Filter reports, replay evidence and mark your own reviews.

Archiving hides a course without deleting recordings, subtitles or reports. After a school session expires, reconnect to download new recordings; existing reports remain available offline.

## Study workspace

Personal notes, original-source bookmarks, playback position and speed are saved locally. Unsaved drafts survive page switches; URLs restore the current lecture after reload. Choose **Resume last playback** to continue. Changed transcripts mark old positions and citations as stale. Regeneration retains the previous complete notes, which can be restored without another model call.

Choose a lecture in **Study**, or open it from a notice report or saved course. Transcript search and timestamp seeking make no model calls. **Generate chapters and notes** invokes your current analysis model, whether self-hosted or online. Provider charges may apply to online APIs.

Long transcripts are processed in windows of up to 160 segments, approximately 15 minutes or 10,000 characters. Structured note generation uses standard mode. Invalid boundaries trigger a source-based single-chapter retry; results that still fail validation are not accepted. The model divides each window into topics; similar topics may appear on either side of a window boundary. The application validates continuous chapter coverage and in-chapter citations, then derives times and copies evidence directly from original segments. This validates references, not the semantic correctness of model summaries. Missing speech and misrecognized formulas still require review. Generated notes summarize available transcripts; original slide files must be obtained separately.

Successful windows are cached privately per lecture. Stopping takes effect after the current request finishes. Resume reuses successful windows; regeneration calls the model again. A changed transcript marks old notes as outdated. Plain TXT files support notes and segment references but have no timestamp seeking. Saved notes remain readable offline and can be exported as Markdown.

Before upgrading an active workspace, stop its tasks to preserve cached results. On restart the queue resumes and Windows validates the saved school session; reconnect only if the official platform reports genuine expiry.

## CLI and tests

```bash
python -m yanhe_sentinel demo --output runs/demo
python -m yanhe_sentinel analyze --subtitle examples/demo.srt --provider openai-compatible --output runs/report
pytest -q
```

See [.env.example](.env.example) for explicit environment configuration. The project does not automatically load `.env` files. For the web UI, set `YANHE_ASR_DEPLOYMENT=self-hosted` and/or `YANHE_EVENT_DEPLOYMENT=self-hosted` when using your own services. CLI users can explicitly allow keyless compatible services with `YANHE_ASR_ALLOW_NO_KEY=1` and/or `YANHE_ALLOW_NO_KEY=1`.

Tests cover media decoding, access checks, cache recovery, evidence validation, course persistence, batches, mixed service authentication and UI preferences. Cloud/GPU responses in automated tests are controlled fixtures. Passing tests is not a measure of classroom detection accuracy. Node.js enables the optional frontend preference regression test.

## Privacy and publication

Real recordings, subtitles, student names, course notes, reports, login sessions, keys and internal server configuration are not part of the public source. Runtime data, input media, weights and credential files are ignored. Before publishing, run:

```bash
python scripts/check_publication.py
```

See [privacy boundaries](docs/PRIVACY.md). Respect school policies and the privacy of instructors and classmates.

See the [product review](docs/PRODUCT_REVIEW.md) for the assessment, implemented improvements and remaining boundaries.

## License

Source code is licensed under [MIT](LICENSE). Third-party models and dependencies retain their own licenses. Model weights are not included.
