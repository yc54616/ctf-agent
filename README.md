# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM reads the live challenge feed and assigns work to **solver swarms**. Each swarm launches multiple AI models in parallel on the same challenge — the first to find the flag wins, and that result is shared with the rest via a cross-solver message bus.

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
     |  Gemini Flash   | |  Gemini Flash  | |                |
     |  Gemini Pro     | |  Gemini Pro    | |     ...        |
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
     | gdb, sage...    |  | gdb, sage...   |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers share discoveries through a message bus and never give up — they keep trying different approaches until the flag is found or the challenge is won by another lane.

### Key Components

| Component | File | Role |
|-----------|------|------|
| Coordinator loop | `backend/agents/coordinator_loop.py` | Main event loop; reads CTFd feed; drives the coordinator LLM |
| Claude coordinator | `backend/agents/claude_coordinator.py` | LLM orchestration via Claude Agent SDK MCP |
| Codex coordinator | `backend/agents/codex_coordinator.py` | LLM orchestration via Codex JSON-RPC |
| Challenge swarm | `backend/agents/swarm.py` | Manages parallel solver lanes for one challenge |
| Claude solver | `backend/agents/solver.py` | Pydantic AI agent (claude-sdk provider) |
| Codex solver | `backend/agents/codex_solver.py` | Codex JSON-RPC agent |
| Gemini solver | `backend/agents/gemini_solver.py` | Gemini CLI subprocess agent |
| Docker sandbox | `backend/sandbox.py` | Async container lifecycle (aiodocker) |
| CTFd client | `backend/ctfd.py` | Challenge fetching, flag submission, CSRF |
| Message bus | `backend/message_bus.py` | Cross-solver shared findings |
| Operator UI | `backend/operator_ui.py` | Browser console for live monitoring and guidance |

## Quick Start

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Override the Ubuntu apt mirror if needed (default: mirror.navercorp.com)
docker build --build-arg UBUNTU_MIRROR=http://mirror.kakao.com/ubuntu \
  -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Configure credentials
cp .env.example .env
# Edit .env with your CTFd URL and token

# Run against a CTFd instance
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v

# Open the browser operator console
uv run ctf-status

# Print a one-shot terminal snapshot
uv run ctf-status --once

# Legacy terminal dashboard (watch mode)
uv run ctf-status --text

# Show every lane in the terminal dashboard
uv run ctf-status --text --verbose

# Send a hint to the running coordinator
uv run ctf-msg "focus on web challenges"

# Send targeted guidance to a specific lane
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

| Model | Provider | Context | Notes |
|-------|----------|---------|-------|
| Gemini 2.5 Flash | Gemini CLI | 1M | Fast general-purpose solver |
| Gemini 2.5 Flash Lite | Gemini CLI | 1M | Cheapest high-parallelism lane |
| Gemini 2.5 Pro | Gemini CLI | 1M | Deep reasoning when quota is available |
| GPT-5.4 | Codex | 1M | Best overall solver; supports vision |
| GPT-5.4-mini | Codex | 400K | Fast, good for easy challenges |
| GPT-5.3-codex | Codex | 1M | Reasoning model (xhigh effort) |
| GPT-5.3-codex-spark | Codex | 128K | Ultra-fast exploratory lane |

Models are added or removed by editing `DEFAULT_MODELS` in `backend/models.py`.

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary / Rev** | radare2, GDB, gdb-multiarch, objdump, readelf, binwalk, strings, patchelf, checksec, qemu-user-static, qemu-system-* |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone, socat, tcpdump, tshark |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs, flatter, hashcat, john, yara |
| **Forensics / Filesystems** | volatility3, Sleuthkit, foremost, testdisk, squashfs-tools, mtd-utils, u-boot-tools, device-tree-compiler |
| **Stego / Media** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR, ffmpeg, sox, Pillow |
| **Web / Recon** | curl, nmap, ffuf, gobuster, sqlmap, whatweb, nikto, dirb, hydra, Python requests, flask |
| **Mobile / Android** | adb, fastboot, apktool, jadx |
| **Cloud / Containers** | podman, podman compose, buildah, skopeo, kubectl, helm |
| **Toolchains / Misc** | gcc, g++, cmake, make, node, npm, go, rustc, cargo, ripgrep, fd, jq, sqlite3, PyTorch |

Run the smoke check after building or updating the image:

```bash
docker run --rm ctf-sandbox sandbox-smoke-check
```

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously; first to find the flag wins
- **Coordinator LLM** — reads solver traces and crafts targeted technical guidance per lane
- **Cross-solver insights** — discoveries are shared between models via an in-memory message bus so no solver re-discovers the same dead end
- **Auto-spawn** — new challenges detected from the CTFd feed are automatically spawned into swarms
- **Cooldown gating** — escalating submission cooldowns per model after incorrect flags (0s → 30s → 2m → 5m → 10m) to prevent rate-limit bans
- **Operator messaging** — send hints to the coordinator or directly to a running lane mid-competition
- **Browser operator console** — live challenge status, per-lane traces, JSONL event browser, and inline controls
- **Artifact spooling** — large command output is saved to a shared directory and pointer-summarized to keep context windows clean
- **Cost tracking** — per-agent token and dollar accounting via `genai-prices`
- **Solve artifacts** — when a flag is confirmed, the winning lane's workspace, trace, and a draft writeup are saved automatically

## Configuration

Copy `.env.example` to `.env` and fill in your CTFd settings:

```bash
cp .env.example .env
```

```env
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token
```

All settings can also be passed as CLI flags or environment variables. See `backend/config.py` for the full list (`Settings` class).

### Authentication

Home auth is auto-detected by default:

- `codex` — `~/.codex/auth.json`
- `claude` — `~/.claude/.credentials.json`
- `gemini` — `~/.gemini/oauth_creds.json` (re-run `gemini` login if expired)

The repository does not embed Gemini OAuth client credentials. `gemini/*` model specs use Gemini CLI with an isolated temporary `GEMINI_HOME`. Legacy `google/*` specs are treated as the same Gemini backend alias.

Direct API providers (Azure, Bedrock, Zen) are not supported. If a lane hits quota or rate limits, that lane stops rather than retrying through an alternate endpoint.

### Infra defaults (overridable via env/CLI)

| Setting | Default | Description |
|---------|---------|-------------|
| `sandbox_image` | `ctf-sandbox` | Docker image name |
| `max_concurrent_challenges` | `10` | Max active swarms at once |
| `max_attempts_per_challenge` | `3` | Swarm restarts before giving up |
| `container_memory_limit` | `16g` | Per-sandbox memory cap |
| `exec_output_spill_threshold_bytes` | `65536` | Spool bash output above this size |
| `read_file_spill_threshold_bytes` | `262144` | Spool file reads above this size |

### Solve artifacts

When a lane confirms a real flag, the winner's artifacts are saved under `challenges/<challenge>/solve/`:

```
challenges/<challenge>/solve/
  flag.txt
  writeup.md      ← draft, not a final report
  result.json
  trace.jsonl
  workspace/
```

Shared outputs (intermediate files, findings) remain in `challenges/<challenge>/.shared-artifacts/` and are referenced from the saved writeup metadata.

## Requirements

- Python 3.14+
- Docker
- Home auth configured for at least one of `codex`, `claude`, or `gemini`
- `codex` CLI (for Codex solver/coordinator)
- `claude` CLI bundled with `claude-agent-sdk` (for Claude coordinator)
- `gemini` CLI if you want `gemini/*` models

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
