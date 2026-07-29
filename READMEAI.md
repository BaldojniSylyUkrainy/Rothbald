# READMEAI — project memory for Codex

Last updated: 2026-07-29

This file is the authoritative technical memory for the local video-search application. Read it completely before diagnosing or modifying the app. After every material change, update the affected sections and append a dated changelog entry.

Do not store secrets, tokens, or full user media paths here. User media and generated data belong in `data/` or on the selected external drive and must not be committed.

## Product goal

A private, local Apple Silicon and Windows application for searching long-form videos used in editing projects. The user selects a project folder on any available internal or external storage. The app recursively discovers media, transcribes it locally, and returns clickable file-relative timestamps.

The app must support two distinct search modes across every indexed video in the selected project:

- exact text search;
- multilingual semantic search for the same idea expressed with different words.

Selecting a video only controls the player. It must never narrow the search scope.

## Target environments

- Apple Silicon uses `mlx-whisper` and is the primary optimized target.
- Packaged macOS builds require macOS 14.0 or newer. The pinned PyTorch Apple Silicon wheel has a macOS 14 deployment target; do not advertise Ventura compatibility unless the dependency set changes and a real Ventura build is verified.
- Windows x64 keeps one Whisper Large V3 Turbo model family with backend-specific local artifacts. NVIDIA uses `faster-whisper`/CTranslate2 through CUDA; AMD and non-NVIDIA Vulkan devices use the bundled `whisper.cpp` Vulkan backend; CPU remains the final fallback. Intel Vulkan is exposed as experimental until representative Intel hardware is verified.
- Source launches require `ffmpeg` and `ffprobe`; packaged builds include the Python runtime, PySide6/QtWebEngine, both media binaries, and all backend dependencies.
- Python virtual environment: `.venv/`.
- Local URL: `http://127.0.0.1:8765`.
- The local URL is an internal transport only. `rothbald.py` presents it in a dedicated native PySide6 window through QtWebEngine. Packaged users do not interact with an external browser and never install a web runtime separately.
- `start.command` / `setup.command` and `start.bat` / `setup.ps1` are developer-only source helpers. End users install a signed/notarized DMG on Apple Silicon or the Windows setup executable and need no Python, PowerShell, ffmpeg, terminal, or browser.
- `start.command` runs the server under `/usr/bin/caffeinate -i`: idle system sleep is prevented for the lifetime of the app, while display sleep and screen locking remain allowed. Active transcription keeps the source disk in use.
- Packaged builds store generated data under Application Support on macOS and LocalAppData on Windows. Source launches retain the repository-local `data/` behavior.
- No VPS or automatic internet video downloading is in scope for this version.

## Main files

- `server.py` — HTTP server, SQLite storage, folder scanning, queue, transcription orchestration, semantic indexing, search, and media range serving.
- `transcribe_video.py` — isolated MLX, faster-whisper, or whisper.cpp transcription subprocess.
- `prepare_models.py` / `prepare_semantic.py` — download and verify the Whisper and semantic models during setup.
- `model_manager.py` — platform model manifest, remote revision check, resilient local verification, exact byte progress, and background downloads for the startup gate.
- `hardware_check.py` — first-run architecture, OS, RAM, disk, CPU, exact CUDA/Vulkan-device detection, persisted acknowledgement, and compute-device preference.
- `process_utils.py` — shared Windows child-process flags that suppress console windows while preserving managed process groups.
- `tools/vulkan_probe/` / `scripts/build_whisper_cpp_windows.ps1` — exact Windows Vulkan-device enumeration and reproducible pinned whisper.cpp Vulkan build.
- `rothbald.py` / `Rothbald.spec` — PySide6/QtWebEngine native desktop-window launcher and self-contained PyInstaller definition.
- `assets/app-icon.png`, `.icns`, and `.ico` — rounded `Ro` application icon derived from the same Bradley Hand wordmark used by the UI.
- `static/index.html` — application markup.
- `static/app.js` — client state, queue progress, search tabs, video selection, and project actions.
- `static/style.css` — responsive dark interface.
- `requirements.txt` / `requirements-build.txt` — direct runtime and build inputs. `requirements-macos.lock`, `requirements-windows.lock`, and the matching `requirements-build-*.lock` files are the reproducible Python 3.12 environments used by setup and CI.
- `VERSION` — the single four-component public application version.
- `RELEASE_NOTES.md` / `release_notes.py` — the single version-bound release text and its fail-closed validation contract. The same Markdown is used by GitHub Release and the in-app updater.
- `app_info.py` and `scripts/prepare_build.py` — runtime build metadata and deterministic platform version resources.
- `update_manifest.py` / `updater.py` — Ed25519 manifest verification, platform asset selection, streaming download, SHA-256 verification, and native installer handoff.
- `scripts/generate_updater_key.py` / `scripts/generate_release_manifest.py` — one-time updater key generation and signed release-manifest assembly.
- `.github/workflows/release.yml` — manual gated signed/notarized draft-release pipeline.
- `tests/test_server.py` — standard-library regression tests for migrations, queue claiming, matching, ranges, and chunk checkpoints.
- `README.md` — user-facing installation and usage; `docs/DEVELOPMENT_UK.md` keeps source-only setup separate.

## Runtime architecture

### Media discovery

The user chooses a folder through the platform-native macOS or Windows picker. `scan_project()` recursively finds supported media without copying it. Supported extensions currently include MP4, MOV, MKV, WebM, M4V, AVI, MP3, M4A, WAV, FLAC, and OGG.

Video identity is stable inside a project and based on `project_id + relative_path`, not the absolute SSD path. The historical global `UNIQUE(source_path)` is rebuilt away during migration, so the same physical media may safely appear in two different projects. Size and mtime select files that need verification; a quick size + first/last-megabyte fingerprint prevents an mtime-only change from forcing transcription and protects `Locate` from attaching an unrelated folder. `ffprobe` has a 45-second timeout and always runs outside SQLite write transactions. Rescanning queues only genuinely new or changed media.

Scanning stages the complete filesystem result before changing SQLite, then atomically marks missing media unavailable and upserts every rediscovered relative path in one transaction. A traversal, stat, or fingerprint failure therefore preserves the last known-good availability state. It must never delete a video row, transcript, or semantic index merely because a file or drive is absent. Duration probing is not part of the scan transaction or application startup: a dedicated single background queue runs `ffprobe`, records the attempt time, and retries failed or zero results no more than once per day. This is required for removable storage and `Locate`.

