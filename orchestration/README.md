# Experiment micro-tasks

Opus 5.5 makes one judgement: the experiment matrix (`plan.yaml`). The Kronig CLI and Sonnet 5.5 do the rest. You answer questions and approve runs.

```text
/kronig-plan <results> -- <goal>                                   (Opus session)
 1 kronig plan prepare        CLI     run dir, lossless flat digest, Kronig context pack, planning tasks
 2 plan.yaml                  OPUS    candidates × Kronig material × base example × intent/targets × backend
 3 planning run  (<run>/plan)
   p10-check-plan             CLI     schema + feasibility (backend state, reviewed material, example)  → NEEDS_HUMAN if not
   p20-requirements           Sonnet  every spec input with its source; lab values re-verified against the raw file
   p30-questions              CLI     missing/conflicting inputs → questions.md → NEEDS_HUMAN → you answer
   p40-render                 CLI     experiment tasks.json + tasks/*.md from fixed templates
 4 experiment run (<run>), per experiment
   x20-spec                   Sonnet  ExperimentSpec; check = kronig validate + every required value present
   x30-validate, x40-compile  Kronig CLI
   x50-approve                YOU     exact spec + compile output, sha256-bound
   x60-run                    Kronig CLI → evidence bundle (failure diagnostics go into the blocker)
   x70-analyse                Sonnet  metrics from canonical fields; check = fields exist, validation matches bundle
   x80-report                 Kronig CLI
   z90-compare                Sonnet  cross-experiment summary (when compare: true)
 blockers → /kronig-unblock (Opus) → fix → re-render if needed → resume
```

Tested 2026-11-11: an infeasible GaN plan was stopped by p10 with no model call. A silicon plan ran p20 (Sonnet, $0.17), which flagged the reverse-bias limit and step as missing instead of guessing; p30 turned them into questions. The x20 spec author (Sonnet) blocked rather than override a sign convention it found in Kronig's code. After the fix it went on through validate → compile to the approval gate.

## Use

```
/kronig-plan ../semiconductor_lab/data/golden_loop_GaN.json -- Compare GaN vs silicon as a 1D pn-junction at 300 K: reverse leakage
```
Manual: `orchestration/kronig plan prepare <slug> <results> --goal "…" --cli orchestration/kronig`, write `plan.yaml`, then `bash orchestration/run_tasks.sh <run>/plan` and `bash orchestration/run_tasks.sh <run>`.

Planning is part of the Kronig CLI (`tcad-agent plan …`, code in `src/tcad_agent/planning/`, tests in `tests/unit/test_planning.py`):
`digest`, `prepare`, `check-plan`, `check-requirements`, `questions`, `render`, `check-spec`, `check-results`.
`orchestration/kronig` runs that CLI from this checkout (`orchestration/kpy` picks the Python; `TCAD_PYTHON` overrides it; DEVSIM must be importable to run). `MICROTASKS_DIR` or `--runs-root` changes where runs are created.

## Task types (`tasks.json`)

| Type | Fields | Model call |
| --- | --- | --- |
| `agent` | `check` (required), `model` (one id, or a ladder tried in order when the check fails), `tools`, `allowed`, `max_turns`, `max_budget_usd`, `add_dirs`; prompt in `tasks/<id>.md` | Sonnet (or `model`) |
| `command` | `cmd` (required), optional `check`, `diagnose` (runs on failure; output goes into the blocker), `on_fail: human` | none |
| `gate` | `show` (what to approve); description in `tasks/<id>.md` | none |

Outputs flow by reference: `{{out:<id>}}` is a command task's trimmed stdout (e.g. a bundle path); `{{outpath:<id>}}` is its stdout file; `{{out:self}}` works inside `diagnose`. A reference to a missing output is a blocker, never an empty string.

Gates bind to a sha256 of the task text plus the `show` output. They are re-checked on every run, so editing the spec after approval reopens the gate. Only you approve; Opus writes the `.approved` file only after your explicit approval in chat.

## Model choice per task

Opus picks a model, or an escalation ladder, for each agent stage in `plan.yaml` under `models:` (with per-experiment overrides). `check-plan` validates the choices, and the generated tasks carry them. A cheap first rung is used only where a deterministic check fully verifies the output. When that rung fails the check without filing its own blocker, the runner retries on the next rung with the failed check output added to the prompt, without involving Opus. Stages whose checks cannot catch omissions or bad reasoning (`requirements`, `compare`) use a single strong model. Actual cost per stage and model is recorded in `logs/usage.tsv`, and Opus uses it to tune later plans.

## Information is never traded for tokens

- The digest is lossless: every scalar at full precision with its path, and large arrays copied verbatim to `inputs/*.json`.
- Command tasks cost no tokens and keep full stdout, stderr, and diagnostics.
- Each agent task restates everything it needs. Executors write `notes/<id>.md`, and notes that affect later tasks pause the run (`REVIEW`).
- Blockers carry complete evidence. Opus opens the full logs, transcripts, and bundles whenever a blocker is insufficient.
- Every session's full transcript (`logs/<id>.<n>.jsonl`) and session id (`logs/usage.tsv`) are kept, along with resolved blockers and their resolutions, and stale approvals.

## Exit codes

`0 ALL_DONE` · `1 ERROR` · `2 BLOCKED` · `3 NEEDS_HUMAN` · `4 REVIEW` · `5 APPROVAL`. The last stdout line is the status.

## Settings

`EXECUTOR_MODEL` (default `claude-sonnet-5-5`), `MAX_ATTEMPTS` per task (default 3), `EXECUTOR_BARE=1` (adds `--bare`; requires `ANTHROPIC_API_KEY`).
