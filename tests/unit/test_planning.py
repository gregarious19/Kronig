import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from tcad_agent.cli import app
from tcad_agent.planning import digest, service

REPO = Path(__file__).parents[2]
BASE_EXAMPLE = REPO / "examples" / "pn-junction.yaml"
runner = CliRunner()


def _results(tmp_path: Path) -> Path:
    path = tmp_path / "lab" / "golden_loop.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "material": "GaN",
                "stages": [{"stage": "S2a lattice a", "value": 3.1891234567, "pass": True}],
                "positions": [[0.1 * i, 0.5, 0.25] for i in range(12)],
            }
        )
    )
    return path


def _prepare(tmp_path: Path) -> Path:
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "base.yaml").write_text(BASE_EXAMPLE.read_text())
    prepared = service.prepare(
        "Trial Run",
        "Measure reverse leakage.",
        [_results(tmp_path)],
        runs_root=tmp_path / "runs",
        examples_dir=examples,
        cli="kronig",
    )
    return prepared.run


def _write_plan(run: Path, material: str = "silicon") -> None:
    (run / "plan.yaml").write_text(
        yaml.safe_dump(
            {
                "backend": "devsim",
                "compare": False,
                "experiments": [
                    {
                        "id": "exp_a",
                        "candidate": "GaN",
                        "kronig_material": material,
                        "base_example": str(run.parent.parent / "examples" / "base.yaml"),
                        "intent": "Reverse sweep to a researcher-chosen limit.",
                        "targets": ["leakage current density (A/cm^2)"],
                    }
                ],
            }
        )
    )


def _write_requirements(run: Path, lattice_value: object = 3.1891234567) -> None:
    raw = run.parent.parent / "lab" / "golden_loop.json"
    entries = [
        {
            "experiment": "exp_a",
            "field": "study.stop",
            "value": None,
            "unit": "V",
            "status": "missing",
            "source": None,
            "note": "Reverse-bias limit.",
        },
        {
            "experiment": "exp_a",
            "field": "physics.temperature",
            "value": "300 K",
            "unit": None,
            "status": "base_example",
            "source": "base_example",
            "note": "",
        },
        {
            "experiment": "exp_a",
            "field": "metadata.lattice_a",
            "value": lattice_value,
            "unit": "Å",
            "status": "found",
            "source": f"digest:{raw}#stages[0].value",
            "note": "",
        },
    ]
    (run / "inputs" / "requirements.json").write_text(json.dumps({"entries": entries}))


def test_flat_digest_is_lossless_and_externalizes_large_arrays(tmp_path) -> None:
    source = _results(tmp_path)
    text = digest.render([source], out_dir=tmp_path / "out", flat=True)
    assert "stages[0].value = 3.1891234567" in text
    assert "material = GaN" in text
    pointer = next(line for line in text.splitlines() if line.startswith("positions = "))
    side_file = Path(pointer.split("→ ")[1].rstrip(">"))
    assert json.loads(side_file.read_text()) == json.loads(source.read_text())["positions"]


def test_prepare_creates_digest_context_and_planning_tasks(tmp_path) -> None:
    run = _prepare(tmp_path)
    assert run.name.endswith("-trial-run")
    assert (run / "inputs" / "goal.md").read_text().strip() == "Measure reverse leakage."
    assert "material = GaN" in (run / "inputs" / "digest.md").read_text()
    assert "silicon" in (run / "inputs" / "kronig-context.md").read_text()
    tasks = json.loads((run / "plan" / "tasks.json").read_text())["tasks"]
    assert [t["id"] for t in tasks] == [
        "p10-check-plan",
        "p20-requirements",
        "p30-questions",
        "p40-render",
    ]
    assert tasks[0]["cmd"].startswith("kronig plan check-plan ")
    assert tasks[0]["on_fail"] == "human"
    assert (run / "plan" / "tasks" / "p20-requirements.md").is_file()


def test_check_plan_refuses_material_without_reviewed_support(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run, material="gallium_nitride")
    problems = service.check_plan(run)
    assert any("no reviewed material 'gallium_nitride'" in p for p in problems)


def test_check_plan_accepts_supported_material(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run)
    assert service.check_plan(run) == []


def test_check_requirements_verifies_lab_values_against_raw_file(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run)
    _write_requirements(run)
    assert service.check_requirements(run) == []
    _write_requirements(run, lattice_value=3.19)
    assert any("differs from raw 3.1891234567" in p for p in service.check_requirements(run))


def test_questions_stay_open_until_answered(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run)
    _write_requirements(run)
    assert [q["field"] for q in service.open_questions(run)] == ["study.stop"]
    assert "exp_a.study.stop" in (run / "inputs" / "questions.md").read_text()
    (run / "inputs" / "answers.yaml").write_text("exp_a.study.stop: -5 V\n")
    assert service.open_questions(run) == []