### Transcription

- Apple engine/model: `mlx-whisper` with `mlx-community/whisper-large-v3-turbo`.
- Windows NVIDIA/CPU engine/model artifact: `faster-whisper` with `mobiuslabsgmbh/faster-whisper-large-v3-turbo`.
- Windows AMD/Intel/other non-NVIDIA GPU engine/model artifact: bundled `whisper.cpp` Vulkan with `ggerganov/whisper.cpp` `ggml-large-v3-turbo.bin`.
- There is no UI model selector. Platform selection is automatic so saved transcription signatures never ambiguously mix engines.
- Every project stores a `language_mode`. `standard` is the default and maps internally to the existing Russian Whisper hint without exposing a language code in the UI; `auto` passes no hint to MLX/faster-whisper and `auto` to whisper.cpp. The project toolbar labels these modes `Стандартна` and `Автовизначення`. The setting applies to new or explicitly repeated transcriptions; it never silently replaces existing text and cannot change while a transcription queue is queued, processing, or paused. Include the mode in every checkpoint signature.
- Whisper runs in a subprocess so its MLX/Metal memory is released after each file.
- Media is split into 30-minute core ranges with two-second overlap. `ffmpeg` extracts one temporary mono WAV at a time; completed parts are checkpointed in `transcription_parts`. Resume reuses all completed parts and repeats only the interrupted part. Midpoint ownership removes overlap duplicates when timestamps are reassembled.
- Only Whisper may use Metal/GPU resources.
- On Windows, Auto resolves in strict order: CTranslate2-visible NVIDIA CUDA, first non-NVIDIA Vulkan GPU, CPU. NVIDIA is deliberately not offered through Vulkan. A failed Vulkan invocation is retried once through whisper.cpp CPU mode for the same 30-minute part.
- Progress is parsed from Whisper/tqdm stderr and saved in SQLite.
- `ffmpeg` and Whisper run in their own process groups. Pause/Abort first terminate the complete group and force-kill it after five seconds if necessary, preventing orphan decoders.
- Normal application exit and updater-triggered exit use the same managed shutdown path: unfinished queues are persisted as paused with a new generation, active process groups are terminated, queued work is drained, and worker threads receive a bounded join before the desktop process exits.
- If a managed subprocess produces no stderr activity for 20 minutes, the watchdog terminates it and marks the video as errored instead of hanging forever.
- UI marks a processing item as potentially stalled after 10 minutes without a progress update.
- On application restart, every project containing `processing` or `queued` transcription items is atomically moved to the paused state, its queue generation is advanced, and its unfinished videos become `paused`. Whisper must never auto-resume at launch or merely because the project was opened; the user must press `Продовжити`.
- Queue pause is persisted per project. Paused projects remain paused across application restarts.
- Pausing immediately terminates the current managed subprocess and changes every unfinished item in that project to `paused`. Saved part checkpoints and progress remain intact.
- Aborting immediately terminates the current managed subprocess, changes unfinished items to `cancelled`, and deletes their checkpoints. Completed transcripts and prior atomic transcript versions remain intact.
- Each project has a persistent `queue_generation`. Pause and abort invalidate the previous generation; a fresh full-folder re-transcription creates a new generation. Every in-memory transcription job carries its generation, and the worker rejects stale jobs before starting Whisper. This prevents repeated abort/restart cycles from reordering or reviving old work.
- Job claiming is one conditional SQLite `UPDATE` that checks video status, availability, project pause, and generation atomically. Never reintroduce separate check-then-set calls; they allowed a pause race to launch a `processing` job.

Important resource decision: semantic embeddings must run on CPU. Do not move them back to Torch MPS without solving unified-memory contention with MLX Whisper. Running both on Metal caused a long transcription to stall at 8%. On Windows, semantic embeddings also remain on CPU so transcription behavior is predictable across CPU and optional CUDA machines.

### Model bootstrap and updates

- The HTTP server starts before models are ready so the UI can show the model gate.
- Model checks, downloads, queue workers, and semantic recovery do not start until the hardware preflight is accepted. Unsupported architecture/OS, unknown RAM/storage readings, less than 8 GB RAM, or less than 6 GB free space blocks model setup; lower-than-recommended resources produce an explicit slow-performance warning.
- `GET /api/hardware` exposes the report and available compute devices. `POST /api/hardware/confirm` persists the hardware fingerprint plus device choice in `hardware.json`. A material hardware/driver/resource-tier change requires acknowledgement again.
- The startup hardware cards show both the detected system/RAM/free-space value and the matching minimum/recommended requirement supplied by the same backend report. Keep these values data-driven from `hardware_check.py`; do not duplicate resource thresholds in UI copy.
- macOS Apple Silicon always uses MLX on the Apple GPU. Windows offers Auto, CPU, every NVIDIA device that CTranslate2 confirms is CUDA-available, and every non-NVIDIA device returned by the bundled Vulkan probe. AMD is the supported Vulkan target; Intel is labeled experimental. Semantic embeddings remain CPU-only regardless of this choice.
- Adding a newly detected CUDA/Vulkan device changes the hardware fingerprint and reopens the startup hardware gate before model bootstrap. A persisted `auto` choice resolves to the new preferred backend after confirmation; an explicit `cpu` choice remains selected until the user changes it. This is the migration path from pre-Vulkan releases.
- The footer shows the effective backend and device beside the version. Its themed dropdown may change among currently available devices only while model preparation, transcription queues, and semantic indexing are idle; both the browser and `/api/hardware/confirm` enforce this guard. A format-changing selection reopens the model gate before the main UI resumes.
- Model-progress polling must query only `/api/bootstrap`. Hardware inspection launches native CUDA/Vulkan probes and therefore runs once before bootstrap and once after readiness, never on every progress tick.
- Model bootstrap selects only the speech artifact required by the resolved backend. Switching between CUDA/CPU and Vulkan may therefore download the same Turbo model in a different runtime format; it does not change the model family.
- `GET /api/bootstrap` exposes overall status plus separate speech/meaning byte totals, downloaded bytes, percentages, transfer speed, estimated time remaining, current file, and errors.
- `POST /api/bootstrap/start` starts or retries the background check.
- Every launch verifies local snapshots and checks the current Hugging Face revision when online. A changed revision downloads through the same progress UI and updates `model-manifest.json` only after both model snapshots verify.
- Local snapshot checks always use the exact remote commit SHA when online or the saved manifest revision when offline. Individual files are downloaded by commit SHA and do not necessarily create a cached `refs/main`; never verify those downloads through an implicit `main`. A snapshot is ready only when every required file pattern exists.
- First launch requires the network. Later launches may proceed offline when both local snapshots are intact.
- Queue work blocks on the model manager, while project and static endpoints stay available.

