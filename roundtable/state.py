"""StateStore: state.json + round dirs + md collection + lock file."""

from __future__ import annotations

import json
import os
import random
import string
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
STATE_FILENAME = "state.json"
LOCK_FILENAME = "LOCK"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_state() -> dict:
    return {
        "version": STATE_VERSION,
        "mode": "discuss",
        "round": 0,
        "picked_agent": None,
        "sessions": {},
        "labels": {},
        "write_phase": None,
        "history": [],
    }


def new_labels(agent_names: list[str]) -> dict[str, str]:
    """Fresh anonymized peer labels for a new discussion thread.

    Maps every participant — each agent plus ``"human"`` — to a shuffled
    "Peer A"/"Peer B"/... label, so no participant (including the human)
    is identifiable by name inside the deliberation prompts.
    """
    participants = list(agent_names) + ["human"]
    labels = [f"Peer {c}" for c in string.ascii_uppercase[: len(participants)]]
    random.SystemRandom().shuffle(labels)
    return dict(zip(participants, labels))


class StateStore:
    """Owns everything under ``<project>/<state_dir>``."""

    def __init__(self, project_root: str | Path, state_dir: str):
        self.root = Path(project_root).resolve()
        self.dir = self.root / state_dir
        self.state_path = self.dir / STATE_FILENAME

    # -- directories ---------------------------------------------------------

    def ensure_dirs(self) -> None:
        (self.dir / "rounds").mkdir(parents=True, exist_ok=True)

    def round_dir(self, round_no: int) -> Path:
        return self.dir / "rounds" / f"round-{round_no:03d}"

    # -- state.json ------------------------------------------------------------

    def load(self) -> dict:
        if not self.state_path.is_file():
            return initial_state()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        base = initial_state()
        base.update(state)
        return base

    def save(self, state: dict) -> None:
        self.ensure_dirs()
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.state_path)

    def add_history(self, state: dict, event: str, detail: str) -> None:
        state["history"].append({"ts": _now_iso(), "event": event, "detail": detail})

    # -- round files -----------------------------------------------------------

    def write_question(self, round_no: int, question: str) -> Path:
        path = self.round_dir(round_no) / "QUESTION.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Question\n\n{question}\n", encoding="utf-8")
        return path

    def read_question(self) -> str:
        """The question of the current discussion thread (round-001)."""
        path = self.round_dir(1) / "QUESTION.md"
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
        return text.removeprefix("# Question").strip()

    def agent_md_path(self, round_no: int, agent_name: str) -> Path:
        return self.round_dir(round_no) / f"{agent_name}.md"

    def human_md_path(self, round_no: int) -> Path:
        return self.round_dir(round_no) / "human.md"

    def meta_path(self, round_no: int) -> Path:
        return self.round_dir(round_no) / "_meta.json"

    def read_meta(self, round_no: int) -> dict:
        """Existing _meta.json for the round, or {} if absent/unreadable."""
        path = self.meta_path(round_no)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_meta(self, round_no: int, meta: dict) -> None:
        path = self.meta_path(round_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def read_md(self, path: Path) -> str:
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return ""

    def collect_agent_mds(self, round_no: int, agent_names: list[str]) -> dict[str, str]:
        """Map of agent name -> markdown text for agents with output."""
        out: dict[str, str] = {}
        for name in agent_names:
            text = self.read_md(self.agent_md_path(round_no, name))
            if text.strip():
                out[name] = text
        return out

    # -- lock --------------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.dir / LOCK_FILENAME

    def acquire_lock(self, force: bool = False) -> None:
        if self.lock_path.exists() and not force:
            raise LockError(
                f"{self.lock_path} exists — another roundtable run may be active. "
                "If it is stale, re-run with --force."
            )
        self.ensure_dirs()
        self.lock_path.write_text(
            f"pid={os.getpid()} ts={_now_iso()}\n", encoding="utf-8"
        )

    def release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


class LockError(Exception):
    """Raised when the .roundtable/LOCK file blocks a round."""
