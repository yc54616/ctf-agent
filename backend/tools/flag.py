"""Flag submission tool."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.tools.core import do_submit_flag


async def submit_flag(ctx: RunContext[SolverDeps], flag: str) -> str:
    """Submit a flag to the active remote platform to verify it directly.

    Returns CORRECT, ALREADY SOLVED, or INCORRECT.
    Do NOT submit placeholder flags like CTF{flag} or CTF{placeholder}.
    """
    if ctx.deps.no_submit:
        if ctx.deps.local_mode:
            return (
                f'LOCAL MODE — not submitting "{flag.strip()}" because remote submission is disabled. '
                "Queue it as a candidate and use operator approval if you want to mark it solved."
            )
        return (
            f'SUBMISSION DISABLED — not submitting "{flag.strip()}" because --no-submit is set. '
            "Keep investigating or submit it manually outside the agent if needed."
        )

    # Use deduped submission via swarm if available, otherwise direct remote submit
    if ctx.deps.submit_fn:
        display, is_confirmed = await ctx.deps.submit_fn(flag)
    else:
        display, is_confirmed = await do_submit_flag(ctx.deps.ctfd, ctx.deps.challenge_name, flag)
    if is_confirmed:
        ctx.deps.confirmed_flag = flag.strip()
    return display
