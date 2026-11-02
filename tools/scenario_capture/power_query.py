"""Read current power settings across power-shell and system-service CLIs."""
from __future__ import annotations

import re


POWER_QUERY_COMMANDS = ("power-shell dump -s", "hidumper -s 3301 -a -a")
_UNSUPPORTED_QUERY = re.compile(
    r"(?im)^\s*(?:usage\s*:|(?:error\s*:\s*)?(?:unknown|unsupported|invalid)\s+"
    r"(?:command|option|argument)\b|(?:command|option)\b[^\n]*\b(?:not supported|unsupported)\b|"
    r"(?:/[^\s:]+:\s*)?(?:power-shell|hidumper):\s*(?:not found|unknown command)\b)"
)


def query_power_compat(run, parse, valid, *, unavailable_reason, output_limit=2000,
                       capability_cache=None):
    """Try the service dump only when the preferred query has no usable value.

    The caller owns command deadlines, cancellation and observation records.
    Parse the complete response before bounding the retained diagnostic text.
    An optional run-local cache remembers interface support, never power state.
    Transport failures and unrecognized state remain retryable.
    """
    commands = list(POWER_QUERY_COMMANDS)
    if capability_cache is not None:
        unsupported = capability_cache.get("power_query_unsupported_commands", [])
        commands = [command for command in commands if command not in unsupported]
        preferred = capability_cache.get("power_query_preferred_command")
        if preferred in commands:
            commands.remove(preferred)
            commands.insert(0, preferred)
    value = None
    observation = {"status": "unavailable", "output": "", "cached_unsupported": True,
                   "reason": "No supported power query interface is available in this run"}
    for command in commands:
        observation = run(command)
        output = observation.get("output", "") or ""
        try:
            value = parse(output) if observation["status"] == "completed" else None
            if valid(value):
                if capability_cache is not None:
                    capability_cache["power_query_preferred_command"] = command
                return observation, value
            if observation["status"] == "completed":
                if capability_cache is not None and _UNSUPPORTED_QUERY.search(output):
                    unsupported = capability_cache.setdefault("power_query_unsupported_commands", [])
                    if command not in unsupported:
                        unsupported.append(command)
                observation.update(status="unavailable", reason=unavailable_reason)
        finally:
            observation["output"] = output[:output_limit]
    return observation, value
