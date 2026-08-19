# Plan: Round-Table AI Coordinator CLI

## Goal
A script-driven coordinator (NOT a model) that orchestrates multiple CLI coding agents
(Codex, Claude Code, Gemini CLI, etc.) in a "round-table" discussion workflow, with the
human as an equally-weighted participant.

## Core workflow (from user)
1. Human gives input/question.
2. Coordinator invokes each agent in yolo mode BUT stresses: NO writes/changes — only
   comprehensive analysis/plans output as an MD file.
3. Coordinator presents the MD files to the human; human writes human.md (judgments/critics).
4. Coordinator feeds every agent the OTHER agents' MDs + human.md, still stressing no
   changes, collects round-2 MDs, shows them to the human.
5. Branch point:
   a) Repeat discussion (go to step 3), or
   b) Human picks one agent for WRITE work → single-agent phase. Ideally coordinator
      "hands over the phone": human talks directly to that agent's interactive session.
6. Human can call back to round-table: coordinator resumes all sessions, asks the other
   agents to review the changes made during the single-agent phase.

## Stages

### Stage 1 — Skill load & design
- Load `vibecoding-general-swarm` skill (mandatory for coding tasks).
- Design decisions:
  - Language: Python 3 CLI (stdlib + subprocess), no heavy deps.
  - Agents configured via a simple config file (name, command, yolo flags, resume flags).
  - State kept on disk: `.roundtable/` dir with rounds/round-N/<agent>.md, human.md,
    state.json (session ids per agent, current mode, round number).
  - Session continuity: agents' native resume/continue flags where available
    (claude `-c`, codex `resume`, gemini `--continue`), else re-inject transcript.
  - "Hand over the phone": launch the picked agent's interactive CLI attached to the
    user's terminal (optional tmux support); coordinator waits until human returns.
  - Diff-aware resume: on returning to round-table, coordinator runs `git diff` since
    the branch point and includes it in the prompt to non-picked agents.

### Stage 2 — Implementation (delegate to coder subagent)
- Build the CLI: commands `ask`, `rounds`/`status`, `continue` (next round),
  `pick <agent>` (enter write phase / hand over phone), `table` (back to round-table),
  `init` (config scaffolding), plus a mock-agent mode for testing.
- Deliver with README + example config.

### Stage 3 — Validation (delegate to verifier)
- Dry-run end-to-end with mock agent CLIs: round 1 → human.md → round 2 → pick →
  write phase → back to table with diff injection.
- Check state machine correctness, file layout, resume logic.

### Stage 4 — Deliver
- Package in /mnt/agents/output/roundtable/ with README.
