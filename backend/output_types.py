"""Structured output types for solver agents."""

from pydantic import BaseModel


class FlagCandidate(BaseModel):
    flag: str
    method: str  # brief description of how


def solver_output_json_schema() -> dict:
    """JSON schema for solver structured output — shared by Claude SDK and Codex.

    Solvers emit a structured candidate when they believe they have a flag.
    The swarm verifies candidates asynchronously and keeps the lane exploring.
    """
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["flag_candidate"]},
            "flag": {"type": "string"},
            "method": {"type": "string"},
        },
        "required": ["type", "flag", "method"],
        "additionalProperties": False,
    }
