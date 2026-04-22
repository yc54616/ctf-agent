# CTF Agent

Autonomous multi-model CTF solver with an operator console, per-challenge swarms, pause/restart controls, and manual review when you want a human in the loop.

This repository was originally built by [Veria Labs](https://verialabs.com) and used to win **1st place at BSidesSF 2026 CTF**. If you are reading this in a fork: thank you for forking it, adapting it, and pushing it further.

## What This Fork Does Well

- Runs multiple models against multiple challenges in parallel
- Imports a competition URL once, keeps a normalized challenge store on disk, and reuses it on later runs
- Captures browser-backed auth state automatically and reattaches remote solved polling / flag submission from saved metadata
- Lets you **pause**, **restart from saved notes**, and **prioritize** challenges from the browser
- Handles deploy-on-demand labs with operator-managed instance checks and restart flow
- Treats flag candidates as reviewable state instead of an all-or-nothing dead end
- Saves winning artifacts automatically when a challenge is confirmed solved

## Quick Start

### 1. Install and build

```bash
uv sync
uv run playwright install chromium
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
```

If your default Ubuntu mirror is slow:

```bash
docker build \
  --build-arg UBUNTU_MIRROR=http://mirror.kakao.com/ubuntu \
  -f sandbox/Dockerfile.sandbox \
  -t ctf-sandbox .
```

### 2. Import a competition URL

The default flow is now:

1. `ctf-import --url <competition-url>`
2. `ctf-solve --challenges-dir ./challenges`

If you do not pass `--cookie-file`, `ctf-import` opens Chromium through Playwright, waits for you to finish any required login, and saves browser state under `.cache/browser-sessions/` in this repo.

That saved session reference is written into the imported metadata, so later `ctf-solve` runs can auto-reattach remote solved polling and flag submission without asking for the same auth again.

Example:

```bash
uv run ctf-import \
  --url https://dreamhack.io/career/competitions/2026-GMDSOFT
```

### 3. Start the coordinator from the saved challenge store

```bash
uv run ctf-solve \
  --challenges-dir ./challenges \
  --max-challenges 4 \
  -v
```

Restore previous paused/requeueable work from saved notes instead of clearing runtime state:

```bash
uv run ctf-solve \
  --challenges-dir ./challenges \
  --max-challenges 4 \
  --restore \
  -v
```

Stopping `ctf-solve` is graceful by default: the first `Ctrl+C` or `SIGTERM` asks the coordinator to stop swarms cleanly and close the operator runtime. A second `Ctrl+C` forces the process to exit if shutdown is stuck. During coordinator shutdown, unsolved lane containers/workspaces are cleaned up by default instead of being preserved as stopped snapshots.

Refresh imported source metadata later without deleting overrides or solve artifacts:

```bash
uv run ctf-import \
  --url https://dreamhack.io/career/competitions/2026-GMDSOFT \
  --refresh
```

Imported competitions create a layout like:

```text
challenges/
└─ <competition>/
   ├─ competition.yml
   ├─ .remote/
   │  └─ automation-profile.json
   ├─ .source-cache/
   └─ <challenge>/
      ├─ metadata.yml
      ├─ distfiles/
      └─ .runtime/
         └─ override.json
```

`competition.yml` stores the competition-level auth reference and remote automation profile reference. Each challenge `metadata.yml` stores source metadata, per-challenge status, and the same remote profile reference so runtime auto-attach can happen from local files alone.

Compatibility/debugging flags still exist:

- `ctf-import --cookie-file ...`: reuse a raw Cookie header file instead of opening Playwright
- `ctf-import --platform-spec ...`: load extra declarative import specs when the heuristic parser is not enough
- `ctf-solve --cookie-file ...`: override saved imported auth for the current run
- `ctf-solve --models ...`: add or replace lane models for the current run
- `ctf-solve --no-submit`: disable automatic remote submit
- `ctf-solve --local`: skip all remote fetch/submit logic
- `ctf-solve --ctfd-url/--ctfd-token`: direct legacy CTFd mode

### 4. Open the operator console

```bash
uv run ctf-status
# opens http://127.0.0.1:9400/ui by default
```

Other useful views:

```bash
uv run ctf-status --once
uv run ctf-status --text
uv run ctf-status --text --verbose
uv run ctf-status --json-output
```

### 5. Send live guidance

Send a message to the coordinator:

```bash
uv run ctf-msg "focus on web and crypto first"
```

Send a targeted bump to one running lane:

```bash
uv run ctf-bump \
  --challenge "Midnight Roulette" \
  --model "codex/gpt-5.4" \
  "Check the admin route and inspect the JWT claims."
```

## Mental Model

Think of the system as three layers:

```text
┌──────────────────────────────────────────────────────────────┐
│ Operator                                                    │
│  Browser UI, status views, manual confirm/reject, reprioritization │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Coordinator                                                 │
│  - watches challenge inventory                              │
│  - fills active challenge slots                             │
│  - reacts to candidates, solves, pauses, retries           │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Challenge Swarms                                            │
│  each challenge gets one swarm, each swarm runs many lanes  │
│                                                              │
│  challenge A: 5.4 | 5.4-mini | 5.3-spark | extra lanes ... │
│  challenge B: 5.4 | 5.4-mini | 5.3-spark | extra lanes ... │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Docker Sandbox                                              │
│  isolated tools, workspace, challenge files, shared artifacts │
└──────────────────────────────────────────────────────────────┘
```

## How It Actually Runs

### Challenge scheduling

`--max-challenges` controls **how many challenges are active at once**, not how many lanes exist in total.

```text
pending queue ──▶ active swarms (up to max active challenges)
```

If `--max-challenges 4`, the coordinator will try to keep up to 4 challenges active at a time. Each active challenge then fans out into multiple model lanes.

### Scheduler states

```text
queued
priority_waiting
ctfd_retry
candidate_retry
candidate_pending
flag_found
```

What they mean:

- `queued`: normal waiting
- `priority_waiting`: held on purpose; do not auto-run until restored
- `ctfd_retry`: historical state name for waiting on remote solved-state refresh/pull to recover
- `candidate_retry`: a candidate was rejected or came back incorrect, so the challenge re-enters the queue
- `candidate_pending`: challenge is paused because a serious candidate is under review
- `flag_found`: solved and done

### Candidate lifecycle

This is the part most people care about operationally:

```text
serious candidate found
        │
        ▼
challenge pauses
        │
 ┌──────┴───────────────┐
 │                      │
 ▼                      ▼
confirm/correct         reject/incorrect
        │                      │
        ▼                      ▼
   challenge done        requeue challenge
```

More concretely:

- In normal remote mode:
  - a serious candidate may be auto-submitted to the imported remote platform
  - if the platform says `correct` or `already solved`, the challenge stops
  - if the platform says `incorrect`, the candidate stays manually reviewable and the challenge can go back to waiting
- In `--no-submit` or `--local`:
  - candidates are not auto-confirmed remotely
  - the operator can confirm or reject them from the UI

### Pause, priority waiting, and restart

The scheduler now distinguishes **hold** from **restart**:

```text
▶ active challenge
   └─ Pause to priority waiting
      └─ held, not auto-spawned

priority_waiting
   └─ Restore waiting
      └─ becomes normal queue entry again

held or queued challenge
   └─ Restart from saved notes
      └─ new swarm starts with saved restart notes
```

Important detail:

- `Restart from saved notes` does **not** resurrect the exact same container process.
- It starts a fresh swarm and injects saved handoff context so lanes continue from prior work instead of starting blind.
- Treat it as a fresh restart, not a warm resume: the previous container, workspace, and provider session are discarded.

## Modes and Semantics

| Mode | Challenge source | Flag submission | Who confirms solves? |
|------|------------------|-----------------|----------------------|
| default remote mode | imported remote platform + local preload | enabled | remote platform or operator override |
| `--no-submit` | imported remote platform + local preload | disabled | operator |
| `--local` | local or imported challenge dirs only | disabled | operator |

Notes:

- `--ctfd-url` and `--ctfd-token` are compatibility overrides for legacy direct CTFd mode.
- `--restore` is a startup mode modifier, not a separate execution mode.
- `--restore` restores pending/requeueable challenge work from saved notes and restarts lanes fresh.
- `--restore` is also **not** a warm resume of the old sandbox. It rebuilds fresh lanes from recorded runtime notes rather than reattaching to the previous container/session.
- `--restore` keeps saved queue/runtime notes on disk for recovery, instead of doing the normal startup cleanup pass first.
- `candidate_pending` entries are intentionally **not** auto-resumed at startup. They stay review-driven.

## Operator Console

Open it with:

```bash
uv run ctf-status
```

The browser UI gives you:

- live challenge and lane status
- candidate confirm / reject
- external solve reporting
- challenge config editing for connection overrides, operator notes, and `needs_instance`
- `max active challenges` runtime changes
- `Prefer this challenge when queued`
- `Pause to priority waiting`
- `Restore waiting`
- `Restart from saved notes`
- `Check instance`
- `Check and restart`
- trace browsing and artifact links

### Runtime max active changes

Changing max active challenges is a **soft cap**:

```text
4 → 6  : fill more slots if work is available
6 → 3  : do not kill running swarms; just stop filling above 3
```

This is intentional. Lowering the cap does not abruptly kill active work.

### Priority waiting

`priority_waiting` means:

- the challenge stays in the scheduler state
- it is visible in the UI
- it will **not** auto-run again until restored

This is useful when you want to clear the deck, keep the work, and come back later.

`Prefer this challenge when queued` is different:

- it does **not** pause or hold the challenge
- it only marks that challenge to sort ahead of normal queue entries the next time it is queued

### Editing challenge connection info

If a remote host, port, URL, or raw connect command changes while a challenge is in progress, you can update it from the operator UI instead of editing `metadata.yml` by hand.

- Open `ctf-status`, select the challenge, and save a challenge config override.
- Connection overrides are written to `.runtime/override.json` and reflected in `.runtime/effective-metadata.yml`.
- The running swarm metadata is refreshed immediately, so the UI and future restart context see the new connection info.
- For in-flight lanes, the safest workflow is: save the override, then use `Restart from saved notes` so fresh lanes start from the updated connection details.
- For deploy-on-demand labs, mark `Manual instance step required`, let the user start/deploy the instance, then use `Check instance` or `Check and restart` after saving the new host/port/url.
- Imported `source.needs_vm` now surfaces as `needs_instance` in effective metadata, and the operator override can still turn that workflow off if the challenge no longer needs a manual deploy step.
- `needs_instance` is intentionally broad: use it both for "start the first VM before solving" and for "enter the portal, deploy/check another VM, then continue" style labs.
- If a challenge has multiple lab hops, you can still seed `instance_stages` in `metadata.yml`, but the operator UI can now save edited stage definitions into `.runtime/override.json` too.
- Each stage can carry multiple named `endpoints`; the UI saves a live `current_endpoint` plus per-endpoint runtime connection values under `.runtime/override.json`.
- The effective top-level `connection` always follows the current stage and active endpoint, so existing solvers and prompts still see one active target even when the operator is walking through a multi-stage lab.
- If the current stage is marked `done`, the effective workflow advances to the next unfinished stage automatically.

Example:

```yaml
instance_stages:
  - id: public_lab
    title: Public Lab
    manual_action: deploy_from_portal
    connection:
      url: https://portal.example/lab
  - id: internal_vm
    title: Internal VM
    manual_action: deploy_inside_lab
```

## Typical Workflows

### Fresh competition start

```bash
uv run ctf-import --url https://...
uv run ctf-solve --challenges-dir challenges --max-challenges 4 -v
uv run ctf-status
```

### Restore yesterday's paused work

```bash
uv run ctf-solve --challenges-dir challenges --max-challenges 4 --restore -v
```

### Work fully locally

```bash
uv run ctf-solve --local --challenges-dir challenges --max-challenges 4 -v
```

### Sync with the imported remote platform, but keep human approval

```bash
uv run ctf-solve --challenges-dir challenges --max-challenges 4 --no-submit -v
```

### Deploy-on-demand lab with a manual instance step

1. Import the challenge normally and start `ctf-solve`.
2. In the operator UI, leave `Manual instance step required` enabled if the lab still needs a human deploy/check step.
3. Start or deploy the instance from the competition site.
4. Save the resulting host/port/url override in challenge config.
5. If the current stage has multiple targets, select the active endpoint in challenge config before saving.
6. Use `Check instance` to confirm the target is live, or `Check and restart` to both verify it and relaunch fresh lanes from saved notes.

### Legacy direct CTFd compatibility

```bash
uv run ctf-solve --ctfd-url https://ctfd.example --ctfd-token ... --challenges-dir challenges --max-challenges 4 -v
```

### Solve outside the swarm, then tell the system

Use the operator UI:

- `Mark solved`
- enter the challenge name and flag
- the challenge is recorded as solved and removed from the active queue

## Commands

| Command | Purpose |
|--------|---------|
| `uv run ctf-import --url ...` | import a competition into the local challenge store |
| `uv run ctf-solve ...` | start the coordinator |
| `uv run ctf-status` | open browser operator UI |
| `uv run ctf-status --once` | one-shot snapshot |
| `uv run ctf-status --text` | terminal dashboard |
| `uv run ctf-status --json-output` | raw JSON status |
| `uv run ctf-msg "..."` | send a message to the coordinator |
| `uv run ctf-bump --challenge ... --model ... "..."` | send targeted advice to one lane |

## Directory Layout

### Imported competition tree

```text
challenges/<competition>/
├── competition.yml
├── .remote/
│   └── automation-profile.json
├── .source-cache/
└── <challenge>/
    ├── metadata.yml
    ├── distfiles/
    ├── .runtime/
    │   ├── effective-metadata.yml
    │   └── override.json
    ├── .lane-state/
    ├── .shared-artifacts/
    └── solve/
        ├── result.json
        ├── flag.txt
        ├── writeup.md
        ├── trace.jsonl
        └── workspace/
```

Notes:

- `override.json` exists only when you save an operator override.
- `--challenges-dir` can point at the whole imported tree or at a single challenge directory that contains `metadata.yml`.
- `.lane-state/` and `.shared-artifacts/` are runtime state on disk; they are cleared on a fresh start and reused by `--restore`.

### Inside the sandbox

The lane sees these important container paths:

```text
/challenge/metadata.yml
/challenge/distfiles/
/challenge/challenge-src/
/challenge/workspace/
/challenge/shared-artifacts/
/challenge/agent-repo/
```

Notes:

- `/challenge/challenge-src/` is a read-only mount of the full challenge directory.
- `/challenge/workspace/` is the writable scratch area for active lanes.
- Local skill docs are available under `/challenge/agent-repo/ctf-skills/`.

### What gets saved when a challenge is solved

```text
solve/
├── result.json   # final status, winner, metadata
├── flag.txt      # confirmed flag
├── writeup.md    # draft writeup
├── trace.jsonl   # winning lane trace, when available
└── export/       # selected exploit/scripts/notes export, when available
```

Solved lanes no longer copy the entire scratch workspace by default. Instead, `solve/export/` keeps a small, heuristic selection of likely-important files such as referenced exploit scripts, recent text notes, executable helpers, and shebang scripts, plus `solve/export/MANIFEST.md` explaining why each file was kept.

If you confirm an external solve without a live winning workspace, `flag.txt` and `result.json` still exist, but `trace.jsonl`, `writeup.md`, or `export/` may be absent.

## Code Structure

| File | Role |
|------|------|
| `backend/import_cli.py` | `ctf-import` entry point |
| `backend/importers/` | competition importers and heuristics |
| `backend/platforms/` | imported remote platform clients and browser-backed automation |
| `backend/challenge_config.py` | source metadata, overrides, effective metadata rendering |
| `backend/instance_probe.py` | operator-side connection readiness checks |
| `backend/cli.py` | CLI entry points: `ctf-solve`, `ctf-status`, `ctf-msg`, `ctf-bump` |
| `backend/prompts.py` | challenge metadata model and solver system prompts |
| `backend/agents/coordinator_loop.py` | shared coordinator event loop and operator API server |
| `backend/agents/coordinator_core.py` | scheduler, queue logic, runtime actions |
| `backend/agents/codex_coordinator.py` | Codex-backed coordinator |
| `backend/agents/swarm.py` | one challenge swarm, candidate handling, restart handoff |
| `backend/agents/codex_solver.py` | Codex lane runtime |
| `backend/agents/gemini_solver.py` | Gemini lane runtime |
| `backend/agents/solver.py` | legacy Claude/Pydantic lane runtime |
| `backend/ctfd.py` | legacy direct CTFd client |
| `backend/sandbox.py` | Docker sandbox lifecycle |
| `backend/operator_ui.py` | UI data assembly |
| `backend/static/` | browser UI |

## Models

Default lane set lives in `backend/models.py`.

Current default lineup includes:

- `codex/gpt-5.4`
- `codex/gpt-5.4-mini`
- `codex/gpt-5.3-codex-spark`

This default is intentionally compact. If you want extra lanes such as Gemini, add them explicitly with `--models`.

Notes:

- Coordinator backend is currently `codex` only.
- Gemini lanes are supported if Gemini home auth is present.
- Claude solver lanes are intentionally disabled; Claude is advisor-only today.

You can override the lineup at startup:

```bash
uv run ctf-solve \
  --models codex/gpt-5.4 \
  --models codex/gpt-5.4-mini \
  --models codex/gpt-5.3-codex-spark \
  --models gemini/gemini-2.5-flash
```

## Sandbox Tooling

The Docker image includes a broad CTF toolbox, including:

- pwntools, radare2, GDB, gdb-multiarch
- SageMath, z3, RsaCtfTool, pycryptodome
- binwalk, foremost, Sleuthkit, volatility3
- ffmpeg, sox, steghide, stegseek, zsteg, tesseract
- curl, ffuf, gobuster, sqlmap, nmap
- gcc, clang-style toolchains, node, npm, rust, cargo, jq, sqlite3

Run the smoke test after changing the image:

```bash
docker run --rm ctf-sandbox sandbox-smoke-check
```

The sandbox also mounts the local skill library at `/challenge/agent-repo/ctf-skills/`. Prompts point lanes at targeted `SKILL.md` reads there when a challenge category matches.

Provider CLIs now support a persistent runtime cache outside the image build too:

- the image still ships working baseline versions
- each lane container mounts a host cache at `.cache/runtime-tools/` under this repo by default
- on container start, `refresh-provider-tooling` can refresh cached `codex`, `gemini`, and `claude-agent-sdk`
- fresh containers then reuse the cached copies first from `PATH` / `PYTHONPATH`, so you do not need to rebuild the whole image for every upstream CLI bump

## Configuration Notes

All runtime settings can come from CLI flags, `.env`, or `backend/config.py`.

Useful defaults:

| Setting | Default | Meaning |
|---------|---------|---------|
| `--max-challenges` | `10` | max active challenges |
| `container_memory_limit` | `4g` | per-container memory cap |
| `sandbox_image` | `ctf-sandbox` | Docker image name |
| `sandbox_runtime_tools_auto_update` | `true` | refresh cached provider tooling on container start |
| `sandbox_runtime_tools_refresh_interval_seconds` | `86400` | minimum delay between runtime tooling refresh attempts |
| `sandbox_runtime_tools_dir` | `.cache/runtime-tools` | host cache used for refreshed provider tooling |
| `msg_port` | `9400` | operator UI / API port |

Authentication is expected through home auth:

- `codex`: `~/.codex/auth.json`
- `claude`: `~/.claude/.credentials.json`
- `gemini`: `~/.gemini/oauth_creds.json`

Remote platform auth can also come from:

- saved Playwright browser sessions created by `ctf-import`
- `--cookie-file` with a raw HTTP `Cookie:` header value

## Requirements

- Python 3.14+
- Docker
- `uv`
- Codex home auth for Codex lanes/coordinator
- Gemini CLI if you want Gemini lanes
- Claude home auth if you want Claude advisor fallback

## Results

| Competition | Solved | Result |
|-------------|:-:|--------|
| BSidesSF 2026 | 52/52 | 1st place |

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
- Everyone who forks this repo, experiments with it, and makes it more useful in real competitions — thank you