### Application updates

- Automatic binary updates are enabled only in frozen builds whose embedded channel is `release`. Source and normal CI builds stay disabled; `ROTHBALD_ENABLE_UPDATER=1` exists only for controlled development tests.
- After the hardware/model startup gate is ready, the UI checks `https://github.com/BaldojniSylyUkrainy/Rothbald/releases/latest/download/latest.json` in a background thread. Manual checking remains available from the footer. A user-dismissed updater stays closed through the active check/download and terminal result; progress and retry remain reachable from the footer.
- `latest.json` is signed with Ed25519. `update_manifest.py` contains only the public key and accepts only schema 1, four-part versions, the exact Rothbald GitHub release path, exact supported platform filenames, positive sizes, and lowercase SHA-256 values.
- The matching private key must exist only as the protected `release` environment secret `ROTHBALD_UPDATER_PRIVATE_KEY` and in an encrypted owner backup. Never commit it or place it in a build artifact. Losing it after the first updater-enabled release prevents existing installations from trusting future manifests; rotating it requires a transition release signed by the existing key.
- The updater streams the selected installer into the platform application-data `updates/` directory, rejects files larger or smaller than the signed size, verifies SHA-256 before the atomic rename, and verifies size/SHA-256 again immediately before native launch.
- The updater modal has an explicit user-controlled visibility state. Polling may update a visible modal but must never reopen one the user closed. During background download the footer exposes progress; terminal success/error uses a toast plus footer action. `Пізніше` suppresses the same available version for 24 hours, while manual checking always overrides the snooze.
- On Windows, Rothbald starts the verified Inno Setup executable and then closes so installation can replace files. On macOS, it opens the verified notarized DMG and also closes so Finder can replace the application safely; replacement in `Applications` remains a user action.
- `RELEASE_NOTES.md` must begin with `# Rothbald <VERSION>`, contain a real section and bullet list, be substantive, and contain no placeholder markers. `scripts/validate_release_notes.py` is required in normal CI and release preflight. The release manifest embeds this exact text and GitHub Release uses the same file through `--notes-file`.
- The updater modal renders a deliberately restricted Markdown subset: headings, paragraphs, unordered lists, and ordered lists. All text is escaped before HTML insertion.

### Atomic transcript replacement

New transcription output is parsed before existing segments are deleted. Existing transcript/search data therefore remains available until a replacement succeeds, including when rescan detects changed source media. Rescan must never delete segments, semantic chunks, checkpoints, or transcript JSON preemptively. A successful replacement increments `transcript_revision`; semantic chunks are stamped with the same revision and are searchable only when `semantic_revision == transcript_revision`. If semantic reindexing fails, vectors from the older transcript can never leak into results. On transcription failure, the prior searchable data remains.

### Semantic indexing

- Sole model: `intfloat/multilingual-e5-large-instruct` (0.6B parameters, 1024-dimensional embeddings). The local Hugging Face cache is about 1.1 GB on the target machine.
- Runs locally on CPU through Hugging Face Transformers and PyTorch.
- Queries use the model's required `Instruct: ...\nQuery: ...` format with a transcript-retrieval instruction. Documents are embedded without a prefix, as required by the Instruct model.
- The model is multilingual; queries should use the same writing system as the source transcript because the same query also feeds the text-match tab.
- Whisper segments containing multiple sentences are split at sentence punctuation. Internal timestamps are estimated proportionally because Whisper only supplies the enclosing segment time range.
- Chunks end at natural pauses, completed sentences near the target size, or a likely lexical topic change measured across rolling neighboring segments. Soft context begins around 22 seconds/240 characters; hard bounds are about 80 seconds/850 characters. Abnormally long single Whisper segments are subdivided before chunking. Topic boundaries do not overlap, avoiding cross-topic contamination.
- Repetitive/hallucinated chunks are filtered using token-frequency and unique-word ratios.
- Normalized float32 embeddings are stored as SQLite BLOBs.
- Semantic indexing is persisted as `pending/indexing/ready/error` with a separate `semantic_progress`. The UI polls through final indexing and shows its own blue project-wide progress bar instead of making the green transcription bar appear stuck.
- Semantic similarity uses cosine similarity via a dot product of normalized vectors.
- Current semantic relevance floor is `0.70`, calibrated conservatively for the higher similarity distribution of Large Instruct. This is a relevance threshold, not a result-count limit.
- Stored chunk model IDs include the chunking version (`@natural-topic-v2`). On startup, every video with saved transcript segments but without the current model/version is marked for reindexing, regardless of its transcription queue status or media availability. Model or chunk-format migrations therefore cannot silently reuse incompatible vectors or lose offline transcripts during a paused re-transcription.

### Search behavior

Search always covers all indexed videos in the selected project.

The search bar contains only the query and the general `Знайти` button. Project switching belongs exclusively to the recent-project home screen; do not restore the redundant project dropdown inside the workspace.

- Text matching normalizes case and punctuation; maps common keyboard and orthographic variants; handles basic language-specific inflections; and permits one Damerau-Levenshtein edit (including adjacent transposition) in query words of at least five letters.
- The `Точні` tab returns every transcript segment matching all normalized query tokens. Matching candidates come from the indexed `segment_terms` vocabulary instead of scanning every transcript token for every request. Arbitrary interior substring matching is forbidden: `мир` must not match `Владимир` and `рост` must not match `просто`. It does not return a separate row for every repeated occurrence inside one segment.
- Semantic search returns every useful chunk at or above the relevance threshold, sorted by score.
- There are no count limits on exact or semantic results.
- The browser launches exact and semantic endpoints independently. Exact hits render as soon as they arrive; a slow semantic query does not block them. A newer query aborts both older requests and stale project responses are ignored.
- UI tabs: `За змістом`, `Точні`, and `Усі`, each with a full result count.
- All results stay in client state, but only 100 DOM rows are rendered at a time and additional rows are appended as the user scrolls. This is presentation virtualization, not a result limit.
- Semantic and exact results are intentionally not deduplicated across tabs.
- Result clicks seek the local player to approximately 1.5 seconds before the timestamp.

