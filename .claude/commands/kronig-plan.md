---
description: Plan and run a Kronig TCAD experiment from Semiconductor-lab results + a goal, delegating all mechanical planning
argument-hint: <semiconductor_lab result path(s)> -- <experiment goal>
model: claude-opus-5-5
---

You are the planner. Your only planning judgement is the experiment matrix in `plan.yaml`. The Kronig CLI (`tcad-agent plan …`) and Sonnet tasks do everything else: digest, context gathering, requirements extraction, questions, task rendering, execution. Do not read the digest, context pack, or examples yourself unless a step below sends you there.

Arguments: `$ARGUMENTS` (result paths before `--`, the goal after it).

1. **Prepare (Kronig CLI, no model):**
   `orchestration/kronig plan prepare <short-slug> <result paths> --goal "<goal verbatim>" --cli orchestration/kronig`
   Read only its printed summary: run dir, candidates found, each backend's materials, studies, and execution state, and the example names.

2. **Write `<run>/plan.yaml`** from `<run>/plan.template.yaml`: which candidates, which Kronig material, which base example, an intent and targets per experiment, the backend.
   - If a candidate has no reviewed Kronig material on the chosen backend, still plan it as you think it should be. The next step's feasibility check will stop and explain. Never map a candidate onto a different material.
   - If you need a detail to choose, read the one relevant section of `<run>/inputs/kronig-context.md` (e.g. one example), not the whole file.
   - **Choose a model for each agent stage** in `models:` (and per-experiment `models:` overrides for `spec`/`analyse`). Optimise accuracy first, then cost:
     - Use a single strong model where the stage's check **cannot detect an error of omission or judgement**: `requirements` (a missed input passes the check), `compare` (cross-candidate reasoning). Sonnet by default; Opus when the goal involves subtle physics trade-offs or many conflicting sources.
     - Use a ladder that starts cheap, e.g. `[claude-haiku-5-5, claude-sonnet-5-5]`, where the check **fully verifies** the output: `spec` (Kronig validation plus every required value present) and `analyse` (fields, canonical paths, and validation status cross-checked against the bundle). A failed check escalates automatically, without you.
     - Start an experiment's `spec` at Sonnet when it departs far from its base example (new study type, multi-region, contacts or interfaces changed). A cheap model would likely fail first, and the failed attempt costs more than it saves.
     - Use evidence, not guesses: if earlier runs exist, read their `logs/usage.tsv` (cost and turns per stage and model) and the `escalate` lines in their output, and adjust.
     - Never choose by price alone: a cheaper model is acceptable only where a deterministic check stands behind it.

3. **Planning run (Kronig CLI + one Sonnet task):** start `bash orchestration/run_tasks.sh <run>/plan` with `run_in_background: true`. It checks feasibility, has Sonnet extract every required input with its source (each lab value is verified against the raw file), lists questions, and renders the experiment tasks. Handle its last line:
   - `NEEDS_HUMAN …/p10-check-plan.md` → the plan is infeasible or invalid. Show the user the reason. For a missing material, offer a separate Kronig development run, or a revised plan. Fix `plan.yaml` only with their agreement.
   - `NEEDS_HUMAN …/p30-questions.md` → show the user `<run>/inputs/questions.md` as it is. Write their answers verbatim to `<run>/inputs/answers.yaml` (`<key>: <value with unit>`), archive the blocker to `blockers/resolved/`, rerun.
   - `BLOCKED` → `/kronig-unblock <path>`. `ALL_DONE` → go to 4.

4. **Experiment run:** start `bash orchestration/run_tasks.sh <run>` with `run_in_background: true` and handle its last line:
   - `APPROVAL <request>` → show the user the request (spec, compile output, input provenance, sha256). Only after their explicit approval in chat, write the `.approved` file exactly as the request instructs, with their name. Then rerun.
   - `BLOCKED <path>` → `/kronig-unblock <path>`. If a fix changes requirements or answers, rerun `orchestration/kronig plan render <run>` before resuming. Finished stages are kept.
   - `REVIEW <notes>` → update `plan.yaml` or answers as the notes require, re-render, rerun.
   - `NEEDS_HUMAN <path>` → show the blocker in full and ask.
   - `ALL_DONE` → read `<run>/results/*.json` (and `summary.md` if there is one) and `notes/`. Report each experiment's metrics with units, validation status, and caveats, plus the bundle paths and the cost from `logs/usage.tsv` of both runs.
