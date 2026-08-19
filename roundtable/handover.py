"""Interactive "phone handover" + git snapshot helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .agents import AgentSpec, BuiltCommand


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=str(cwd), timeout=60
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 127, ""
    return proc.returncode, proc.stdout


def is_git_repo(cwd: Path) -> bool:
    code, out = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    return code == 0 and out.strip() == "true"


def git_head(cwd: Path) -> str | None:
    """Current HEAD commit, or None (not a repo / no commits yet)."""
    code, out = _git(["rev-parse", "HEAD"], cwd)
    if code == 0:
        return out.strip()
    return None


def git_diff_stat(base: str, cwd: Path) -> str:
    # --no-ext-diff: always capture the canonical diff format, even when the
    # user's git config sets diff.external (e.g. difftastic).
    code, out = _git(["diff", "--no-ext-diff", "--stat", base], cwd)
    return out.strip() if code == 0 else ""


def git_diff(base: str, cwd: Path, max_chars: int) -> str:
    """Working-tree diff vs *base*, truncated to *max_chars*."""
    code, out = _git(["diff", "--no-ext-diff", base], cwd)
    if code != 0:
        return ""
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n[... diff truncated to {max_chars} chars ...]"
    return out


def hand_over(
    spec: AgentSpec,
    primer: str,
    session: str | None,
    cwd: Path,
) -> tuple[int, str | None]:
    """Spawn the agent's interactive CLI attached to the user's terminal.

    Returns ``(returncode, warning)``. Blocks until the session exits.
    """
    built, warning = spec.build_interactive(primer, session)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    _print_banner(spec.name, built)
    try:
        returncode = subprocess.call(built.argv, cwd=str(cwd))
    except FileNotFoundError:
        print(f"error: command not found: {built.argv[0]!r}", file=sys.stderr)
        returncode = 127
    print(f"\n[roundtable] session with {spec.name} ended (exit code {returncode}).")
    return returncode, warning


def _print_banner(agent_name: str, built: BuiltCommand) -> None:
    line = "=" * 72
    print(line)
    print(f"  You are now talking directly to {agent_name}.")
    print("  Exit the session to return to the coordinator.")
    print(line)
