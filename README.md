# CTF Agent

Autonomous multi-model CTF solver with an operator console, per-challenge swarms, pause/resume controls, and manual review when you want a human in the loop.

This repository was originally built by [Veria Labs](https://verialabs.com) and used to win **1st place at BSidesSF 2026 CTF**. If you are reading this in a fork: thank you for forking it, adapting it, and pushing it further.

## What This Fork Does Well

- Runs multiple models against multiple challenges in parallel
- Lets you **pause**, **resume previous work**, and **prioritize** challenges from the browser
- Supports **CTFd mode**, **CTFd sync with submission disabled**, and **fully local mode**
- Treats flag candidates as reviewable state instead of an all-or-nothing dead end
- Saves winning artifacts automatically when a challenge is confirmed solved

## Quick Start

### 1. Install and build

```bash
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
```

If your default Ubuntu mirror is slow:

```bash
docker build \
  --build-arg UBUNTU_MIRROR=http://mirror.kakao.com/ubuntu \
  -f sandbox/Dockerfile.sandbox \
  -t ctf-sandbox .
```

### 2. Configure CTFd credentials

```bash
cp .env.example .env
```

Then edit `.env`:

```env
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token
```

### 3. Start the coordinator

Normal CTFd mode:

```bash
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 4 \
  -v
```

Resume previous paused/requeueable work instead of clearing runtime state:

```bash
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 4 \
  --resume \
  -v
```

Local-only mode:

```bash
uv run ctf-solve \
  --local \
  --challenges-dir challenges \
  --max-challenges 4 \
  -v
```

CTFd sync, but no automatic flag submission:

```bash
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 4 \
  --no-submit \
  -v
```

### 4. Open the operator console

```bash
uv run ctf-status
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
│  challenge A: gpt-5.4 | gpt-5.4-mini | gemini | ...         │
│  challenge B: gpt-5.4 | gpt-5.4-mini | gemini | ...         │
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
- `ctfd_retry`: waiting for CTFd refresh/pull to recover
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

- In normal CTFd mode:
  - a serious candidate may be auto-submitted
  - if CTFd says `correct` or `already solved`, the challenge stops
  - if CTFd says `incorrect`, the candidate stays manually reviewable and the challenge can go back to waiting
- In `--no-submit` or `--local`:
  - candidates are not auto-confirmed through CTFd
  - the operator can confirm or reject them from the UI

### Pause, priority waiting, and resume

The scheduler now distinguishes **hold** from **restart**:

```text
▶ active challenge
   └─ Pause to priority waiting
      └─ held, not auto-spawned

priority_waiting
   └─ Restore waiting
      └─ becomes normal queue entry again

held or queued challenge
   └─ Resume previous work
      └─ new swarm starts with saved resume packet
```

Important detail:

- `Resume previous work` does **not** resurrect the exact same container process.
- It starts a fresh swarm and injects saved handoff context so lanes continue from prior work instead of starting blind.

## Modes and Semantics

| Mode | Challenge source | Flag submission | Who confirms solves? |
|------|------------------|-----------------|----------------------|
| default CTFd mode | CTFd + local preload | enabled | CTFd or operator override |
| `--no-submit` | CTFd + local preload | disabled | operator |
| `--local` | local challenge dirs only | disabled | operator |
| `--resume` | same as selected mode | same as selected mode | same as selected mode |

Notes:

- `--resume` is a startup mode modifier, not a separate execution mode.
- `--resume` skips runtime cleanup and restores pending/requeueable challenge work from saved runtime state.
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
- `max active challenges` runtime changes
- `Pause to priority waiting`
- `Restore waiting`
- `Resume previous work`
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

## Typical Workflows

### Fresh competition start

```bash
uv run ctf-solve --ctfd-url ... --ctfd-token ... --challenges-dir challenges --max-challenges 4 -v
uv run ctf-status
```

### Resume yesterday's paused work

```bash
uv run ctf-solve --ctfd-url ... --ctfd-token ... --challenges-dir challenges --max-challenges 4 --resume -v
```

### Work fully locally

```bash
uv run ctf-solve --local --challenges-dir challenges --max-challenges 4 -v
```

### Sync with CTFd, but keep human approval

```bash
uv run ctf-solve --ctfd-url ... --ctfd-token ... --challenges-dir challenges --no-submit -v
```

### Solve outside the swarm, then tell the system

Use the operator UI:

- `Mark solved`
- enter the challenge name and flag
- the challenge is recorded as solved and removed from the active queue

## Commands

| Command | Purpose |
|--------|---------|
| `uv run ctf-solve ...` | start the coordinator |
| `uv run ctf-status` | open browser operator UI |
| `uv run ctf-status --once` | one-shot snapshot |
| `uv run ctf-status --text` | terminal dashboard |
| `uv run ctf-status --json-output` | raw JSON status |
| `uv run ctf-msg "..."` | send a message to the coordinator |
| `uv run ctf-bump --challenge ... --model ... "..."` | send targeted advice to one lane |

## Directory Layout

### Challenge working tree

```text
challenges/<challenge>/
├── metadata.yml
├── distfiles/              # pulled files or local challenge data
├── challenge-src/          # unpacked app/source when relevant
├── workspace/              # active lane scratch space
├── .lane-state/            # runtime lane control / handoff state
├── .shared-artifacts/      # shared outputs, manifests, digests
└── solve/
    ├── result.json
    ├── flag.txt
    ├── writeup.md
    ├── trace.jsonl
    └── workspace/
```

### What gets saved when a challenge is solved

```text
solve/
├── result.json   # final status, winner, metadata
├── flag.txt      # confirmed flag
├── writeup.md    # draft writeup
├── trace.jsonl   # winning lane trace, when available
└── workspace/    # winning workspace snapshot, when available
```

If you confirm an external solve without a live winning workspace, `flag.txt` and `result.json` still exist, but `trace.jsonl`, `writeup.md`, or `workspace/` may be absent.

## Code Structure

| File | Role |
|------|------|
| `backend/cli.py` | CLI entry points: `ctf-solve`, `ctf-status`, `ctf-msg`, `ctf-bump` |
| `backend/agents/coordinator_loop.py` | shared coordinator event loop and operator API server |
| `backend/agents/coordinator_core.py` | scheduler, queue logic, runtime actions |
| `backend/agents/codex_coordinator.py` | Codex-backed coordinator |
| `backend/agents/swarm.py` | one challenge swarm, candidate handling, pause/resume handoff |
| `backend/agents/codex_solver.py` | Codex lane runtime |
| `backend/agents/gemini_solver.py` | Gemini lane runtime |
| `backend/agents/solver.py` | Claude/Pydantic lane runtime |
| `backend/ctfd.py` | challenge sync and flag submission |
| `backend/sandbox.py` | Docker sandbox lifecycle |
| `backend/operator_ui.py` | UI data assembly |
| `backend/static/` | browser UI |

## Models

Default lane set lives in `backend/models.py`.

Current default lineup includes:

- `codex/gpt-5.4`
- `codex/gpt-5.4-mini`
- `codex/gpt-5.3-codex`
- `codex/gpt-5.3-codex-spark`
- `gemini/gemini-2.5-flash`
- `gemini/gemini-2.5-flash-lite`
- `gemini/gemini-2.5-pro`

You can override the lineup at startup:

```bash
uv run ctf-solve \
  --models codex/gpt-5.4 \
  --models codex/gpt-5.4-mini \
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

## Configuration Notes

All runtime settings can come from CLI flags, `.env`, or `backend/config.py`.

Useful defaults:

| Setting | Default | Meaning |
|---------|---------|---------|
| `--max-challenges` | `10` | max active challenges |
| `container_memory_limit` | `16g` | per-container memory cap |
| `sandbox_image` | `ctf-sandbox` | Docker image name |
| `msg_port` | `9400` | operator UI / API port |

Authentication is expected through home auth:

- `codex`: `~/.codex/auth.json`
- `claude`: `~/.claude/.credentials.json`
- `gemini`: `~/.gemini/oauth_creds.json`

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
