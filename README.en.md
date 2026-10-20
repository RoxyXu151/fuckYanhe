# Yanhe Classroom Sentinel

English | [简体中文](README.md)

Find attendance checks, homework, exams, grading rules and schedule changes in lecture recordings or local media you are authorized to access. Every candidate notice includes original evidence and a replayable timestamp.

This is a local web workspace, not an official university product. Model results require human review. A completed analysis or an empty result does not guarantee that nothing was missed.

## Features

- **Course library:** automatically retain browsed courses and processed lectures; view saved reports offline, search, add notes, star, archive and restore courses.
- **Batch analysis:** select multiple courses, refresh their recordings and queue unfinished lectures. Completed and already queued lectures are skipped by default. Access failures are reported per course.
- **Task queue:** view stages and progress, stop tasks or resume interrupted processing while keeping successful cached results.
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

Keys remain in the current server session only. A blank password field preserves a key only for the **same service**. Changing an endpoint, provider or deployment type clears its previous key before saving, so credentials are not forwarded to another service. Browser preferences never contain model keys.

Changing settings does not rerun completed tasks. Explicitly resume or reanalyze a report to invoke models again.

## Self-hosted deployment

The supplied service scripts support Qwen3-ASR-1.7B with Qwen3-ForcedAligner-0.6B and a compatible Qwen text model. Reuse an existing compatible text service if available. Model weights, runtime dependencies, caches and GPU memory are additional to this source repository.

See [remote model deployment](docs/REMOTE_MODELS.md) and the configurable `scripts/start_remote_workbench.ps1`. Self-hosted inference uses your own server resources and avoids third-party per-request API charges.

## Workflow

1. Connect in **Add courses** and browse lectures. Course metadata is retained in the library.
2. Select lectures within a course, or select courses in the library and choose **Analyze selected courses**.
3. Check the queue. Incomplete model configuration is rejected before batch tasks are queued.
4. Filter reports, replay evidence and mark your own reviews.

Archiving hides a course without deleting recordings, subtitles or reports. After a school session expires, reconnect to download new recordings; existing reports remain available offline.

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

## License

Source code is licensed under [MIT](LICENSE). Third-party models and dependencies retain their own licenses. Model weights are not included.