### Video selection

- Clicking a sidebar video selects it for playback.
- Clicking the selected sidebar video again clears it.
- The `×` button over the player also clears the selection.
- Clicking a search result selects/seeks the referenced video and leaves that exact or semantic result visibly highlighted. The highlight survives asynchronous result updates, incremental rendering, and tab switches. Clicking the same result again removes only its highlight without stopping playback; choosing another result transfers the highlight, while choosing a sidebar video or clearing the player removes it.
- A persistent caption strip directly below the player shows the selected video's full name and, when different, its project-relative path. It updates for both sidebar selection and search-result navigation, so the active source remains identifiable even when its sidebar row is far outside the scroll position.

### Project actions

- App launch shows a home screen with recent projects and no project opened by default. Unfinished transcription queues remain paused until the user explicitly presses `Продовжити` after opening a project. Pending semantic indexing may recover in the background, but Whisper transcription does not.
- A project is an internal SQLite record; the user does not manage a separate project file.
- Opening a recent project performs a quick existence check at the saved folder path without a recursive rescan.
- `Locate` changes the project's root folder while preserving stable video IDs, transcripts, semantic chunks, and queue history. Videos are reconnected using their relative paths.
- Missing media is visibly marked, cannot be played or requeued, but its completed transcript remains searchable.
- `До проєктів` returns to the empty home screen without deleting data or stopping background work.
- `Оновити папку` rescans and queues only new or modified media.
- `Перерозпізнати все` is available only when the current queue is idle.
- It requires browser confirmation and queues every currently available video in the selected project.
- It does not immediately delete old transcripts; each is atomically replaced after successful reprocessing.
- `Пауза` stops the active subprocess and pauses all queued videos for the current project; `Продовжити` requeues only those paused videos.
- `Abort` clears the unfinished queue and returns the project to an idle state. The user can then run `Перерозпізнати все` to rebuild the whole available folder from the beginning.
- `Видалити проєкт` deletes only its local database rows, checkpoints, vectors, and transcript JSON files after explicit browser confirmation. It never touches source media.

## Queue and ETA

The UI shows:

- per-video progress, percentage, and approximate ETA;
- total processed media duration versus project duration;
- completed video count;
- project-wide ETA estimated from the active video's observed processing speed.
- a separate semantic-index percentage and completed-video count.

ETA is approximate and stabilizes only after the active file has measurable progress.

## SQLite data

Database: `data/video_search.sqlite3`. Generated transcript JSON files: `data/transcripts/`. The entire `data/` directory is ignored by Git.

Core tables:

- `projects` — stable project ID, display name, saved root folder, creation/scan times, `last_opened_at`, pause state, and persistent queue generation.
- `videos` — stable ID, project-relative path, current absolute source path, availability, file fingerprint, queue state, progress, duration, errors, and semantic state.
- `segments` — timestamped Whisper transcript segments plus normalized text.
- `segment_terms` — deduplicated normalized term index per segment for scalable text lookup.
- `semantic_chunks` — larger timestamped passages, embedding BLOBs, model ID, and transcript revision.
- `transcription_parts` — resumable 30-minute part payloads keyed by video, source/model signature, and part index.

Schema additions are migrated in `init_storage()` using `PRAGMA table_info(videos)` and conditional `ALTER TABLE` statements. Preserve backward compatibility with the user's existing database.

Rolling SQLite backups live in `data/backups/`. Startup creates at most one per day; destructive local actions create one immediately; the newest five are retained. Backups and all generated data are Git-ignored.

`db()` is a custom context manager that commits successful transactions, rolls back failures, and always closes the SQLite connection. Do not change it back to returning a raw `sqlite3.Connection`: SQLite's own `with connection:` transaction context does not close the connection and previously leaked file descriptors until the worker crashed with `unable to open database file`.

The queue worker catches unexpected job-level failures so a secondary database/logging error cannot terminate the only worker thread and silently freeze the remaining queue.

Queue items are three-tuples `(action, video_id, queue_generation)`. Never enqueue a two-tuple or bypass the generation check for transcription jobs. Semantic-only jobs carry the generation for a consistent queue shape but are governed by their semantic/status checks. Slow `ffprobe` work belongs exclusively to the separate single-consumer `duration_jobs` queue so a sleeping drive cannot block transcription or semantic indexing.

## HTTP endpoints

- `GET /api/projects`
- `GET /api/bootstrap`
- `GET /api/hardware`
- `POST /api/hardware/confirm`
- `POST /api/bootstrap/start`
- `GET /api/update`
- `POST /api/update/check`
- `POST /api/update/download`
- `POST /api/update/install`
- `POST /api/projects/choose`
- `POST /api/projects/{id}/open`
- `POST /api/projects/{id}/locate`
- `POST /api/projects/{id}/scan`
- `POST /api/projects/{id}/retranscribe`
- `POST /api/projects/{id}/pause`
- `POST /api/projects/{id}/resume`
- `POST /api/projects/{id}/abort`
- `GET /api/videos?project={id}`
- `POST /api/videos/{id}/transcribe`
- `GET /api/search?q={query}&project={id}`
- `GET /api/search/exact?q={query}&project={id}`
- `GET /api/search/semantic?q={query}&project={id}`
- `DELETE /api/projects/{id}`
- `GET /media/{video_id}` with HTTP byte-range support
- `/` and `/static/*`

Static files send `Cache-Control: no-store` so UI fixes appear after reload.
State-changing requests reject foreign browser origins; missing Origin remains allowed for local command-line diagnostics.

The desktop launcher binds the local HTTP socket synchronously before it creates the WebView. A second instance or another process occupying the configured port must fail closed; never replace this with a readiness probe that could accept a response from an unrelated local service. Request-handler threads are daemonized so shutdown cannot hang behind an abandoned media request.

## Operational notes

