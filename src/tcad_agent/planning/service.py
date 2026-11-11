"""Planning service: prepare a run, check the plan, verify requirements, ask, and render tasks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from tcad_agent.capabilities.models import CapabilityManifest
from tcad_agent.physics.materials import MaterialRegistry
from tcad_agent.planning import digest
from tcad_agent.planning.templates import (
    ANALYSE_TASK,
    COMPARE_TASK,
    PLAN_TEMPLATE,
    REQUIREMENTS_TASK,
    SPEC_TASK,
)

BACKENDS = ("devsim", "sentaurus")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
STATUSES = {"found", "missing", "conflict", "base_example", "kronig"}
KNOWN_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
PLAN_STAGES = ("requirements", "spec", "analyse", "compare")
EXPERIMENT_STAGES = ("spec", "analyse")
_PATH_TOKEN = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


class PlanningError(RuntimeError):
    """A planning step cannot proceed; the message lists every problem."""


@dataclass(frozen=True)
class PreparedRun:
    run: Path
    candidates: tuple[str, ...]
    summary: str


# ------------------------------------------------------------------ helpers
def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_meta(run: Path) -> dict[str, Any]:
    path = run / "run.json"
    return json.loads(path.read_text()) if path.is_file() else {"cli": "tcad-agent"}


def resolve_path(data: Any, path: str) -> Any:
    current = data
    for key, index in _PATH_TOKEN.findall(path):
        current = current[int(index)] if index else current[key]
    return current


def _model_problems(models: Any, where: str, stages: tuple[str, ...]) -> list[str]:
    if models is None:
        return []
    if not isinstance(models, dict):
        return [f"{where} must map stage names to a model or a ladder of models"]
    problems = []
    for stage, choice in models.items():
        if stage not in stages:
            problems.append(f"{where}.{stage}: unknown stage (allowed: {list(stages)})")
            continue
        ladder = [choice] if isinstance(choice, str) else choice
        if not isinstance(ladder, list) or not ladder:
            problems.append(f"{where}.{stage} must be a model id or a non-empty list of model ids")
            continue
        for model in ladder:
            if model not in KNOWN_MODELS:
                problems.append(
                    f"{where}.{stage}: unknown model {model!r} (known: {list(KNOWN_MODELS)})"
                )
    return problems


def _stage_model(plan: dict[str, Any], stage: str, experiment: dict[str, Any] | None = None) -> Any:
    if experiment is not None and stage in (experiment.get("models") or {}):
        return experiment["models"][stage]
    return (plan.get("models") or {}).get(stage)


def _apply_requirements_model(run: Path, plan: dict[str, Any]) -> None:
    path = run / "plan" / "tasks.json"
    if not path.is_file():
        return
    manifest = json.loads(path.read_text())
    choice = _stage_model(plan, "requirements")
    for task in manifest["tasks"]:
        if task["id"] == "p20-requirements":
            if choice is None:
                task.pop("model", None)
            else:
                task["model"] = choice
    _dump_json(path, manifest)


def same_value(left: Any, right: Any) -> bool:
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _candidates(files: list[Path]) -> tuple[str, ...]:
    keys = {"material", "composition", "formula", "reduced_formula", "pretty_formula"}
    found: list[str] = []

    def walk(value: Any, depth: int) -> None:
        if depth > 3:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and item not in found:
                    found.append(item)
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    for f in files:
        if f.suffix == ".json":
            try:
                walk(json.loads(f.read_text()), 0)
            except ValueError:
                continue
    return tuple(found)


def _context_pack(examples_dir: Path) -> str:
    out = ["# Kronig context pack (verbatim)", ""]
    for backend in BACKENDS:
        manifest = CapabilityManifest.from_backend(backend).model_dump(mode="json")
        out += [
            f"## Backend capability manifest: {backend}",
            "```json",
            json.dumps(manifest, indent=2),
            "```",
            "",
        ]
    materials = [m.model_dump(mode="json") for m in MaterialRegistry.default().all()]
    out += [
        "## Reviewed materials (MaterialRegistry.default)",
        "```json",
        json.dumps(materials, indent=2),
        "```",
        "",
    ]
    for example in sorted(examples_dir.glob("*.yaml")):
        out += [f"## Example {example}", "```yaml", example.read_text().rstrip(), "```", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ prepare
def prepare(
    slug: str,
    goal: str,
    results: list[Path],
    *,
    runs_root: Path,
    examples_dir: Path,
    cli: str,
) -> PreparedRun:
    files = digest.expand([p.resolve() for p in results])
    name = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-") or "run"
    run = runs_root.resolve() / f"{datetime.now():%Y%m%d-%H%M}-{name}"
    if run.exists():
        raise PlanningError(f"{run} already exists")
    inputs = run / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "digest.md").write_text(digest.render(files, out_dir=inputs, flat=True))
    (inputs / "goal.md").write_text(goal.strip() + "\n")
    (inputs / "kronig-context.md").write_text(_context_pack(examples_dir))
    (run / "plan.template.yaml").write_text(PLAN_TEMPLATE)
    _dump_json(run / "run.json", {"cli": cli, "examples_dir": str(examples_dir.resolve())})

    plan_dir = run / "plan"
    (plan_dir / "tasks").mkdir(parents=True)
    check_req = f"{cli} plan check-requirements {run}"
    (plan_dir / "tasks" / "p20-requirements.md").write_text(
        REQUIREMENTS_TASK.format(run=run, check=check_req)
    )
    _dump_json(
        plan_dir / "tasks.json",
        {
            "goal": goal.strip(),
            "repo_root": ".",
            "defaults": {
                "tools": "Read,Write,Grep,Glob",
                "max_turns": 30,
                "max_budget_usd": 3,
                "add_dirs": [str(run)],
            },
            "tasks": [
                {
                    "id": "p10-check-plan",
                    "type": "command",
                    "cmd": f"{cli} plan check-plan {run}",
                    "on_fail": "human",
                },
                {
                    "id": "p20-requirements",
                    "type": "agent",
                    "check": check_req,
                    "tools": "Read,Write,Grep,Glob,Bash",
                    "allowed": f"Bash({cli} plan check-requirements:*)",
                },
                {
                    "id": "p30-questions",
                    "type": "command",
                    "cmd": f"{cli} plan questions {run}",
                    "on_fail": "human",
                },
                {"id": "p40-render", "type": "command", "cmd": f"{cli} plan render {run}"},
            ],
        },
    )

    candidates = _candidates(files)
    lines = [
        f"RUN {run}",
        f"goal: {goal.strip()}",
        f"candidates in results: {', '.join(candidates) or '(none detected; see digest)'}",
    ]
    for backend in BACKENDS:
        m = CapabilityManifest.from_backend(backend)
        lines.append(
            f"{backend}: execution_state={m.execution_state} dimensions={list(m.dimensions)} "
            f"materials={list(m.materials)} studies={list(m.studies)}"
        )
    lines.append("examples: " + ", ".join(str(p) for p in sorted(examples_dir.glob("*.yaml"))))
    lines.append(
        f"next: write {run}/plan.yaml from plan.template.yaml, "
        f"then run the planning tasks in {plan_dir}"
    )
    return PreparedRun(run=run, candidates=candidates, summary="\n".join(lines))


# ------------------------------------------------------------------ plan
def load_plan(run: Path) -> dict[str, Any]:
    path = run / "plan.yaml"
    if not path.is_file():
        raise PlanningError(f"{path} not written yet")
    return yaml.safe_load(path.read_text()) or {}


def _example_path(run: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def check_plan(run: Path) -> list[str]:
    plan = load_plan(run)
    problems: list[str] = []
    backend = plan.get("backend")
    manifest = CapabilityManifest.from_backend(backend) if backend in BACKENDS else None
    if manifest is None:
        problems.append(f"backend must be one of {list(BACKENDS)}, got {backend!r}")
    elif manifest.execution_state != "configured":
        problems.append(
            f"backend {backend} execution_state is {manifest.execution_state!r}; "
            "it cannot run experiments yet"
        )
    problems += _model_problems(plan.get("models"), "models", PLAN_STAGES)
    experiments = plan.get("experiments") or []
    if not experiments:
        problems.append("plan has no experiments")
    seen: set[str] = set()
    for i, exp in enumerate(experiments):
        tag = f"experiments[{i}]"
        eid = str(exp.get("id", ""))
        if not ID_RE.match(eid):
            problems.append(f"{tag}.id {eid!r} must match {ID_RE.pattern}")
        if eid in seen:
            problems.append(f"{tag}.id {eid!r} is duplicated")
        seen.add(eid)
        for key in ("candidate", "kronig_material", "base_example", "intent"):
            if not exp.get(key):
                problems.append(f"{tag}.{key} is required")
        if not exp.get("targets"):
            problems.append(f"{tag}.targets must list at least one measurement")
        problems += _model_problems(exp.get("models"), f"{tag}.models", EXPERIMENT_STAGES)
        if exp.get("base_example") and not _example_path(run, exp["base_example"]).is_file():
            problems.append(f"{tag}.base_example {exp['base_example']} not found")
        material = exp.get("kronig_material")
        if manifest is not None and material and material not in manifest.materials:
            problems.append(
                f"{tag}: Kronig has no reviewed material {material!r} for {backend} "
                f"(supported: {list(manifest.materials)}). "
                f"Simulating candidate {exp.get('candidate')!r} needs a Kronig development run "
                "that adds a reviewed material descriptor, physics-registry support, and the "
                "backend manifest entry, test-first. "
                "It cannot be approximated with another material."
            )
    if not problems:
        _apply_requirements_model(run, plan)
    return problems


# ------------------------------------------------------------------ requirements
def load_requirements(run: Path) -> list[dict[str, Any]]:
    path = run / "inputs" / "requirements.json"
    if not path.is_file():
        raise PlanningError(f"{path} not written")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise PlanningError("requirements.json must be an object with an 'entries' list")
    entries: list[dict[str, Any]] = data["entries"]
    if not all(isinstance(e, dict) for e in entries):
        raise PlanningError("every requirements.json entry must be an object")
    return entries


def load_answers(run: Path) -> dict[str, Any]:
    path = run / "inputs" / "answers.yaml"
    return (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}


def check_requirements(run: Path) -> list[str]:
    plan_ids = {e["id"] for e in load_plan(run).get("experiments", [])}
    entries = load_requirements(run)
    problems: list[str] = []
    covered: set[str] = set()
    for i, entry in enumerate(entries):
        tag = f"entries[{i}] ({entry.get('experiment')}.{entry.get('field')})"
        for key in ("experiment", "field", "status"):
            if not entry.get(key):
                problems.append(f"{tag}: {key} is required")
        if entry.get("experiment") not in plan_ids:
            problems.append(f"{tag}: experiment not in plan.yaml")
        covered.add(str(entry.get("experiment")))
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(f"{tag}: status must be one of {sorted(STATUSES)}")
        if status in {"found", "kronig"} and entry.get("value") is None:
            problems.append(f"{tag}: status {status} needs a value")
        if status == "conflict" and not entry.get("note"):
            problems.append(f"{tag}: conflict needs a note listing every value and source")
        if status != "found":
            continue
        source = str(entry.get("source") or "")
        if not source.startswith("digest:") or "#" not in source:
            problems.append(f"{tag}: found values need source 'digest:<raw file>#<json path>'")
            continue
        file, json_path = source[len("digest:") :].split("#", 1)
        try:
            raw = resolve_path(json.loads(Path(file).read_text()), json_path)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            problems.append(f"{tag}: cannot resolve {source}: {exc!r}")
            continue
        if not same_value(raw, entry.get("value")):
            problems.append(
                f"{tag}: value {entry.get('value')!r} differs from raw {raw!r} at {source}"
            )
    for missing in sorted(plan_ids - covered):
        problems.append(f"experiment {missing} has no requirement entries")
    return problems


def open_questions(run: Path) -> list[dict[str, Any]]:
    answers = load_answers(run)
    items = [
        e
        for e in load_requirements(run)
        if e.get("status") in {"missing", "conflict"}
        and f"{e['experiment']}.{e['field']}" not in answers
    ]
    path = run / "inputs" / "questions.md"
    if not items:
        path.write_text("No open questions.\n")
        return []
    lines = [
        "# Inputs needed from you",
        "",
        f"Answer in {run}/inputs/answers.yaml as `<key>: <value with unit>`.",
        "",
    ]
    for e in items:
        lines.append(
            f"- `{e['experiment']}.{e['field']}` ({e['status']}): {e.get('note') or ''}".rstrip()
        )
    path.write_text("\n".join(lines) + "\n")
    return items


# ------------------------------------------------------------------ render
def render(run: Path) -> int:
    plan = load_plan(run)
    entries = load_requirements(run)
    answers = load_answers(run)
    cli = _run_meta(run)["cli"]
    goal = (run / "inputs" / "goal.md").read_text().strip()
    backend = plan["backend"]
    tasks_dir = run / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    tasks: list[dict[str, Any]] = []
    result_files: list[str] = []

    unanswered: list[str] = []
    rendered: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for exp in plan["experiments"]:
        eid = exp["id"]
        reqs = []
        for entry in entries:
            if entry["experiment"] != eid:
                continue
            entry = dict(entry)
            key = f"{eid}.{entry['field']}"
            if key in answers:
                entry.update(
                    value=answers[key], status="answered", source="user (inputs/answers.yaml)"
                )
            elif entry["status"] in {"missing", "conflict"}:
                unanswered.append(key)
            reqs.append(entry)
        rendered.append((exp, reqs))
    if unanswered:
        raise PlanningError(
            f"unanswered inputs: {', '.join(unanswered)}; answer the questions first"
        )

    for exp, reqs in rendered:
        eid = exp["id"]
        spec = run / "specs" / f"{eid}.yaml"
        result = run / "results" / f"{eid}.json"
        result_files.append(str(result))
        req_json = json.dumps(reqs, indent=2, ensure_ascii=False)
        targets = "; ".join(map(str, exp["targets"]))
        fmt = dict(
            eid=eid,
            intent=exp["intent"],
            targets=targets,
            goal=goal,
            candidate=exp["candidate"],
            material=exp["kronig_material"],
            backend=backend,
            base=exp["base_example"],
            spec=spec,
            result=result,
            requirements=req_json,
        )
        spec_check = f"{cli} validate {spec} >/dev/null && {cli} plan check-spec {run} {eid}"
        res_check = f"{cli} plan check-results {run} {eid}"
        (tasks_dir / f"{eid}20-spec.md").write_text(SPEC_TASK.format(check=spec_check, **fmt))
        (tasks_dir / f"{eid}70-analyse.md").write_text(ANALYSE_TASK.format(check=res_check, **fmt))
        (tasks_dir / f"{eid}50-approve.md").write_text(
            f"# {eid}50-approve: approve running experiment `{eid}` on {backend}\n\n"
            f"Intent: {exp['intent']}\nTargets: {targets}\n\n"
            f"Inputs and their provenance:\n```json\n{req_json}\n```\n"
        )
        tasks += [
            {
                "id": f"{eid}20-spec",
                "type": "agent",
                "check": spec_check,
                "tools": "Read,Write,Edit,Grep,Glob,Bash",
                "allowed": f"Bash({cli} validate:*),Bash({cli} plan check-spec:*)",
            },
            {"id": f"{eid}30-validate", "type": "command", "cmd": f"{cli} validate {spec}"},
            {
                "id": f"{eid}40-compile",
                "type": "command",
                "cmd": f"{cli} compile {spec} --backend {backend} --output {run}/compiled/{eid}",
            },
            {
                "id": f"{eid}50-approve",
                "type": "gate",
                "show": f"cat {spec}; echo; cat {{{{outpath:{eid}40-compile}}}}",
            },
            {
                "id": f"{eid}60-run",
                "type": "command",
                "cmd": f"{cli} run {spec} --backend {backend} --approve "
                f"--output {run}/bundles/{eid} "
                f"--timeout-seconds {int(exp.get('timeout_seconds', 300))}",
                "diagnose": "b={{out:self}}; "
                'echo "state: $(jq -r .state $b/manifest.json 2>/dev/null)"; '
                "echo '== simulator stderr'; cat $b/logs/stderr.log; "
                "echo '== validation'; cat $b/validation/report.json",
            },
            {
                "id": f"{eid}70-analyse",
                "type": "agent",
                "check": res_check,
                "tools": "Read,Write,Grep,Glob,Bash",
                "allowed": f"Bash({cli} plan check-results:*),Bash(jq:*)",
            },
            {
                "id": f"{eid}80-report",
                "type": "command",
                "cmd": f"{cli} report {{{{out:{eid}60-run}}}}",
            },
        ]

    experiments = plan["experiments"]
    if plan.get("compare", True) and len(experiments) > 1:
        summary = run / "results" / "summary.md"
        check = f"test -s {summary} && " + " && ".join(
            f"grep -q {e['id']} {summary}" for e in experiments
        )
        (tasks_dir / "z90-compare.md").write_text(
            COMPARE_TASK.format(
                goal=goal,
                results=", ".join(f"`{r}`" for r in result_files),
                summary=summary,
                check=check,
            )
        )
        tasks.append(
            {"id": "z90-compare", "type": "agent", "check": check, "tools": "Read,Write,Grep,Glob"}
        )

    _assign_models(plan, tasks)
    _dump_json(
        run / "tasks.json",
        {
            "goal": goal,
            "repo_root": ".",
            "defaults": {
                "tools": "Read,Write,Grep,Glob",
                "max_turns": 30,
                "max_budget_usd": 3,
                "add_dirs": [str(run)],
            },
            "tasks": tasks,
        },
    )
    return len(tasks)


def _assign_models(plan: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    """Apply the planner's per-stage model (or escalation ladder) choices to agent tasks."""
    experiments = {e["id"]: e for e in plan["experiments"]}
    stage_by_suffix = {"20-spec": "spec", "70-analyse": "analyse"}
    for task in tasks:
        choice = None
        if task["id"] == "z90-compare":
            choice = _stage_model(plan, "compare")
        for suffix, stage in stage_by_suffix.items():
            eid = task["id"].removesuffix(suffix)
            if eid != task["id"] and eid in experiments:
                choice = _stage_model(plan, stage, experiments[eid])
        if choice is not None:
            task["model"] = choice


