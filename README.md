# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Claude/Codex)  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  Opus (med)     | |  Opus (med)    | |                |
     |  Opus (max)     | |  Opus (max)    | |     ...        |
     |  GPT-5.4        | |  GPT-5.4       | |                |
     |  GPT-5.4-mini   | |  GPT-5.4-mini  | |                |
     |  GPT-5.3-codex  | |  GPT-5.3-codex | |                |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

## Quick Start

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Configure credentials
cp .env.example .env
# Edit .env with your CTFd token

# Run against a CTFd instance
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v

# Open the browser operator console
uv run ctf-status

# Print one terminal snapshot instead of opening the browser
uv run ctf-status --once

# Keep the legacy terminal dashboard watch mode
uv run ctf-status --text

# Show every lane in the terminal dashboard
uv run ctf-status --text --verbose

# Send an operator message to the running coordinator
uv run ctf-msg "focus on web challenges"

# Send targeted guidance directly to a running lane
uv run ctf-bump --challenge "Midnight Roulette" --model "codex/gpt-5.4" \
  "Use the provided CTFd token and verify /ctfd/api/v1/challenges first"
```

## Coordinator Backends

```bash
# Claude SDK coordinator (default)
uv run ctf-solve --coordinator claude ...

# Codex coordinator (GPT-5.4-mini via JSON-RPC)
uv run ctf-solve --coordinator codex ...
```

## Solver Models

Default model lineup (configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| Gemini 2.5 Flash | Gemini CLI | Fast general-purpose solver |
| Gemini 2.5 Flash Lite | Gemini CLI | Cheapest high-parallelism lane |
| Gemini 2.5 Pro | Gemini CLI | Deep reasoning when quota is available |
| GPT-5.4 | Codex | Best overall solver |
| GPT-5.4-mini | Codex | Fast, good for easy challenges |
| GPT-5.3-codex | Codex | Reasoning model (xhigh effort) |
| GPT-5.3-codex-spark | Codex | Ultra-fast exploratory lane |

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary** | radare2, GDB, objdump, binwalk, strings, readelf |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| **Web** | curl, nmap, Python requests, flask |
| **Misc** | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Configuration

Copy `.env.example` to `.env` and fill in your CTFd settings:

```bash
cp .env.example .env
```

```env
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token
```

Home auth is auto-detected by default:

- `codex` from `~/.codex/auth.json`
- `claude` from `~/.claude/.credentials.json`
- `gemini` from `~/.gemini/oauth_creds.json` for `gemini/*` models via Gemini CLI

All settings can also be passed as environment variables or CLI flags. `gemini/*`
uses Gemini CLI with an isolated temporary `GEMINI_HOME`, and legacy `google/*`
specs are treated as the same Gemini backend alias. The repository does not embed
Gemini OAuth client credentials; if `~/.gemini/oauth_creds.json` expires or is
invalid, re-run `gemini` login locally.

Direct API providers are not supported. If a Codex, Claude, or Gemini lane hits
quota or rate limits, that lane stops instead of retrying through Azure,
Bedrock, or Zen.

Large command output is automatically spooled to `/challenge/shared-artifacts/`
with preview text returned to the model. Large `read_file` calls are pointer-first:
the model gets a preview plus a path and is expected to continue with targeted
`bash` inspection (`sed -n`, `tail`, `rg`, `strings`, `xxd`) instead of loading
entire blobs into context. The shared artifact directory is mounted into every
solver for the same challenge, so findings and logs can be referenced across
solvers by shared path.

`ctf-status` now opens a local browser operator console by default. It shows the
live challenge list, per-lane trace files, full JSONL event browsing, and inline
controls for coordinator messages plus lane/challenge bump fan-out. Use
`uv run ctf-status --text` for the legacy terminal dashboard, and
`uv run ctf-status --text --verbose` when you want every lane in that view.

When a lane confirms a real flag, the winner's artifacts are saved immediately
under `challenges/<challenge>/solve/`:

- `flag.txt`
- `writeup.md`
- `result.json`
- `trace.jsonl`
- `workspace/`

The writeup is a draft, not a final report. Shared outputs remain in
`challenges/<challenge>/.shared-artifacts/` and are referenced from the saved
writeup/result metadata.

## Requirements

- Python 3.14+
- Docker
- Home auth for `codex`, `claude`, and/or `gemini`
- `codex` CLI (for Codex solver/coordinator)
- `claude` CLI (bundled with claude-agent-sdk)
- `gemini` CLI if you want to run `gemini/*` models from `~/.gemini/oauth_creds.json`

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