- Server-side changes require stopping with `Control-C` and rerunning `start.command`.
- Static changes usually require only a hard browser reload, but restarting is the safest handoff instruction.
- If macOS blocks a command file, the user may need to open it through Finder's context menu or Privacy & Security.
- Keep the Terminal window open during long transcription runs. `start.command` automatically prevents idle system sleep through `caffeinate`; no manual Energy settings change is normally needed.
- Screen locking and display sleep are safe. Closing the MacBook lid normally forces sleep and cannot be reliably overridden by this app.
- External SSD disconnection causes media access errors but must not delete completed transcript data.
- `setup.command` performs Apple-Silicon, Python ≥3.11, ffmpeg/ffprobe, and 8-GB-free-space checks and installs `requirements-macos.lock`; `setup.ps1` installs `requirements-windows.lock`. Model verification/download now belongs to the in-app model gate.
- `setup.ps1` performs the corresponding Windows dependency checks and installs the platform-marked requirements.
- Packaged Windows builds must run `scripts/build_whisper_cpp_windows.ps1` before PyInstaller. It prepares a pinned SHA-256-verified Vulkan SDK when no system SDK is available, downloads and verifies the pinned whisper.cpp source archive, builds static `whisper-cli.exe` with Vulkan plus `rothbald-vulkan-probe.exe`, and places both under ignored `build/windows-tools/` for bundling.
- `.github/workflows/build.yml` tests and packages on native `macos-15` ARM64 and `windows-latest` runners for `main`, pull requests, and explicit manual dispatches. Pushing a release tag must not start a duplicate native build; the manually dispatched release workflow is the only post-tag build. Pull requests verify full packaging without uploading the large bundles; main/manual CI artifacts are retained for one day.
- `.github/workflows/release.yml` is manual-only and uses the `release` environment. Its free Ubuntu preflight accepts only an existing four-part tag that equals `VERSION`, points at the dispatched current `main` commit, validates `RELEASE_NOTES.md`, and requires every Apple and updater credential before native jobs start. It installs platform-specific runtime/build locks, launches a smoke test against each packaged native executable, builds an intentionally unsigned Windows Inno Setup installer, signs/notarizes/staples the Apple Silicon DMG, restores the runner's original Keychain search list, generates checksums plus a signed `latest.json`, and creates a draft release whose body is the same `RELEASE_NOTES.md`. The draft must be published manually before `/releases/latest` exposes it to installed applications.
- Build version metadata is generated from `VERSION` before PyInstaller. The packaged UI reads `/api/app`; never hardcode a displayed version in HTML or JavaScript. `scripts/versioning.py fix|feature` implements the documented `MAJOR.MINOR.PATCH.0` policy and synchronizes `VERSION`, release notes, and the manual workflow tag default; `check` is a CI contract.

### Local HTTP boundary

- The desktop API binds only to `127.0.0.1`, and every GET/POST/DELETE must also carry a `Host` matching the exact configured loopback port. This prevents a hostile DNS-rebinding page from reading local projects, paths, transcripts, search results, or media.
- Mutating requests additionally enforce a same-origin `Origin` when the header is present. JSON, static UI, and media responses carry same-origin resource and content-sniffing protections; the main document supplies the restrictive application CSP.

## Verification checklist

Run after changes:

```bash
ast-grep scan .
ast-grep test
.venv/bin/python -m py_compile app_info.py hardware_check.py process_utils.py server.py transcribe_video.py prepare_semantic.py prepare_models.py model_manager.py release_notes.py update_manifest.py updater.py rothbald.py scripts/prepare_build.py scripts/generate_release_manifest.py scripts/generate_updater_key.py scripts/smoke_packaged.py scripts/validate_release_notes.py scripts/versioning.py
node --check static/app.js
node --check static/update_flow.js
node --test tests/test_update_flow.cjs
.venv/bin/python scripts/validate_release_notes.py
.venv/bin/python scripts/versioning.py check
.venv/bin/python -m unittest discover -s tests -v
```

Also verify proportionally to the change:

- SQLite migration on a temporary database or backup;
- `progress_from_line()` against tqdm-style output;
- semantic model retrieval with `prepare_semantic.py`;
- semantic BLOB storage/search on a copied database;
- exact and semantic result counts without hidden limits;
- HTTP API after restarting the local server.

Do not modify or delete user media during testing. Prefer temporary files and copied databases.

## Known limitations

- Timestamps are file-relative, not embedded source timecode or Premiere sequence timecode.
- Embedded WebView playback depends on its codec/container support; transcription can support formats the player cannot preview.
- Speaker diarization is not implemented.
- Whisper may hallucinate repetitive text in poor audio; semantic indexing filters obvious repetition but cannot repair the transcript.
- Semantic relevance is threshold-based and may require future tuning or a user-adjustable sensitivity control.
- Exact results are segment-level rather than per-token-occurrence.
- Checkpoint resume is at 30-minute granularity; the one interrupted part restarts, not the entire media file.
- There is no queue reordering control.

## Packaging status

- Native CI bundles are prepared through PyInstaller and GitHub Actions; the manual release path additionally signs and notarizes Apple Silicon.
- The bundle opens as a standalone PySide6/QtWebEngine desktop window, not as a browser tab. PyInstaller embeds the platform-specific Dock/executable icon.
- Model updates are checked in-app against published Hugging Face revisions and downloaded with exact byte progress, speed, and ETA.
- Draft GitHub Release hosting, embedded build identity, macOS Developer ID signing/notarization, signed application-update manifests, verified in-app downloads, and native installer handoff are implemented. Windows artifacts are intentionally unsigned until a trusted Authenticode certificate exists and may trigger SmartScreen; the updater manifest remains Ed25519-signed independently.

## Changelog

### 2026-07-29 — unsigned Windows release parity

- Bumped the fix release from `0.4.1.0` to `0.4.2.0` and aligned the workflow with the actual `yt-dlp-BD` credential model: Apple remains Developer ID signed/notarized, while Windows builds without Authenticode because no trusted certificate exists. The signed updater manifest, checksums, packaged smoke gate, and draft-only publication remain mandatory.

### 2026-07-29 — packaged smoke title hotfix

- Bumped the fix release from `0.4.0.0` to `0.4.1.0`. The packaged macOS and Windows applications had built and served the correct UI, but the new smoke gate required an exact short `<title>` that did not match the real extended product title. The gate now recognizes the real title plus the application shell marker, with regression coverage.

### 2026-07-29 — MVP review hardening and version contract

