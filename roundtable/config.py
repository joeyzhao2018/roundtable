"""TOML config load / validate / scaffold for roundtable.

The config file is ``roundtable.toml`` in the target project root.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = "roundtable.toml"

#: Placeholders allowed inside agent ``args_*`` lists.
ALLOWED_PLACEHOLDERS = {"prompt", "session"}

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

SCAFFOLD_TEMPLATE = '''\
# roundtable.toml — configuration for the roundtable coordinator.
#
# Each agent is a CLI coding tool. Command templates are argument lists;
# placeholders:
#   {prompt}  — substituted with the full prompt text (a single list arg).
#               If {prompt} is absent from the args, the prompt is piped to
#               the process' stdin instead.
#   {session} — session id from the previous round (resume), if known.
# session_regex: optional regex with ONE capture group, applied to stdout,
# to learn the agent's session id for later resume.

[settings]
state_dir = ".roundtable"      # relative to project root
max_diff_chars = 60000         # truncate git diff injected into review prompts
timeout_seconds = 1800         # per-agent round timeout
editor = ""                    # override $EDITOR

# --- Example agents (uncomment and adapt to your installed CLIs) -----------

# [agents.claude]
# command = "claude"
# args_round_first  = ["-p", "{prompt}", "--output-format", "json"]
# args_round_resume = ["--resume", "{session}", "-p", "{prompt}", "--output-format", "json"]
# args_interactive_first  = ["{prompt}"]
# args_interactive_resume = ["--resume", "{session}", "{prompt}"]
# session_regex = '"session_id"\\s*:\\s*"([^"]+)"'
# readonly_args: extra argv appended to discussion-round invocations ONLY
# (never to the interactive/relay write phase) — enforce the no-write rule
# at the CLI level when the agent supports it:
# readonly_args = ["--permission-mode", "plan", "--disallowed-tools", "Write", "Edit", "NotebookEdit"]

# [agents.codex]
# command = "codex"
# args_round_first  = ["exec", "--skip-git-repo-check", "{prompt}"]
# args_round_resume = ["exec", "resume", "--skip-git-repo-check", "{session}", "{prompt}"]
# args_interactive_first  = ["{prompt}"]
# args_interactive_resume = ["resume", "{session}", "{prompt}"]
# session_regex = 'session id:\\s*(\\S+)'
# # "codex exec resume" rejects --sandbox; use the -c config override form.
# readonly_args = ["-c", 'sandbox_mode="read-only"']

# [agents.kimi]
# command = "kimi"
# args_round_first  = ["-p", "{prompt}", "--output-format", "stream-json"]
# args_round_resume = ["-r", "{session}", "-p", "{prompt}", "--output-format", "stream-json"]
# # kimi's interactive TUI takes no positional initial prompt, so these
# # "interactive" entries are one-shot -p runs (equivalent to pick --relay).
# args_interactive_first  = ["-p", "{prompt}"]
# args_interactive_resume = ["-r", "{session}", "-p", "{prompt}"]
# session_regex = '"session_id":"([^"]+)"'
# # No readonly_args available: kimi's --plan conflicts with -p, so the
# # no-write rule is prompt-only for this agent.
'''


class ConfigError(Exception):
    """Raised for any configuration problem (message is user-facing)."""


@dataclass
class AgentConfig:
    name: str
    command: str
    args_round_first: list[str]
    args_round_resume: list[str] | None = None
    args_interactive_first: list[str] | None = None
    args_interactive_resume: list[str] | None = None
    session_regex: str | None = None
    # Appended to discussion-round (no-write) invocations only — never to
    # the interactive or relayed write phase.
    readonly_args: list[str] | None = None


@dataclass
class Settings:
    state_dir: str = ".roundtable"
    max_diff_chars: int = 60000
    timeout_seconds: int = 1800
    editor: str = ""


@dataclass
class Config:
    project_root: Path
    settings: Settings
    agents: dict[str, AgentConfig] = field(default_factory=dict)


def _check_placeholders(agent: str, key: str, args: list[str]) -> None:
    for arg in args:
        for match in _PLACEHOLDER_RE.finditer(arg):
            if match.group(1) not in ALLOWED_PLACEHOLDERS:
                raise ConfigError(
                    f"agent {agent!r}, key {key!r}: unknown placeholder "
                    f"{{{match.group(1)}}} in {arg!r}; allowed placeholders: "
                    + ", ".join(sorted("{%s}" % p for p in ALLOWED_PLACEHOLDERS))
                )


def _require_str_list(agent: str, key: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"agent {agent!r}: {key!r} must be a list of strings")
    return list(value)


def load_config(project_root: str | Path) -> Config:
    """Load and validate ``roundtable.toml`` under *project_root*."""
    root = Path(project_root).resolve()
    path = root / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(
            f"no {CONFIG_FILENAME} found in {root} — run `roundtable init` first"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML parse error: {exc}") from exc

    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ConfigError("[settings] must be a table")
    settings = Settings(
        state_dir=str(raw_settings.get("state_dir", Settings.state_dir)),
        max_diff_chars=int(raw_settings.get("max_diff_chars", Settings.max_diff_chars)),
        timeout_seconds=int(
            raw_settings.get("timeout_seconds", Settings.timeout_seconds)
        ),
        editor=str(raw_settings.get("editor", Settings.editor)),
    )

    raw_agents = data.get("agents", {})
    if not isinstance(raw_agents, dict) or not raw_agents:
        raise ConfigError(
            f"{path}: at least one agent is required — add an [agents.<name>] table"
        )

    agents: dict[str, AgentConfig] = {}
    for name, spec in raw_agents.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"[agents.{name}] must be a table")
        command = spec.get("command")
        if not command or not isinstance(command, str):
            raise ConfigError(f"agent {name!r}: 'command' (string) is required")
        if "args_round_first" not in spec:
            raise ConfigError(f"agent {name!r}: 'args_round_first' is required")

        kwargs: dict = {}
        for key in (
            "args_round_first",
            "args_round_resume",
            "args_interactive_first",
            "args_interactive_resume",
            "readonly_args",
        ):
            if key in spec and spec[key] is not None:
                kwargs[key] = _require_str_list(name, key, spec[key])
                _check_placeholders(name, key, kwargs[key])

        session_regex = spec.get("session_regex")
        if session_regex is not None:
            if not isinstance(session_regex, str):
                raise ConfigError(f"agent {name!r}: 'session_regex' must be a string")
            try:
                compiled = re.compile(session_regex)
            except re.error as exc:
                raise ConfigError(
                    f"agent {name!r}: invalid session_regex: {exc}"
                ) from exc
            if compiled.groups < 1:
                raise ConfigError(
                    f"agent {name!r}: session_regex must contain one capture group"
                )

        agents[name] = AgentConfig(
            name=name, command=command, session_regex=session_regex, **kwargs
        )

    return Config(project_root=root, settings=settings, agents=agents)


def config_warnings(config: Config) -> list[str]:
    """Non-fatal warnings about the configuration."""
    warnings: list[str] = []
    if len(config.agents) == 1:
        warnings.append(
            "only one agent configured — a round-table works best with two or more"
        )
    for agent in config.agents.values():
        if agent.args_interactive_first is None and agent.args_interactive_resume is None:
            warnings.append(
                f"agent {agent.name!r} has no args_interactive_* — `pick` will fall "
                "back to args_round_* semantics minus the no-write constraint"
            )
    return warnings


def scaffold(project_root: str | Path, force: bool = False) -> Path:
    """Write the scaffold config; refuse to overwrite unless *force*."""
    root = Path(project_root).resolve()
    path = root / CONFIG_FILENAME
    if path.exists() and not force:
        raise ConfigError(
            f"{path} already exists — use --force to overwrite"
        )
    path.write_text(SCAFFOLD_TEMPLATE, encoding="utf-8")
    return path
