---
description: Resolve a micro-task blocker in a Kronig experiment run, then resume run_tasks.sh
argument-hint: <run_dir>/blockers/<id>.md
model: claude-opus-5-5
---

You are the planner resolving one blocker: `$ARGUMENTS`.

## 1. Understand the cause
Read the blocker, the task's entry in `tasks.json`, and its `tasks/<id>.md` if it is an agent task. For a failed `run`, the blocker already contains the bundle state, simulator stderr, and validation report from `diagnose`. Read `notes/` from earlier tasks if they might explain it.
If the blocker does not let you identify the cause with confidence, read the full records it lists (combined/check logs, session transcript `logs/<id>.<n>.jsonl`, the bundle). Do not fix a cause you have only guessed.

## 2. Classify and fix

| Cause | Fix |
| --- | --- |
| Environment (simulator missing, wrong Python, tunnel down) | Fix it if it needs no install, credentials, or network approval (e.g. set `TCAD_PYTHON` for `orchestration/kronig`, restart the approved SSH tunnel). Otherwise NEEDS_HUMAN. |
| Spec rejected by `validate` / capability check | Correct the spec or the authoring task's instructions. If the physics is unsupported by the backend, stop and tell the user. Never approximate it. |
| Solver failure (non-convergence, timeout, oscillation) | Follow `skills/solver-recovery/SKILL.md`: change numerical strategy only (bias steps, mesh, initial guess, timeout), never scientific intent. Add an agent task or edit the spec; the gate re-opens because the approved content changed, so the user approves the revised spec. Keep the failed bundle. |
| Validation failure in a completed run | Do not hide it. Follow `skills/result-validation`; if a refinement is justified, plan it as new stages; otherwise carry the failure into the analysis as a caveat. |
| Agent task wrong or missing information | Correct `tasks/<id>.md` (and later tasks with the same flaw); add exact values from the digest, never guesses. |
| Task too big | Split into new ids in the gaps, each self-contained. |
| Needs Kronig source changes | Out of scope for an experiment run: tell the user and propose a separate development run. |

## 3. Archive and resume
`mv <blocker> <run_dir>/blockers/resolved/<id>.$(date +%s).md` and append a `## Resolution` section (cause, fix, files changed). If you rewrote or split the task, or fixed the environment for a command task, `rm -f <run_dir>/attempts/<id>`. Rerun `bash orchestration/run_tasks.sh <run_dir>` with `run_in_background: true` and handle its last line as in `/kronig-plan` step 4.

If the same task blocks again with the same cause, stop and present both blockers and resolutions to the user.