- Closed the local HTTP DNS-rebinding read boundary, added fail-closed unknown-resource checks, stopped clean `SystemExit` from creating crash reports, and made the semantic retrieval instruction language-neutral.
- Preserved explicit updater dismissal throughout checks and downloads, added deterministic regression coverage, and made native CI/release jobs launch each packaged application and validate its served UI plus embedded version.
- Added the `MAJOR.MINOR.PATCH.0` release helper used by `yt-dlp BD`: fixes increment PATCH, features increment MINOR and reset PATCH, while the manual release tag default, release notes, and `VERSION` remain synchronized. Prepared feature version `0.4.0.0`.

### 2026-07-29 — visible hardware requirements

- Added compact, readable requirement lines beneath the detected system, memory, and free-space values in the startup preflight. The UI receives the same 8/16 GB RAM and 6/8 GB disk thresholds that enforce the hardware gate, so its explanation cannot silently drift from validation.

### 2026-07-29 — project language mode

- Added a persistent project-wide transcription language setting in the top toolbar. Existing projects migrate to `standard`; new projects use it by default. The alternative `auto` mode uses Whisper language detection consistently across MLX, faster-whisper, and whisper.cpp. Language mode is carried into the isolated subprocess and checkpoint signature, while busy queues reject a mid-run mode change.

### 2026-07-29

- Hardened MVP shutdown and media discovery: application/updater exit now pauses unfinished queues, advances queue generations, terminates all managed ffmpeg/Whisper process groups, drains pending jobs, and performs bounded worker joins. Folder scans stage filesystem metadata before one atomic database update, while duration probing moved out of startup and scans into a dedicated background queue with a 24-hour failure backoff. The footer backend selector now refreshes its server permission when the final transcription or indexing job becomes idle.
- Added regression coverage for shutdown persistence/process termination, failed-scan rollback behavior, deferred duration probing/backoff, and backend-permission refresh. The manual Windows release path now requires a protected PFX, Authenticode-signs and verifies both `Rothbald.exe` and the Inno Setup installer, and removes temporary signing material on every outcome.

### 2026-07-28

- Rebuilt updater UI state handling: closing or minimizing a download is now respected, background progress moves to the footer, completion/error produces a toast without forced reopening, `Пізніше` snoozes one version for 24 hours, manual checking reopens the current state, download retries reuse the verified manifest, and the updater dialog now traps/restores focus and supports Escape. Added deterministic Node state-flow tests to CI and made macOS quit after opening the verified DMG for safe application replacement.
- Replaced the Windows-native backend selects with themed dark menus, preserved the server-enforced busy-state guard, stopped model-progress polling from rerunning the Vulkan hardware probe every 500 ms, and applied `CREATE_NO_WINDOW` to runtime child processes so the packaged window no longer flashes consoles during setup or transcription.
- Fixed the Windows frozen-runtime lookup for bundled Vulkan executables: PyInstaller onedir stores collected binaries under its `_MEIPASS` (`_internal`) root, not necessarily beside `Rothbald.exe`. Added an explicit hardware-preflight revision so upgrades from the broken probe build reopen backend confirmation once, and exposed the effective MLX/CUDA/Vulkan/CPU backend beside the footer version.
- Bumped the feature release to `0.3.0.0` and added Windows AMD transcription through a bundled whisper.cpp Vulkan backend while preserving Whisper Large V3 Turbo. NVIDIA remains on faster-whisper/CUDA, Intel Vulkan is exposed as experimental, Auto prefers CUDA then discrete AMD then other Vulkan devices, and Vulkan failure retries the current part on whisper.cpp CPU.
- Added exact Vulkan device probing, backend-specific model bootstrap, dynamic transcription signatures, pinned SHA-256-verified whisper.cpp/Vulkan SDK builds, PyInstaller bundling, Windows CI/release coverage, and the upstream whisper.cpp license.
- Added and tested the pre-Vulkan upgrade path: newly detected CUDA/Vulkan devices invalidate the saved hardware fingerprint and reopen backend selection, while preserving an explicit CPU preference until the user changes it.
- Adopted ast-grep with a project config, tested structural rule against unsafe subprocess shell invocation, CI scan/test gates, and documented review commands.
- Prepared hotfix `0.2.0.2`: fixed post-download model verification on macOS and Windows by checking the exact Hugging Face commit SHA instead of an absent cached `main` ref. Complete already-downloaded snapshots are reused, and readiness now requires every configured model file pattern.
- Prepared hotfix `0.2.0.1`: rebuilt the macOS ICNS through Apple `iconutil` from a complete standard iconset so Finder and Spotlight receive valid 16 px and 32 px representations.
- Prevented the decorative model-gate background from creating phantom overflow, while preserving vertical scrolling on genuinely small screens. Added macOS iconset and model-gate overflow regression coverage.

### 2026-07-27

