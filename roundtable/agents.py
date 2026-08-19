"""AgentSpec: command building and session-id extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import AgentConfig


@dataclass
class BuiltCommand:
    """A fully substituted command line.

    ``stdin_prompt`` is the prompt to pipe to stdin when the configured args
    contain no ``{prompt}`` placeholder; otherwise ``None``.
    """

    argv: list[str]
    stdin_prompt: str | None = None


def _substitute(args: list[str], prompt: str, session: str | None) -> list[str]:
    out = []
    for arg in args:
        arg = arg.replace("{prompt}", prompt)
        if session is not None:
            arg = arg.replace("{session}", session)
        out.append(arg)
    return out


class AgentSpec:
    """Runtime view of one configured agent."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.name = config.name

    # -- round (non-interactive) commands ----------------------------------

    def build_round(
        self, prompt: str, session: str | None = None, readonly: bool = True
    ) -> BuiltCommand:
        """Build the non-interactive round command.

        Uses ``args_round_resume`` when a session id is known, otherwise
        ``args_round_first``. With ``readonly=True`` (discussion rounds) the
        configured ``readonly_args`` are appended; the write phase (relay)
        passes ``readonly=False`` so those restrictions do not apply.
        """
        if session and self.config.args_round_resume:
            args = self.config.args_round_resume
        else:
            args = self.config.args_round_first
        stdin_prompt = None if any("{prompt}" in a for a in args) else prompt
        argv = [self.config.command] + _substitute(args, prompt, session)
        if readonly and self.config.readonly_args:
            argv += self.config.readonly_args
        return BuiltCommand(argv=argv, stdin_prompt=stdin_prompt)

    # -- interactive (handover) commands ------------------------------------

    def build_interactive(
        self, prompt: str, session: str | None = None
    ) -> tuple[BuiltCommand, str | None]:
        """Build the interactive handover command.

        Returns ``(command, warning)``. When no ``args_interactive_*`` are
        configured, falls back to the ``args_round_*`` templates (the
        no-write constraint lives in the prompt text, not the argv, so the
        caller passes a write-allowed primer) and returns a warning string.
        """
        warning = None
        if session and self.config.args_interactive_resume:
            args = self.config.args_interactive_resume
        elif self.config.args_interactive_first:
            args = self.config.args_interactive_first
        else:
            warning = (
                f"agent {self.name!r} has no args_interactive_* — falling back "
                "to args_round_* semantics minus the no-write constraint"
            )
            if session and self.config.args_round_resume:
                args = self.config.args_round_resume
            else:
                args = self.config.args_round_first
        stdin_prompt = None if any("{prompt}" in a for a in args) else prompt
        argv = [self.config.command] + _substitute(args, prompt, session)
        return BuiltCommand(argv=argv, stdin_prompt=stdin_prompt), warning

    # -- session-id extraction ----------------------------------------------

    def extract_session(self, stdout: str) -> str | None:
        """Apply ``session_regex`` to *stdout*; return the captured id."""
        if not self.config.session_regex:
            return None
        match = re.search(self.config.session_regex, stdout)
        if match:
            return match.group(1)
        return None
