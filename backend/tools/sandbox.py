"""Pydantic AI tool wrappers — thin delegation to backend.tools.core."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.tools.core import (
    do_bash,
    do_fs_query,
)


async def bash(ctx: RunContext[SolverDeps], command: str, timeout_seconds: int = 60) -> str:
    """Execute a bash command inside the sandboxed Docker container.

    Distfiles are at /challenge/distfiles/ (read-only).
    Write generated/repaired files to /challenge/workspace/ (writable).
    Large stdout/stderr is automatically saved under /challenge/shared-artifacts/
    and this tool returns a preview plus the saved path.
    Challenge services are reachable via host.docker.internal.
    Run `cat /tools.txt` to see all installed tools. The image is intentionally
    headless-only; prefer CLI entrypoints such as `ghidra-headless`, `bpftool`,
    `bpftrace`, `httpx`, `subfinder`, `amass`, `certipy`, `nxc`,
    `impacket-*`, `nuclei`, `feroxbuster`, `smali`, `dex2jar`, `jefferson`,
    `forge`, `slither`, and `sage`. Bundled wordlists live under
    `/opt/wordlists/seclists` and `/opt/wordlists/assetnote`.
    """
    return await do_bash(ctx.deps.sandbox, command, timeout_seconds)


async def fs_query(
    ctx: RunContext[SolverDeps],
    action: str,
    path: str,
    maxdepth: int = 3,
    kind: str = "files",
    pattern: str = "",
    limit: int = 200,
    mode: str = "text",
    start_line: int = 1,
    line_count: int = 120,
    byte_offset: int = 0,
    byte_count: int = 256,
    query: str = "",
    glob: str = "",
    ignore_case: bool = True,
    context_lines: int = 2,
) -> str:
    """Bounded read-only filesystem inspection.

    Use this instead of broad shell pipelines when you need a quick preview or a
    token-efficient artifact pointer. Supported actions are `find`, `peek`,
    `search`, `inspect`, and `archive_list`.
    """
    return await do_fs_query(
        ctx.deps.sandbox,
        action=action,
        path=path,
        maxdepth=maxdepth,
        kind=kind,
        pattern=pattern,
        limit=limit,
        mode=mode,
        start_line=start_line,
        line_count=line_count,
        byte_offset=byte_offset,
        byte_count=byte_count,
        query=query,
        glob=glob,
        ignore_case=ignore_case,
        context_lines=context_lines,
    )


async def report_flag_candidate(
    ctx: RunContext[SolverDeps],
    flag: str,
    evidence: str = "",
    confidence: str = "medium",
) -> str:
    """Run the guarded candidate path.

    In CTFd mode this applies simple placeholder guardrails and then submits the
    candidate remotely. In local / --no-submit mode it queues the candidate for
    operator confirmation instead.
    """
    if not ctx.deps.report_flag_candidate_fn:
        return "No candidate reporter connected."
    runtime = ctx.deps.runtime_status_getter() if ctx.deps.runtime_status_getter else {}
    step_count = 0
    if isinstance(runtime, dict):
        raw_step_count = runtime.get("step_count", 0)
        if isinstance(raw_step_count, int):
            step_count = raw_step_count
        elif isinstance(raw_step_count, str):
            try:
                step_count = int(raw_step_count)
            except ValueError:
                step_count = 0
    return await ctx.deps.report_flag_candidate_fn(
        flag.strip(),
        evidence,
        confidence,
        step_count,
        ctx.deps.trace_path,
    )


async def notify_coordinator(ctx: RunContext[SolverDeps], message: str) -> str:
    """Send a message to the coordinator about a strategic discovery or request.

    Use this when you find something that affects the overall competition strategy,
    like discovering a flag format pattern, a shared vulnerability across challenges,
    or when you need help from other solvers.
    """
    if ctx.deps.notify_coordinator:
        try:
            await ctx.deps.notify_coordinator(message)
            return "Message sent to coordinator."
        except Exception as e:
            return f"Notification failed: {e}"
    return "No coordinator connected."