- Bumped the feature release to `0.2.0.0` and added a signed cross-platform updater. Release builds check the latest published GitHub Release, verify an Ed25519 manifest, stream and SHA-256-check the exact platform installer, render escaped release notes, and hand the verified installer to the native OS flow.
- Made `RELEASE_NOTES.md` a mandatory version-bound release artifact. CI/release preflight rejects missing, stale, short, or placeholder notes; the same Markdown becomes both the GitHub Release body and updater modal content.
- Added the one-time updater key generator, embedded only its public key, required the protected `ROTHBALD_UPDATER_PRIVATE_KEY` release-environment secret, and made manifest assembly verify the resulting signature before upload.
- Reworked the public README around capabilities, usage, Apple Silicon/Windows installation, updater behavior, and a prominent latest-release link.
- Prepared `0.1.2.0` as the public-release candidate and removed the duplicate `test-and-build` tag trigger. A release now costs one normal `main` CI matrix plus the intentionally dispatched signed release matrix, never an automatic third matrix on tag push. Large CI bundles are not uploaded for pull requests and are retained for only one day on main/manual runs.
- Replaced architecture-oriented release filenames with user-facing names: `Rothbald-<version>-Mac-Apple-Silicon.dmg`, the matching macOS ZIP, and `Rothbald-<version>-Windows-Setup.exe`. The manifest and checksum generator treats these names as the release contract.
- Released the `0.1.1.0` readiness hardening: rescan now distinguishes content changes from mtime-only changes and preserves the last searchable transcript/semantic revision until replacement succeeds.
- Made the desktop launcher own its listening socket before opening QtWebEngine, fail clearly on a second instance/occupied port, daemonize request handlers, and close the server cleanly with the window.
- Added separate reproducible Python 3.12 runtime and PyInstaller build locks for Apple Silicon and Windows x64. CI and release builds now install only the matching lock pair; dependency audit reported no known vulnerabilities.
- Raised packaged macOS support to 14.0 to match the pinned PyTorch wheel, synchronized the hardware gate and bundle metadata, and added regression coverage.
- Hardened the `yt-dlp-BD`-style manual release workflow: current-main/existing-tag preflight, least-privilege tokens, platform locks, original Keychain search-list restoration, verified existing tag use, and draft-only publication.
- Verified 22 Python regressions, JavaScript/Python syntax, lock compatibility, workflow syntax/actionlint, a real 1.4 GB Apple Silicon PyInstaller bundle, arm64 architecture, bundle metadata, deep code-sign structure, and a frozen local-API startup smoke.
- Added a mandatory pre-model hardware gate that checks supported OS/architecture, RAM, free disk, CPU capacity, and usable compute devices before any model download or queue worker starts. It blocks unsafe configurations, warns about likely slow operation, persists a hardware fingerprint, and rechecks after meaningful configuration changes.
- Added Auto / CPU / CUDA device selection on Windows and explicit Apple GPU/MLX reporting on macOS. Documented 8 GB RAM/6 GB disk minimum and 16 GB RAM/8 GB disk recommended requirements.
- Moved project navigation and destructive actions into a visible top toolbar, made project-card deletion explicit, and reduced the application footer to a compact bottom row.
- Added a quiet application footer with `baldojnisyly@gmail.com` support contact and the version embedded by the current build. The UI fetches `/api/app`; it contains no hardcoded version string.
- Added the single four-part `VERSION`, generated build metadata, macOS `Info.plist` versioning, Windows executable version resources, and regression coverage for embedded metadata precedence.
- Added a manual-only protected release workflow modeled on `yt-dlp BD`: Windows x64 verification, Apple Silicon Developer ID signing, hardened runtime, notarization, stapling, Gatekeeper validation, checksums, `latest.json`, and draft GitHub Release assembly.
- Added owner handoff and Apple credential documentation. Tauri updater keys are intentionally not reused; Rothbald uses its own Ed25519 manifest contract because it is a PyInstaller application.

- Replaced the development-oriented pywebview shell with PySide6 and QtWebEngine. The existing HTML/CSS interface remains unchanged inside a real cross-platform desktop window, while the native folder picker is safely bridged from backend request threads to the Qt main thread.
- Changed the release contract to an installable product: Apple Silicon ships through the signed/notarized DMG and Windows x64 through an Inno Setup executable. Both package the Python runtime, Qt, backend dependencies, ffmpeg, and ffprobe; end users do not install developer tools or open a browser.
- Added download speed and ETA to first-launch and model-update progress, updated regression coverage, separated developer source setup into `docs/DEVELOPMENT_UK.md`, and made the main README installation-only.
- Added packaged-runtime PATH setup for bundled ffmpeg/ffprobe and a local `Application Support/Rothbald/crash.log` for otherwise silent windowed-launch failures. A real frozen Apple Silicon smoke test loaded MLX Whisper, processed a WAV, and wrote its transcript JSON without relying on a system Python or ffmpeg.

### 2026-07-26

- Replaced the browser-launching packaged shell with a standalone desktop window sized for 13-inch screens (1280×800, 960×640 minimum).
- Reworked the UI into a predominantly monochrome Helvetica system using black, graphite, neutral gray, and white. Low-saturation pastel violet marks important numbers, indices, focus, selection, live progress, result markers, and functional card separators; pastel green is reserved for completion, while pastel red is reserved for destructive actions, errors, and unavailable media. Increased section, card, metadata, search, and player-caption spacing for dense text-heavy projects. Search remains prominent through structure and scale, and the completed-processing panel stays auto-collapsing and manually expandable.
- Replaced the generated single-letter icon with a deterministic black rounded `Ro` icon using a large white Bradley Hand mark; calibrated its transparent canvas for standard Dock sizing and added reproducible PNG, ICNS, ICO, favicon, and PyInstaller wiring.
- Replaced the home headline with a concise product explanation and capability list; removed SSD-specific language from all user-facing UI because project media may live on any accessible storage.
- Reduced repeated status styling in the video list: completed cards show one neutral `Готово` state without a full progress bar or a second semantic-ready line; hover and captions remain grayscale, while selection and separators use the restrained violet signal.
- Completed video cards use only a small green dot beside neutral `Готово`, preventing repeated color-heavy labels. When every media file is offline, processing now reports `Файли проєкту зараз недоступні`, highlights the state softly in red, confirms how many transcripts remain saved, and points to reconnecting the disk or using Locate while text search continues to work.
- Added an inline `Locate` action directly beside the all-media-offline explanation so the user can repair the project path without returning to the recent-project screen.

- Renamed the product and all primary UI identity to Rothbald (`rothpithnavach baldojnyi`) without using a `Z`; added a handwritten system-font wordmark and matching favicon.
- Rebuilt the interface as a dark editorial system inspired by the Spiilka/Kunsht case study: strict grid, oversized typography, thin rules, orange signal color, acid progress accents, flat project/result rows, and responsive layout.
- Added an in-app model gate with total and per-model progress, exact byte counts, current file, first-launch downloads, online revision checks, offline fallback for intact snapshots, and retryable errors.
- Added Windows support through Faster-Whisper Turbo, a native folder picker, portable child-process streaming/termination, Windows launch/setup scripts, and packaged data locations.
- Added a frozen launcher path for transcription subprocesses, bundled ffmpeg discovery, a PyInstaller onedir spec, native ARM64 macOS/Windows GitHub Actions builds, and repository ignore rules.
- Expanded regression coverage to ten tests, including packaged transcription routing and detached weighted model-progress snapshots; verified Python/JavaScript syntax, tests, startup UI, final home UI, and clean browser logs.

### 2026-07-24