# ------------------------------------------------------------------ stage checks
def check_spec(run: Path, experiment: str) -> list[str]:
    spec = yaml.safe_load((run / "specs" / f"{experiment}.yaml").read_text())
    answers = load_answers(run)
    problems: list[str] = []
    for entry in load_requirements(run):
        if entry["experiment"] != experiment or entry["status"] in {"base_example", "kronig"}:
            continue
        value = answers.get(f"{experiment}.{entry['field']}", entry.get("value"))
        try:
            actual = resolve_path(spec, entry["field"])
        except (KeyError, IndexError, TypeError):
            problems.append(f"{entry['field']}: missing from spec (expected {value!r})")
            continue
        unit = entry.get("unit")
        expected = (
            f"{value} {unit}" if unit and not str(value).strip().endswith(str(unit)) else value
        )
        if not (same_value(actual, value) or same_value(actual, expected)):
            problems.append(f"{entry['field']}: spec has {actual!r}, required {expected!r}")
    return problems


def check_results(run: Path, experiment: str) -> list[str]:
    data = json.loads((run / "results" / f"{experiment}.json").read_text())
    problems: list[str] = []
    for key in ("experiment", "bundle", "validation_overall", "metrics", "caveats"):
        if key not in data:
            problems.append(f"missing {key}")
    bundle = Path(str(data.get("bundle", "")))
    report = bundle / "validation" / "report.json"
    if report.is_file():
        overall = json.loads(report.read_text()).get("overall")
        if data.get("validation_overall") != overall:
            problems.append(
                f"validation_overall {data.get('validation_overall')!r} != bundle {overall!r}"
            )
    else:
        problems.append(f"bundle validation report not found: {report}")
    canonical_path = bundle / "results" / "canonical.json"
    canonical = json.loads(canonical_path.read_text()) if canonical_path.is_file() else None
    metrics = data.get("metrics") or []
    if not metrics:
        problems.append("no metrics reported")
    for i, metric in enumerate(metrics):
        for key in ("name", "value", "unit", "canonical_field"):
            if key not in metric:
                problems.append(f"metrics[{i}] missing {key}")
        if canonical is not None and metric.get("canonical_field"):
            try:
                resolve_path(canonical, metric["canonical_field"])
            except (KeyError, IndexError, TypeError):
                problems.append(
                    f"metrics[{i}].canonical_field {metric['canonical_field']!r} "
                    "not in canonical.json"
                )
    return problems
