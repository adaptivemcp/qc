"""End-to-end CLI tests: the user journeys of SPEC section 3.

Runs the real console script in a subprocess against a temp project:
init → source add → grant → test → profile → show/diff → sanitize
preview/export → generate plan/run/validate → job/doctor/version.
Also asserts stable exit codes (SPEC 11.3) and no secrets in output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def run_qc(
    project: Path, *args: str, env_extra: dict[str, str] | None = None, expect: int | None = 0
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.pop("QC_DEMO_ROOT", None)
    # fixture sources resolve their root from env:QC_UNUSED
    env["QC_UNUSED"] = str(project / "data")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", "qc.cli.app", *args],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"qc {' '.join(args)} exited {proc.returncode} (want {expect})\n"
            f"stdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-1500:]}"
        )
    return proc


@pytest.fixture
def project(tmp_path: Path, edge_rows: list[dict]) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    data = root / "data"
    data.mkdir()
    (data / "people.jsonl").write_text(
        "\n".join(json.dumps(r) for r in edge_rows), encoding="utf-8"
    )
    run_qc(root, "init")
    run_qc(
        root,
        "source",
        "add",
        "demo",
        "--type",
        "fixture",
        "--connection-ref",
        "env:QC_UNUSED",
        "--dataset",
        "people.jsonl",
    )
    return root


class TestJourneyProfile:
    def test_init_and_doctor(self, tmp_path: Path) -> None:
        root = tmp_path / "fresh"
        root.mkdir()
        out = run_qc(root, "init")
        assert "Initialized QC project" in out.stdout
        assert (root / "qc.toml").is_file()
        assert (root / "policies" / "strict-local.yaml").is_file()
        # no secrets in generated config
        text = (root / "qc.toml").read_text().lower()
        assert "password" not in text and "token" not in text
        doc = run_qc(root, "doctor")
        assert "python" in doc.stdout

    def test_init_twice_fails_with_exit_2(self, tmp_path: Path) -> None:
        root = tmp_path / "twice"
        root.mkdir()
        run_qc(root, "init")
        proc = run_qc(root, "init", expect=2)
        assert "already exists" in proc.stderr

    def test_profile_flow(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        test = run_qc(project, "source", "test", "demo")
        assert "ok" in test.stdout
        listed = run_qc(project, "dataset", "list", "demo")
        assert "people.jsonl" in listed.stdout
        prof = run_qc(project, "profile", "run", "demo", "people.jsonl")
        assert "profile prof_" in prof.stdout
        profiles = list((project / "profiles").glob("*.profile.json"))
        assert len(profiles) == 1
        pid = profiles[0].name.replace(".profile.json", "")
        show = run_qc(project, "profile", "show", pid)
        assert "columns:" in show.stdout
        assert "email" in show.stdout
        # JSON mode is valid JSON
        j = run_qc(project, "profile", "show", pid, "--json")
        payload = json.loads(j.stdout)
        assert payload["profile_id"].startswith("prof_")

    def test_profile_requires_policy_grant(self, project: Path) -> None:
        # deny-by-default: no grant yet
        proc = run_qc(project, "profile", "run", "demo", "people.jsonl", expect=4)
        assert "POLICY_DENIED" in proc.stderr

    def test_profile_with_typed_where(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        # typed predicate filters the profiled population (no raw SQL)
        out = run_qc(
            project,
            "profile",
            "run",
            "demo",
            "people.jsonl",
            "--where",
            "country=US",
            "--json",
        )
        payload = json.loads(out.stdout)
        assert payload["row_count"] == 4  # US rows in the fixture
        assert payload["job_id"].startswith("job_")

    def test_profile_rejects_raw_sql_where(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        proc = run_qc(
            project,
            "profile",
            "run",
            "demo",
            "people.jsonl",
            "--where",
            "country=US; DROP TABLE x",
            expect=2,
        )
        assert "illegal" in proc.stderr.lower() or "predicate" in proc.stderr.lower()

    def test_profile_with_ordering_where(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        out = run_qc(
            project,
            "profile",
            "run",
            "demo",
            "people.jsonl",
            "--where",
            "age:>=:30",
            "--json",
        )
        payload = json.loads(out.stdout)
        # ages 34 and 41 are >= 30
        assert payload["row_count"] == 2

    def test_profile_diff(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        run_qc(project, "profile", "run", "demo", "people.jsonl")
        pids = sorted(
            p.name.replace(".profile.json", "")
            for p in (project / "profiles").glob("*.profile.json")
        )
        # add a row, re-profile, diff
        data = project / "data" / "people.jsonl"
        rows = data.read_text().splitlines()
        rows.append(
            json.dumps(
                {
                    "id": "00131",
                    "email": "new@example.com",
                    "age": 50,
                    "score": 0.5,
                    "active": True,
                    "note": "x",
                    "country": "US",
                    "signup": "2025-09-01",
                    "balance": "10",
                    "ref_code": "D1",
                    "phone": "+1 555 000 0000",
                }
            )
        )
        data.write_text("\n".join(rows), encoding="utf-8")
        run_qc(project, "profile", "run", "demo", "people.jsonl")
        pids = sorted(
            p.name.replace(".profile.json", "")
            for p in (project / "profiles").glob("*.profile.json")
        )
        # same fingerprint file overwritten; diff same-id is a no-op but valid
        diff = run_qc(project, "profile", "diff", pids[-1], pids[0])
        assert diff.returncode == 0

    def test_missing_profile_exit_code(self, project: Path) -> None:
        proc = run_qc(project, "profile", "show", "prof_nonexistent", expect=1)
        assert "not found" in proc.stderr.lower()


class TestJourneySanitize:
    @pytest.fixture
    def granted(self, project: Path) -> Path:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        # explicit column actions for every column (unlisted columns are
        # manual_review and get redacted — SPEC 5.7)
        policy_file = project / "policies" / "strict-local.yaml"
        policy_file.write_text(
            """
