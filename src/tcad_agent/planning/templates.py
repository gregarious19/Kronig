# ruff: noqa: E501  (prompt prose; wrapping would change nothing but noise)
"""Prompt and file templates rendered by the planning service."""

PLAN_TEMPLATE = """\
# Written by Opus: the experiment matrix. Everything else is generated.
backend: devsim            # devsim | sentaurus (must list each kronig_material in its manifest)
compare: true              # add a final cross-experiment comparison task
# Model per agent stage: one id, or a ladder tried in order when the stage's check fails.
# Known: claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5. Omit a stage to use the runner default (Sonnet).
# Start cheap only where the check fully verifies the output; see /kronig-plan for the policy.
models:
  requirements: claude-sonnet-5-5                      # check cannot detect an omitted input
  spec: [claude-haiku-5-5, claude-sonnet-5-5]          # validate + every required value present
  analyse: [claude-haiku-5-5, claude-sonnet-5-5]       # fields, canonical paths, validation cross-checked
  compare: claude-sonnet-5-5                           # reasoning across candidates is not machine-checked
experiments:
  - id: exp_a              # [a-z][a-z0-9_]*
    candidate: ""          # material name as it appears in the Semiconductor-lab results
    kronig_material: ""    # reviewed Kronig material id used for the semiconductor regions
    base_example: examples/<example>.yaml
    intent: ""             # what this experiment changes from the base example and why
    targets: []            # what to measure, with units, e.g. "built-in potential (V)"
    timeout_seconds: 300
    # models: {spec: claude-sonnet-5-5}   # optional per-experiment override (spec, analyse)
"""

REQUIREMENTS_TASK = """\
# p20-requirements: list every input each planned experiment needs, with its source

## Goal
For each experiment in `{run}/plan.yaml`, determine every `ExperimentSpec` value that must be set or changed from the
experiment's `base_example` to carry out its `intent`, and where each value comes from. The output drives the
questions to the user and the spec-authoring tasks, so completeness matters more than brevity.

## Context
- Research goal (verbatim): `{run}/inputs/goal.md`
- Experiment matrix: `{run}/plan.yaml`
- Semiconductor-lab results, lossless digest with exact JSON paths: `{run}/inputs/digest.md` (raw files are listed in its headings)
- Kronig backend manifests, reviewed materials, and examples (verbatim): `{run}/inputs/kronig-context.md`
- Spec field definitions: `src/tcad_agent/domain/models.py`; method: `skills/specification/SKILL.md`, `skills/physics-model-selection/SKILL.md`, `skills/mesh-design/SKILL.md`

## Output
Write `{run}/inputs/requirements.json`:
```json
{{"entries": [
  {{"experiment": "<plan id>", "field": "<spec path, e.g. regions[0].relative_permittivity or physics.temperature>",
    "value": <exact value or null>, "unit": "<unit or null>",
    "status": "found | missing | conflict | base_example | kronig",
    "source": "digest:<absolute raw file path>#<path exactly as printed before ' = ' in the digest> | base_example | kronig:<material id>.<key> | null",
    "note": "<why this field is needed; for conflict, every conflicting value and source>"}}
]}}
```
Rules:
- `found`: the value comes from Semiconductor-lab. Copy it exactly as it appears in the raw file (same number, no rounding or unit conversion); the check re-reads the raw file at the given path and compares.
- `kronig`: provided by Kronig's reviewed material; `base_example`: kept from the example unchanged and still appropriate.
- `missing`: required but available from no source. `conflict`: sources disagree. Never fill these from your own knowledge.
- Include doping, geometry, contacts, bias/study ranges, temperature, and model choices the intent implies, not only material properties.
- If an intent cannot be expressed in an ExperimentSpec or is not supported by the backend manifest, write a blocker.

## Done when
`{check}` exits 0.

## Out of scope
Do not write specs or edit Kronig files.
"""

SPEC_TASK = """\
# {eid}20-spec: author the ExperimentSpec for experiment `{eid}`

## Goal
Write `{spec}` so experiment `{eid}` does exactly this: {intent}
Targets to measure: {targets}

## Context
- Research goal (verbatim): {goal}
- Candidate: {candidate}; Kronig material for semiconductor regions: `{material}`; backend: `{backend}`.
- Start from `{base}` (copy it, then change only what the requirements below and the intent require).
- Required values. Use each exactly as given (value and unit), at the given field:
```json
{requirements}
```
- Follow `skills/specification/SKILL.md`, `skills/mesh-design/SKILL.md`, `skills/physics-model-selection/SKILL.md`.
- Spec field definitions: `src/tcad_agent/domain/models.py`. Backend capabilities: `src/tcad_agent/adapters/{backend}/manifest.yaml`.

## Files
- {base}: read
- {spec}: create

## Done when
`{check}` exits 0 (Kronig validation, then a check that every required value is present).

## Out of scope
Do not edit Kronig files. If the intent cannot be expressed or the backend refuses it, write a blocker.
"""

ANALYSE_TASK = """\
# {eid}70-analyse: extract the results of experiment `{eid}`

## Goal
Report the targets of experiment `{eid}` ({targets}) from Kronig's evidence bundle.

## Context
- Research goal (verbatim): {goal}
- Bundle: `{{{{out:{eid}60-run}}}}`. Read `results/canonical.json`, `validation/report.json`, and `report.md` there.
- Spec that was run: `{spec}`. Candidate {candidate} (Kronig material `{material}`).
- Requirement provenance for this experiment (carry caveats into the result):
```json
{requirements}
```
- Follow `skills/result-validation/SKILL.md`. Numbers come only from canonical result fields; never compute them from logs.

## Output
Write `{result}`:
```json
{{"experiment": "{eid}", "bundle": "<bundle path>", "validation_overall": "<overall from validation/report.json>",
  "metrics": [{{"name": "<target>", "value": <number>, "unit": "<unit>", "canonical_field": "<path in canonical.json>"}}],
  "caveats": ["<every validation warning/failure, provisional or ML-only input, assumption>"]}}
```

## Done when
`{check}` exits 0.
"""

COMPARE_TASK = """\
# z90-compare: compare the experiments against the research goal

## Goal
Answer the research goal from the experiment results. Research goal (verbatim): {goal}

## Context
- Results: {results}
- Each result's caveats and validation status must appear in the comparison; a failed validation is never ranked as a success.
- Follow `skills/reporting/SKILL.md`.

## Output
Write `{summary}`: a table of every experiment's metrics (value, unit, canonical field), validation status, and caveats;
then a ranking against the goal's criteria, with the reasoning and its limits.

## Done when
`{check}` exits 0.
"""
