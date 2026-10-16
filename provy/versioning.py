"""What changed before it broke: the version of the instructions and the model on every agent step (argus#1735).

Provy can name a change as a cause only when the steps say which instructions and which model they ran with. On production 0 of 10,614 spans carried a
`prompt_version` and 4% a `model`, because nothing asked for them and nothing said they were missing. This module does both:

* `prompt_hash(text)` turns a system prompt into a short, stable version label, so a developer sends their prompt once and never invents a version string.
* `VersionCoverage` counts what a run sent and says, at the end, which steps went without. It reports; it never refuses to send and never changes a span.

⛔ A STEP THAT RUNS NO MODEL HAS NO `model`, AND IS NOT COUNTED AS MISSING ONE. A tool call and a step of plain code are not model calls. Only an agent step that
reported tokens is expected to name its model, and only an agent step is expected to carry a prompt version.
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading

# The step types that are an agent reasoning or deciding. Mirrors the ingest contract's reasoned-about agent steps.
VERSIONED_STEPS = frozenset({"agent_message", "decision", "llm_call"})


def prompt_hash(text: str) -> str:
    """A short, stable label for a prompt: `p-` and ten hex characters of its SHA-256.

    Whitespace is collapsed first, so re-indenting a prompt does not read as a new version. Any other edit does.
    """
    normalised = " ".join(str(text).split())
    return "p-" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:10]


def default_prompt_version() -> str | None:
    """A fleet-wide version from the environment, for an agent that registers none of its own."""
    v = os.environ.get("PROVY_PROMPT_VERSION", "").strip()
    return v or None


def _quiet() -> bool:
    return os.environ.get("PROVY_QUIET", "").strip().lower() in ("1", "true", "yes", "on")


class VersionCoverage:
    """Counts agent steps sent with and without a prompt version, and model steps sent with and without a model name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.agent_steps = 0
        self.no_prompt_version = 0
        self.model_steps = 0
        self.no_model = 0
        self._said = False

    def note(self, step_type: str | None, has_prompt_version: bool, has_model: bool, has_tokens: bool) -> None:
        if step_type not in VERSIONED_STEPS:
            return
        with self._lock:
            self.agent_steps += 1
            if not has_prompt_version:
                self.no_prompt_version += 1
            if has_tokens:
                self.model_steps += 1
                if not has_model:
                    self.no_model += 1

    def report(self) -> dict:
        with self._lock:
            return {"agent_steps": self.agent_steps, "no_prompt_version": self.no_prompt_version,
                    "model_steps": self.model_steps, "no_model": self.no_model}

    def lines(self) -> list[str]:
        r = self.report()
        out: list[str] = []
        if r["no_prompt_version"]:
            out.append(
                f"provy: {r['no_prompt_version']} of {r['agent_steps']} agent step(s) carried no prompt_version, so Provy cannot say what changed before a failure. "
                "Call client.set_prompt(agent, system_prompt) once per agent, or set PROVY_PROMPT_VERSION.")
        if r["no_model"]:
            out.append(
                f"provy: {r['no_model']} of {r['model_steps']} model step(s) carried no model name. Pass model= on the step.")
        return out

    def say(self) -> None:
        """Print the gaps once, to stderr, unless PROVY_QUIET is set. Silence about a gap is how it stayed at 0% for a month."""
        with self._lock:
            if self._said:
                return
            self._said = True
        if _quiet():
            return
        for line in self.lines():
            print(line, file=sys.stderr)
