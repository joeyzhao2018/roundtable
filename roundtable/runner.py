"""Parallel subprocess execution, MD capture, fallback handling."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .agents import AgentSpec

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def extract_markdown(stdout: str) -> str:
    """Best-effort extraction of the agent's prose from machine-formatted
    stdout, for the stdout fallback path.

    Agents enforced read-only at the CLI level cannot write their answer
    file, so their stdout becomes the answer. When that stdout is JSON we
    unwrap it rather than saving the raw envelope:

    - a single JSON object with a string ``result`` field (claude
      ``--output-format json``) → that field;
    - JSONL with ``{"role": "assistant", "content": "..."}`` messages
      (kimi ``--output-format stream-json``) → concatenated contents;
    - anything else → the input unchanged.
    """
    text = stdout.strip()
    if not text or text[0] not in "[{":
        return stdout
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        return obj["result"]
    parts = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("role") == "assistant" \
                and isinstance(obj.get("content"), str):
            parts.append(obj["content"])
    if parts:
        return "\n\n".join(parts)
    return stdout


@dataclass
class AgentRunResult:
    name: str
    exit_code: int | None = None
    duration_s: float = 0.0
    session_id: str | None = None
    source: str | None = None  # "file" | "stdout" | None
    timed_out: bool = False
    error: str | None = None
    cmd: list[str] = field(default_factory=list)
    output_path: str | None = None

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and self.error is None
            and self.exit_code == 0
            and self.source is not None
        )

    def meta_entry(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "session_id": self.session_id,
            "source": self.source,
            "timed_out": self.timed_out,
            "error": self.error,
            "cmd": self.cmd,
            "output_path": self.output_path,
        }


def _to_text(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def run_agent(
    spec: AgentSpec,
    prompt: str,
    answer_path: Path,
    session: str | None,
    timeout: int,
    cwd: Path,
    readonly: bool = True,
) -> AgentRunResult:
    """Run one agent non-interactively and capture its markdown answer."""
    built = spec.build_round(prompt, session, readonly=readonly)
    result = AgentRunResult(name=spec.name, cmd=built.argv)
    stdout = ""
    stderr = ""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            built.argv,
            input=built.stdin_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
        )
        result.exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        result.timed_out = True
        result.error = f"timed out after {timeout}s"
        stdout = _to_text(exc.stdout) if exc.stdout is not None else _to_text(exc.output)
        stderr = _to_text(exc.stderr)
    except FileNotFoundError:
        result.error = f"command not found: {built.argv[0]!r}"
    except OSError as exc:
        result.error = f"failed to spawn {built.argv[0]!r}: {exc}"
    result.duration_s = time.monotonic() - start

    result.session_id = spec.extract_session(stdout)

    # MD capture priority: (1) file at instructed answer path if non-empty
    # after exit; (2) else stdout (ANSI-stripped, JSON envelopes unwrapped)
    # written to that path.
    existing = ""
    if answer_path.is_file():
        existing = answer_path.read_text(encoding="utf-8", errors="replace")
    if existing.strip():
        result.source = "file"
    else:
        text = extract_markdown(strip_ansi(stdout))
        if text.strip():
            answer_path.parent.mkdir(parents=True, exist_ok=True)
            answer_path.write_text(text, encoding="utf-8")
            result.source = "stdout"
        else:
            result.source = None

    if result.exit_code not in (0, None) and result.error is None:
        result.error = f"exited with code {result.exit_code}"
    if stderr.strip() and result.error:
        result.error += f"; stderr: {strip_ansi(stderr).strip()[:500]}"
    if result.source:
        result.output_path = str(answer_path)
    return result


def run_round(
    specs: list[AgentSpec],
    prompts: dict[str, str],
    answer_paths: dict[str, Path],
    sessions: dict[str, str],
    timeout: int,
    cwd: Path,
    readonly: bool = True,
) -> dict[str, AgentRunResult]:
    """Run all agents in parallel; never raises for individual failures.

    ``readonly=False`` (the relayed write phase) omits each agent's
    configured ``readonly_args`` from the resolved argv.
    """
    results: dict[str, AgentRunResult] = {}

    def _one(spec: AgentSpec) -> AgentRunResult:
        return run_agent(
            spec,
            prompts[spec.name],
            answer_paths[spec.name],
            sessions.get(spec.name),
            timeout,
            cwd,
            readonly=readonly,
        )

    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as pool:
        for spec, result in zip(specs, pool.map(_one, specs)):
            results[spec.name] = result
    return results


def format_cmd(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