- Added persistent exact/semantic result selection with a strong active-card state, `aria-pressed`, survival across tab/rerender updates, transfer to another result, and repeat-click deselection that leaves playback running.
- Added a responsive caption strip below the player with the selected video's full name and project-relative path; it clears with player selection and updates from both sidebar and search results.
- Completed the full pre-packaging review patch. Rebuilt the legacy video schema to remove global source-path uniqueness while retaining project-relative uniqueness and old transcripts; added revision-gated semantic indexes, fast `segment_terms`, rolling SQLite backups, strict `Locate` fingerprints, ffprobe timeouts outside write transactions, safe Range parsing, local-origin checks, and project deletion that never touches media.
- Replaced whole-file transcription with 30-minute resumable checkpoints plus two-second overlaps, temporary ffmpeg audio extraction, atomic queue claims, generation checks, and full process-group termination. Pause now preserves completed parts and Abort intentionally clears them.
- Split exact and semantic HTTP searches so exact results display without waiting for the embedding model; fixed false positives such as `мир` inside `Владимир` and `рост` inside `просто`; added client request cancellation and 100-row incremental rendering without truncating results.
- Added a separate semantic-index progress bar, fixed polling through final indexing, robust UI/network error handling, accessible labels/live regions/progress values, keyboard focus states, project deletion controls, and a favicon. Verified the finished UI in the local in-app browser with clean console logs.
- Added deterministic dependency locking, setup preflight checks, setup-time model downloads, runtime offline Hugging Face mode, and eight regression tests covering old-database migration, foreign-key integrity, atomic queue claims, real checkpoint reuse/reassembly, typo/inflection matching, false-positive prevention, checkpoint ranges, normalization, and HTTP byte ranges.
- Removed the stray `.start.command.swp` and generated Python caches; added ignore rules for future swap files.
- Downloaded the previously missing Turbo weights, constrained setup downloads to runtime-required files (excluding the unused 2+ GB ONNX export), removed 632+ MB of interrupted duplicate blobs, and verified both models offline. A real one-second MLX/Metal Turbo transcription smoke test completed successfully.

- Migrated semantic retrieval to the locally cached and verified `intfloat/multilingual-e5-large-instruct`, including its required query instruction, raw document format, 1024-dimensional vector validation, a `0.70` relevance floor, and versioned semantic indexes. Measured warm query encoding at about 0.9 seconds on the target M1; initial model load was about 7.5 seconds.
- Replaced fixed minute chunks with natural transcript blocks: sentence splitting with proportional internal timestamps, pause-aware and sentence-aware boundaries, rolling lexical topic-change detection, and hard size/time bounds. On the former 11.5-hour test corpus this produced 882 filtered blocks averaging about 24 seconds, with no block over 58 seconds.
- Added typo-tolerant text matching without result limits: keyboard-letter and orthographic normalization, basic inflections, and one edit/transposition for words of five or more letters. The same query feeds both the text and semantic tabs.
- Removed the sole first/test project `Виступи` from local storage at the user's request, including its 11 video records, 13,199 segments, 637 old semantic chunks, and 11 JSON transcripts. No source media was touched. SQLite integrity remains OK; the temporary verification backup was also removed after all migration tests passed.
- Removed the obsolete 470 MB `multilingual-e5-small` cache after Large Instruct passed a real local retrieval check.
- Fixed semantic migration/recovery to use the presence of saved transcript segments rather than requiring video status `done`; paused, cancelled, or offline videos with existing text can now rebuild an incompatible/missing semantic index without restarting Whisper.
- Audited application storage after switching models: all 11 transcript JSON files match the 11 database video records and SQLite integrity is OK. Removed the obsolete 459 MB `whisper-small-mlx` Hugging Face cache while retaining the required semantic model, environment, database, and transcripts.
- Replaced the sole transcription model with `mlx-community/whisper-large-v3-turbo`; removed the `WHISPER_MODEL` override and model-choice instructions so every new or repeated transcription consistently uses Turbo.
- Recorded automatic update checking/downloading as a requirement for the future signed native macOS application; it is deliberately not implemented in the current development launcher.
- Changed restart behavior so existing transcription queues never auto-run. Interrupted/queued work is persisted as paused with an advanced generation and starts only after the user opens the project and presses `Продовжити`.
- Added persistent queue generations so stale in-memory transcription jobs from repeated Pause/Abort/restart cycles are rejected before Whisper starts. A new full-folder run also advances the generation.
- Backed up the live database and removed the orphaned development fixture `sample.wav`/`testproject` plus its cascaded segment and semantic chunk. Post-cleanup checks show zero duplicate rows, zero orphans, and `integrity_check=ok`.
- Fixed a recurring SQLite file-descriptor leak: every database context now explicitly commits/rolls back and closes. Added worker-level exception recovery so one failed job cannot kill the entire queue thread. Verified 3,000 consecutive database contexts with no descriptor growth.
- Wrapped the server process in macOS `caffeinate -i`, allowing the screen to lock or turn off while preventing the Mac from entering idle system sleep during transcription.
- Removed the redundant project selector from the search bar and renamed the general search action from `Знайти за змістом` to `Знайти`; result-type tabs still separate semantic and exact matches.
- Added persistent per-project queue pause/resume and immediate abort controls, including safe Whisper subprocess termination and stale-job guards.
- Added `paused` and `cancelled` video states. Resume restarts only the interrupted/paused files; abort preserves completed transcripts and enables a fresh full-folder re-transcription.
- Added a Photoshop/Premiere-style recent-project home screen; startup no longer auto-opens the last project.
- Added stable project-relative video identity, quick media availability checks, missing-media states, and `Locate` for moved or renamed folders.
- Changed rescanning and startup checks to preserve transcripts and semantic indexes when media or an external SSD is missing.
- Added `READMEAI.md` as persistent technical memory for repository maintenance.
- Added full-folder re-transcription with confirmation, idle-queue protection, and atomic transcript replacement.
- Added repeat-click and player `×` controls to clear video selection.
- Diagnosed a real transcription stall at 8%; reserved Metal for Whisper, moved semantic embeddings to CPU, and added stalled-state UI plus a 15-minute watchdog.
- Removed exact and semantic result-count limits and split results into `За змістом`, `Точні`, and `Усі` tabs.

### 2026-07-23

- Built the initial local M1 application with native project-folder selection, recursive media discovery, MLX Whisper transcription, SQLite storage, and timestamped playback.
- Added per-video and whole-queue progress/ETA.
- Added project-wide search independent of player selection.
- Added multilingual semantic search with `multilingual-e5-small`, hybrid exact/semantic results, and semantic-noise filtering.
- Fixed long filenames overflowing the sidebar into the player.
- Added cache prevention for static assets.