def test_render_builds_stage_tasks_with_answers_inlined(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run)
    _write_requirements(run)
    (run / "inputs" / "answers.yaml").write_text("exp_a.study.stop: -5 V\n")
    count = service.render(run)
    tasks = json.loads((run / "tasks.json").read_text())["tasks"]
    assert count == len(tasks)
    assert [t["id"] for t in tasks] == [
        "exp_a20-spec",
        "exp_a30-validate",
        "exp_a40-compile",
        "exp_a50-approve",
        "exp_a60-run",
        "exp_a70-analyse",
        "exp_a80-report",
    ]
    by_id = {t["id"]: t for t in tasks}
    assert by_id["exp_a50-approve"]["type"] == "gate"
    assert "--approve" in by_id["exp_a60-run"]["cmd"]
    assert "{{out:self}}" in by_id["exp_a60-run"]["diagnose"]
    assert by_id["exp_a20-spec"]["check"].startswith("kronig validate ")
    spec_task = (run / "tasks" / "exp_a20-spec.md").read_text()
    assert '"value": "-5 V"' in spec_task and '"status": "answered"' in spec_task


def test_render_refuses_unanswered_questions(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run)
    _write_requirements(run)
    try:
        service.render(run)
    except service.PlanningError as exc:
        assert "study.stop" in str(exc)
    else:
        raise AssertionError("render must refuse unanswered inputs")


def test_check_spec_requires_every_required_value(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run)
    _write_requirements(run)
    (run / "inputs" / "answers.yaml").write_text("exp_a.study.stop: -5 V\n")
    spec = yaml.safe_load(BASE_EXAMPLE.read_text())
    spec["metadata"] = {"lattice_a": 3.1891234567}
    (run / "specs").mkdir()
    (run / "specs" / "exp_a.yaml").write_text(yaml.safe_dump(spec))
    assert any("study.stop" in p for p in service.check_spec(run, "exp_a"))
    spec["study"]["stop"] = "-5 V"
    (run / "specs" / "exp_a.yaml").write_text(yaml.safe_dump(spec))
    assert service.check_spec(run, "exp_a") == []


def test_check_results_cross_checks_bundle_validation(tmp_path) -> None:
    run = _prepare(tmp_path)
    bundle = tmp_path / "bundle"
    (bundle / "validation").mkdir(parents=True)
    (bundle / "results").mkdir()
    (bundle / "validation" / "report.json").write_text(json.dumps({"overall": "passed"}))
    (bundle / "results" / "canonical.json").write_text(
        json.dumps({"metrics": {"leakage": 1.0e-12}})
    )
    result = {
        "experiment": "exp_a",
        "bundle": str(bundle),
        "validation_overall": "failed",
        "metrics": [
            {
                "name": "leakage",
                "value": 1.0e-12,
                "unit": "A/cm^2",
                "canonical_field": "metrics.leakage",
            }
        ],
        "caveats": [],
    }
    (run / "results").mkdir()
    (run / "results" / "exp_a.json").write_text(json.dumps(result))
    assert any("validation_overall" in p for p in service.check_results(run, "exp_a"))
    result["validation_overall"] = "passed"
    (run / "results" / "exp_a.json").write_text(json.dumps(result))
    assert service.check_results(run, "exp_a") == []


def test_cli_plan_commands_report_problems_with_nonzero_exit(tmp_path) -> None:
    run = _prepare(tmp_path)
    _write_plan(run, material="gallium_nitride")
    result = runner.invoke(app, ["plan", "check-plan", str(run)])
    assert result.exit_code == 1
    assert "no reviewed material" in result.output
    _write_plan(run)
    assert runner.invoke(app, ["plan", "check-plan", str(run)]).exit_code == 0


def _plan_with_models(run: Path, models: dict, exp_models: dict | None = None) -> None:
    _write_plan(run)
    plan = yaml.safe_load((run / "plan.yaml").read_text())
    plan["models"] = models
    if exp_models is not None:
        plan["experiments"][0]["models"] = exp_models
    (run / "plan.yaml").write_text(yaml.safe_dump(plan))


def test_check_plan_rejects_unknown_models(tmp_path) -> None:
    run = _prepare(tmp_path)
    _plan_with_models(run, {"spec": "gpt-4", "analyse": ["claude-haiku-5-5", "nope"]})
    problems = service.check_plan(run)
    assert any("models.spec" in p and "gpt-4" in p for p in problems)
    assert any("models.analyse" in p and "nope" in p for p in problems)


def test_check_plan_applies_requirements_model_to_planning_task(tmp_path) -> None:
    run = _prepare(tmp_path)
    _plan_with_models(run, {"requirements": ["claude-sonnet-5-5", "claude-opus-5-5"]})
    assert service.check_plan(run) == []
    tasks = json.loads((run / "plan" / "tasks.json").read_text())["tasks"]
    req = next(t for t in tasks if t["id"] == "p20-requirements")
    assert req["model"] == ["claude-sonnet-5-5", "claude-opus-5-5"]


def test_render_assigns_stage_models_with_experiment_overrides(tmp_path) -> None:
    run = _prepare(tmp_path)
    _plan_with_models(
        run,
        {"spec": ["claude-haiku-5-5", "claude-sonnet-5-5"], "analyse": "claude-haiku-5-5"},
        exp_models={"analyse": "claude-sonnet-5-5"},
    )
    _write_requirements(run)
    (run / "inputs" / "answers.yaml").write_text("exp_a.study.stop: -5 V\n")
    service.render(run)
    by_id = {t["id"]: t for t in json.loads((run / "tasks.json").read_text())["tasks"]}
    assert by_id["exp_a20-spec"]["model"] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert by_id["exp_a70-analyse"]["model"] == "claude-sonnet-5-5"
    assert "model" not in by_id["exp_a30-validate"]