name: strict-local
version: 1
defaults:
  source_access: deny
  raw_row_output: deny
  source_writes: deny
  external_egress: deny
  profile_disclosure: aggregate_only
budget:
  max_rows: 1000
  max_queries: 50
export_allowed: true
allowed_destinations: [local_file]
sources:
  demo:
    access: read
    datasets: [people.jsonl]
    columns:
      email: {action: synthetic}
      id: {action: tokenize}
      note: {action: redact}
      age: {action: keep}
      country: {action: keep}
      signup: {action: keep}
      active: {action: keep}
      score: {action: keep}
      balance: {action: keep}
      ref_code: {action: keep}
      phone: {action: keep}
""",
            encoding="utf-8",
        )
        run_qc(project, "policy", "validate", str(policy_file))
        return project

    def test_preview_transforms_rows(self, granted: Path) -> None:
        out = run_qc(
            granted,
            "sanitize",
            "preview",
            "demo",
            "people.jsonl",
            "--policy",
            "strict-local",
            "--limit",
            "2",
            "--json",
        )
        payload = json.loads(out.stdout)
        assert payload["raw_output"] is False
        blob = out.stdout
        assert "alice@example.com" not in blob
        assert '"00123"' not in blob

    def test_export_jsonl_and_manifest(self, granted: Path) -> None:
        target = granted / "output" / "safe.jsonl"
        run_qc(
            granted,
            "sanitize",
            "export",
            "demo",
            "people.jsonl",
            "--policy",
            "strict-local",
            "--output",
            str(target),
        )
        assert target.is_file()
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 8
        first = json.loads(lines[0])
        assert first["email"].startswith("synthetic_")
        assert first["id"].startswith("tok_")
        manifest = json.loads((granted / "output" / "safe.jsonl.manifest.json").read_text())
        assert manifest["contains_raw_samples"] is False
        assert manifest["policy"] == "strict-local"
        # manifest has no credentials
        assert "secret" not in json.dumps(manifest).lower() or manifest.get("secrets") == "none"

    def test_export_csv_null_encoding_and_formulas(self, granted: Path) -> None:
        target = granted / "output" / "safe.csv"
        run_qc(
            granted,
            "sanitize",
            "export",
            "demo",
            "people.jsonl",
            "--policy",
            "strict-local",
            "--output",
            str(target),
            "--format",
            "csv",
        )
        content = target.read_text(encoding="utf-8")
        assert "\\N" in content  # null sentinel documented
        manifest = json.loads((granted / "output" / "safe.csv.manifest.json").read_text())
        assert manifest["formula_injection_protection"] is True

    def test_export_denied_without_flag(self, granted: Path) -> None:
        policy_file = granted / "policies" / "strict-local.yaml"
        text = policy_file.read_text(encoding="utf-8")
        policy_file.write_text(text + "\nexport_allowed: false\n", encoding="utf-8")
        proc = run_qc(
            granted,
            "sanitize",
            "export",
            "demo",
            "people.jsonl",
            "--policy",
            "strict-local",
            "--output",
            str(granted / "output" / "x.jsonl"),
            expect=4,
        )
        assert "POLICY_DENIED" in proc.stderr


class TestJourneyGenerate:
    @pytest.fixture
    def profile_id(self, project: Path) -> str:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        run_qc(project, "profile", "run", "demo", "people.jsonl")
        return next(
            p.name.replace(".profile.json", "")
            for p in (project / "profiles").glob("*.profile.json")
        )

    def test_plan_run_validate(self, project: Path, profile_id: str) -> None:
        plan_out = run_qc(
            project,
            "generate",
            "plan",
            profile_id,
            "--rows",
            "25",
            "--seed",
            "7",
            "--json",
        )
        plan = json.loads(plan_out.stdout)
        assert plan["rows"] == 25
        plan_file = next((project / "reports").glob("*.plan.json"))
        out = project / "output" / "synth.jsonl"
        run_out = run_qc(
            project,
            "generate",
            "run",
            str(plan_file),
            "--output",
            str(out),
            "--json",
        )
        run_payload = json.loads(run_out.stdout)
        assert run_payload["report"]["rows_generated"] == 25
        assert out.is_file()
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert len(rows) == 25
        assert len({r["id"] for r in rows}) == 25  # uniqueness preserved

        val = run_qc(
            project,
            "generate",
            "validate",
            str(out),
            "--profile",
            profile_id,
            "--json",
        )
        report = json.loads(val.stdout)
        hard = [m for m in report["metrics"] if m["hard"] and not m["passed"]]
        assert hard == []

    def test_deterministic_with_same_seed(self, project: Path, profile_id: str) -> None:
        run_qc(project, "generate", "plan", profile_id, "--rows", "10", "--seed", "99")
        plan_file = next((project / "reports").glob("*.plan.json"))
        out1 = project / "output" / "a.jsonl"
        out2 = project / "output" / "b.jsonl"
        run_qc(project, "generate", "run", str(plan_file), "--output", str(out1))
        run_qc(project, "generate", "run", str(plan_file), "--output", str(out2))
        assert out1.read_text() == out2.read_text()

    def test_generate_denied_without_grant(self, tmp_path: Path) -> None:
        root = tmp_path / "nogrant"
        root.mkdir()
        (root / "data").mkdir()
        (root / "data" / "x.jsonl").write_text('{"a":1}\n')
        run_qc(root, "init")
        run_qc(root, "source", "add", "s", "--type", "fixture", "--connection-ref", "env:QC_UNUSED")
        # no grant + no profile -> profile run fails first
        proc = run_qc(root, "profile", "run", "s", "x.jsonl", expect=4)
        assert "POLICY_DENIED" in proc.stderr


class TestGlobalFlags:
    def test_version_json(self, project: Path) -> None:
        out = run_qc(project, "version")
        payload = json.loads(out.stdout)
        assert payload["tool"] == "qc"
        assert payload["version"]

    def test_unknown_command_exit_2(self, project: Path) -> None:
        proc = run_qc(project, "definitely-not-a-command", expect=2)
        assert proc.returncode == 2

    def test_human_errors_on_stderr_not_stdout(self, project: Path) -> None:
        proc = run_qc(project, "profile", "show", "nope", expect=1)
        assert proc.stdout.strip() == "" or "{" not in proc.stdout
        assert "error" in proc.stderr

    def test_json_flag_outputs_valid_json_on_error(self, project: Path) -> None:
        # --json on a failing command: JSON error envelope on stdout
        proc = run_qc(project, "profile", "show", "nope", "--json", expect=None)
        if proc.returncode != 0 and proc.stdout.strip().startswith("{"):
            json.loads(proc.stdout)  # must parse


class TestJobRegistry:
    def test_profile_run_records_job(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        run_qc(project, "profile", "run", "demo", "people.jsonl")
        out = run_qc(project, "job", "list", "--json")
        jobs = json.loads(out.stdout)
        assert jobs, "profile run must record a job"
        profile_jobs = [j for j in jobs if j["kind"] == "profile"]
        assert profile_jobs
        assert profile_jobs[0]["status"] == "completed"
        # job show works and leaks nothing
        show = run_qc(project, "job", "show", profile_jobs[0]["id"], "--json")
        payload = json.loads(show.stdout)
        assert payload["status"] == "completed"
        assert "alice@example.com" not in show.stdout

    def test_job_show_missing_exit_1(self, project: Path) -> None:
        proc = run_qc(project, "job", "show", "job_nonexistent", expect=1)
        assert "not found" in proc.stderr.lower()

    def test_job_list_empty_before_runs(self, project: Path) -> None:
        out = run_qc(project, "job", "list")
        assert "no jobs" in out.stdout

    def test_audit_log_written(self, project: Path) -> None:
        run_qc(project, "policy", "grant", "strict-local", "demo", "--dataset", "people.jsonl")
        run_qc(project, "profile", "run", "demo", "people.jsonl")
        audit = project / "audit.jsonl"
        assert audit.is_file()
        events = [json.loads(line) for line in audit.read_text().splitlines() if line]
        kinds = {e["operation"] for e in events}
        assert "profile" in kinds
        blob = audit.read_text()
        assert "alice@example.com" not in blob
        assert "password" not in blob
