"""Per-tool-call JSONL event tracing — one file per solver, streamable via tail -f."""

from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path


def _sanitize(s: str) -> str:
    return s.replace("/", "_").replace(" ", "_")


class SolverTracer:
    """Append-only JSONL event tracer. Flushes every write for tail -f streaming."""

    def __init__(self, challenge_name: str, model_id: str, log_dir: str = "logs") -> None:
        log_dir = os.environ.get("CTF_AGENT_LOG_DIR", log_dir)
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        trace_path = Path(log_dir) / f"trace-{_sanitize(challenge_name)}-{_sanitize(model_id)}-{ts}.jsonl"
        self.path = str(trace_path)
        self.rpc_path = str(trace_path.with_name(f"{trace_path.stem}-rpc.jsonl"))
        self._trace_rpc = os.environ.get("CTF_AGENT_TRACE_RPC", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._fh = open(self.path, "a")
        self._rpc_fh = open(self.rpc_path, "a") if self._trace_rpc else None
        atexit.register(self._close)

    def close(self) -> None:
        """Explicitly close the trace file. Safe to call multiple times."""
        if not self._fh.closed:
            try:
                self._fh.close()
            except Exception:
                pass
        if self._rpc_fh is not None and not self._rpc_fh.closed:
            try:
                self._rpc_fh.close()
            except Exception:
                pass

    _close = close  # atexit compat

    def _write(self, event: dict) -> None:
        try:
            self._fh.write(json.dumps({"ts": time.time(), **event}) + "\n")
            self._fh.flush()
        except Exception:
            pass

    def tool_call(self, tool_name: str, args: dict | str, step: int) -> None:
        args_str = args if isinstance(args, str) else json.dumps(args)
        self._write({"type": "tool_call", "tool": tool_name, "args": args_str[:2000], "step": step})

    def tool_result(self, tool_name: str, result: str, step: int) -> None:
        self._write({"type": "tool_result", "tool": tool_name, "result": result[:2000], "step": step})

    def model_response(self, text: str, step: int, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._write({"type": "model_response", "text": text[:1000], "step": step,
                      "input_tokens": input_tokens, "output_tokens": output_tokens})

    def usage(self, input_tokens: int, output_tokens: int, cache_read: int, cost_usd: float) -> None:
        self._write({"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cache_read_tokens": cache_read, "cost_usd": round(cost_usd, 6)})

    def event(self, kind: str, **kwargs) -> None:
        self._write({"type": kind, **kwargs})

    def rpc_message(self, direction: str, payload: dict | list | str | int | float | bool | None) -> None:
        if self._rpc_fh is None:
            return
        try:
            self._rpc_fh.write(json.dumps({"ts": time.time(), "direction": direction, "payload": payload}) + "\n")
            self._rpc_fh.flush()
        except Exception:
            pass
