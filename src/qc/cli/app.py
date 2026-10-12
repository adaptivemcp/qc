"""QC command-line interface (SPEC section 11).

Exit codes are stable: see :class:`qc.domain.errors.ExitCode`.
Human-readable errors go to stderr; ``--json`` output is valid JSON on
stdout.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from qc import TOOL_VERSION
from qc.cli.present import OutputOptions, arun, emit, handle_errors
from qc.domain.errors import ExitCode, InvalidArgumentsError, QCError

if TYPE_CHECKING:
    from qc.application.runtime import Runtime
    from qc.domain.models import DatasetRef

app = typer.Typer(
    name="qc",
    help="QueryCleaner/QC: profile, sanitize, and generate data with policy enforcement.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
source_app = typer.Typer(help="Manage data sources.", no_args_is_help=True)
dataset_app = typer.Typer(help="Explore datasets.", no_args_is_help=True)
profile_app = typer.Typer(help="Run and inspect profiles.", no_args_is_help=True)
policy_app = typer.Typer(help="Validate and apply policies.", no_args_is_help=True)
sanitize_app = typer.Typer(help="Sanitize previews and exports.", no_args_is_help=True)
generate_app = typer.Typer(help="Plan, run, and validate synthetic data.", no_args_is_help=True)
export_app = typer.Typer(help="Export outputs.", no_args_is_help=True)
job_app = typer.Typer(help="Inspect background jobs.", no_args_is_help=True)

app.add_typer(source_app, name="source")
app.add_typer(dataset_app, name="dataset")
app.add_typer(profile_app, name="profile")
app.add_typer(policy_app, name="policy")
app.add_typer(sanitize_app, name="sanitize")
app.add_typer(generate_app, name="generate")
app.add_typer(export_app, name="export")
app.add_typer(job_app, name="job")

# --------------------------------------------------------------- global options

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to qc.toml")]
RootOpt = Annotated[Path | None, typer.Option("--root", help="Project root (default: cwd)")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")]
QuietOpt = Annotated[bool, typer.Option("--quiet", "-q", help="Suppress non-essential output")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]
FormatOpt = Annotated[str, typer.Option("--format", help="Output format: text|json|csv")]


def _opts(json_output: bool, quiet: bool, verbose: bool, fmt: str) -> OutputOptions:
    opts = OutputOptions(as_json=json_output, quiet=quiet, verbose=verbose, format=fmt)
    opts.apply()
    return opts


def _runtime(root: Path | None, config: Path | None) -> Runtime:
    from qc.application.runtime import Runtime

    return Runtime(root=root, config_path=config)


# --------------------------------------------------------------- qc version


@app.command("version")
@handle_errors
def version_cmd(
    json_output: JsonOpt = False,
) -> None:
    """Print version information."""
    import platform

    payload = {
        "tool": "qc",
        "version": TOOL_VERSION,
        "python": platform.python_version(),
        "mcp_sdk": _pkg_version("mcp"),
        "faker": _pkg_version("faker"),
    }
    typer.echo(json.dumps(payload, indent=2))


def _pkg_version(name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return None


# ----------------------------------------------------------------- qc init


@app.command("init")
@handle_errors
def init_cmd(
    root: RootOpt = None,
    force: bool = typer.Option(False, "--force", help="Overwrite existing config"),
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Initialize a QC project: config, default policy, directories."""
    from qc.config.loader import Paths, default_config_toml
    from qc.config.policies import default_policy_yaml

    opts = _opts(json_output, quiet, verbose, "text")
    base = Path(root).expanduser().resolve() if root else Path.cwd()
    paths = Paths.for_root(base)

    if paths.config.exists() and not force:
        raise InvalidArgumentsError(
            f"{paths.config.name} already exists (use --force to overwrite)"
        )
    paths.ensure()
    paths.config.write_text(default_config_toml(), encoding="utf-8")
    with contextlib.suppress(OSError):
        paths.config.chmod(0o600)
    policy_file = paths.policies / "strict-local.yaml"
    if not policy_file.exists() or force:
        policy_file.write_text(default_policy_yaml(), encoding="utf-8")

    payload = {
        "root": str(base),
        "config": str(paths.config),
        "policy": str(policy_file),
        "directories": [
            str(paths.policies),
            str(paths.profiles),
            str(paths.reports),
            str(paths.output),
        ],
    }
    emit(payload, opts=opts, text=f"Initialized QC project at {base}")


# ------------------------------------------------------------- qc source *


@source_app.command("add")
@handle_errors
def source_add(
    name: str,
    type: str = typer.Option(
        ..., "--type", help="postgres|mysql|maxcompute|google-sheets|lark-sheets|fixture"
    ),
    connection_ref: str = typer.Option(
        ...,
        "--connection-ref",
        help="Secret reference: env:VAR | file:path | keyring:svc/user | cmd:program",
    ),
    default_schema: str | None = typer.Option(None, "--schema"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    dataset: Annotated[
        list[str], typer.Option("--dataset", help="Allowed dataset (repeatable)")
    ] = [],
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Register a source using a secret reference (never inline credentials)."""
    from qc.domain.models import SourceConfig

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    cfg = SourceConfig(
        name=name,
        connector=type,
        connection_ref=connection_ref,
        default_schema=default_schema,
        allowed_datasets=list(dataset),
    )
    rt.sources.add(cfg)
    emit(cfg, opts=opts, text=f"Added source '{name}' ({type})")


@source_app.command("list")
@handle_errors
def source_list(
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """List configured sources (never credentials)."""
    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    sources = rt.sources.list()
    lines = [f"{s.name}\t{s.connector}\t{s.connection_ref.split(':', 1)[0]}:***" for s in sources]
    emit(
        [s.json_safe() for s in sources],
        opts=opts,
        text="\n".join(lines) if lines else "no sources configured",
    )


@source_app.command("test")
@handle_errors
def source_test(
    name: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Test connectivity to a source."""
    from qc.domain.enums import OperationKind

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    cfg = rt.source(name)
    engine = rt.engine()
    from qc.security.policy import DecisionContext, Operation

    engine.require(Operation.SOURCE_ACCESS, DecisionContext(source=name))

    async def _run() -> Any:
        connector = rt.connector_for(cfg)
        try:
            return await connector.test_connection(cfg)
        finally:
            await connector.close()

    result = arun(_run())
    rt.audit.record(
        OperationKind.SOURCE_TEST,
        source_id=cfg.id,
        decision="allow" if result.ok else "deny",
        result_status="ok" if result.ok else "error",
        policy_version=rt.load_policy_named().version,
    )
    if not result.ok:
        raise QCError(result.message)
    emit(result, opts=opts, text=f"{name}: ok ({result.message})")


@source_app.command("inspect")
@handle_errors
def source_inspect(
    name: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Show source configuration with the secret reference redacted."""
    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    cfg = rt.source(name)
    data = cfg.json_safe()
    scheme = str(data["connection_ref"]).split(":", 1)[0]
    data["connection_ref"] = f"{scheme}:***"
    emit(data, opts=opts, text=json.dumps(data, indent=2))


# ------------------------------------------------------------- qc dataset *


@dataset_app.command("list")
@handle_errors
def dataset_list(
    source: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """List datasets visible to a source under policy."""
    from qc.security.policy import DecisionContext, Operation

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    cfg = rt.source(source)
    engine = rt.engine()
    engine.require(Operation.SOURCE_ACCESS, DecisionContext(source=source))

    async def _run() -> Any:
        connector = rt.connector_for(cfg)
        try:
            return await connector.list_datasets(cfg)
        finally:
            await connector.close()

    datasets = arun(_run())
    # policy filter: only permitted datasets reach output
    permitted = [
        d for d in datasets if not cfg.allowed_datasets or d.qualified_name in cfg.allowed_datasets
    ]
    emit(
        [d.json_safe() for d in permitted],
        opts=opts,
        text="\n".join(d.qualified_name for d in permitted) or "no datasets",
    )


@dataset_app.command("schema")
@handle_errors
def dataset_schema(
    source: str,
    dataset: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Show schema metadata for one dataset."""
    from qc.security.policy import DecisionContext, Operation

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    cfg = rt.source(source)
    engine = rt.engine()
    engine.require(Operation.DATASET_READ, DecisionContext(source=source, dataset=dataset))
    ref = _parse_dataset_ref(cfg, dataset)

    async def _run() -> Any:
        connector = rt.connector_for(cfg)
        try:
            return await connector.get_schema(cfg, ref)
        finally:
            await connector.close()

    schema = arun(_run())
    emit(schema, opts=opts, text=_format_schema(schema))


def _parse_dataset_ref(cfg: Any, dataset: str) -> DatasetRef:
    from qc.domain.models import DatasetRef

    if "." in dataset and not dataset.endswith((".jsonl", ".csv")):
        ns, _, name = dataset.rpartition(".")
        return DatasetRef(source_id=cfg.id, namespace=ns, name=name, locator=dataset)
    return DatasetRef(source_id=cfg.id, name=dataset, locator=dataset)


def _format_schema(schema: Any) -> str:
    lines = [
        f"dataset: {schema.dataset.qualified_name}",
        f"fingerprint: {schema.source_fingerprint}",
        f"rows: {schema.row_estimate} ({schema.row_estimate_method.value})",
        "columns:",
    ]
    for c in schema.columns:
        null = (
            "?" if c.nullable_declared is None else ("null" if c.nullable_declared else "not null")
        )
        pk = " PK" if c.is_primary_key else ""
        lines.append(f"  {c.ordinal:>3} {c.name:<30} {c.source_type:<20} {null}{pk}")
    if schema.foreign_keys:
        lines.append("foreign keys:")
        for fk in schema.foreign_keys:
            lines.append(
                f"  ({', '.join(fk.columns)}) -> {fk.ref_name}({', '.join(fk.ref_columns)})"
            )
    return "\n".join(lines)


# ------------------------------------------------------------- qc profile *


@profile_app.command("run")
@handle_errors
def profile_run(
    source: str,
    dataset: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    max_queries: int | None = typer.Option(None, "--max-queries"),
    max_rows: int | None = typer.Option(None, "--max-rows"),
    max_bytes: int | None = typer.Option(None, "--max-bytes"),
    max_runtime: float | None = typer.Option(None, "--max-runtime"),
    sample_size: int = typer.Option(1000, "--sample-size"),
    columns: Annotated[list[str], typer.Option("--column", help="Restrict to column")] = [],
    sensitivity_scan: bool = typer.Option(True, "--sensitivity-scan/--no-sensitivity-scan"),
    allow_full_scan: bool = typer.Option(
        False, "--allow-full-scan", help="Explicitly allow expensive full scans"
    ),
    profile_output: Path | None = typer.Option(None, "--profile-output"),
    policy: str | None = typer.Option(None, "--policy"),
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Run adaptive profiling under configured budgets."""
    from qc.artifacts.profiles import save_profile
    from qc.domain.enums import Budget, OperationKind
    from qc.profiling.engine import Profiler, ProfileSettings

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    cfg = rt.source(source)
    engine = rt.engine(policy)
    ref = _parse_dataset_ref(cfg, dataset)

    settings = ProfileSettings(
        sample_size=sample_size,
        columns=list(columns) or None,
        budget=Budget(
            max_queries=max_queries,
            max_rows=max_rows,
            max_bytes=max_bytes,
            max_runtime_seconds=max_runtime,
        ),
        allow_full_scan=allow_full_scan,
        sensitivity_scan=sensitivity_scan,
    )

    async def _run() -> Any:
        connector = rt.connector_for(cfg)
        try:
            profiler = Profiler(connector, engine, ceiling=rt.ceiling)
            return await profiler.profile(cfg, ref, settings)
        finally:
            await connector.close()

    result = arun(_run())
    artifact = result.artifact
    out_path = Path(profile_output) if profile_output else rt.paths.profiles
    saved = save_profile(out_path, artifact)
    rt.state.index_profile(artifact, saved)
    rt.audit.record(
        OperationKind.PROFILE,
        source_id=cfg.id,
        dataset_id=dataset,
        policy_version=rt.load_policy_named(policy).version,
        decision="allow",
        rows_affected=result.budget_usage.rows,
        duration_seconds=artifact.duration_seconds,
        result_status="ok",
        detail={"profile_id": artifact.profile_id, "queries": str(result.budget_usage.queries)},
    )
    emit(
        {
            "profile_id": artifact.profile_id,
            "path": str(saved),
            "row_count": artifact.row_count,
            "columns": len(artifact.columns),
            "queries_used": result.budget_usage.queries,
            "warnings": artifact.warnings,
            "incomplete_checks": artifact.incomplete_checks,
            "confidence_summary": artifact.confidence_summary,
        },
        opts=opts,
        text=(
            f"profile {artifact.profile_id} saved to {saved}\n"
            f"rows: {artifact.row_count} | columns: {len(artifact.columns)} | "
            f"queries: {result.budget_usage.queries} | "
            f"confidence: {artifact.confidence_summary}"
            + (f"\nwarnings: {'; '.join(artifact.warnings)}" if artifact.warnings else "")
        ),
    )


@profile_app.command("show")
@handle_errors
def profile_show(
    profile_id: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    column: Annotated[list[str], typer.Option("--column", help="Filter to column")] = [],
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Show a saved profile with measured/inferred labels."""
    from qc.artifacts.profiles import find_profile, load_profile

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    path = rt.state.find_profile_index(profile_id) or find_profile(rt.paths.profiles, profile_id)
    artifact = load_profile(path)
    if column:
        wanted = set(column)
        artifact.columns = [c for c in artifact.columns if c.name in wanted]
        if not artifact.columns:
            raise InvalidArgumentsError(f"no columns matched: {sorted(wanted)}")
    emit(artifact, opts=opts, text=_format_profile(artifact))


def _format_profile(art: Any) -> str:
    lines = [
        f"profile: {art.profile_id}",
        f"dataset: {art.dataset.qualified_name} ({art.tool_version}/{art.algorithm_version})",
        f"rows: {art.row_count} ({art.row_count_measurement.value})",
        f"confidence: {art.confidence_summary}",
        f"budget: {art.budget_usage}",
        "columns:",
    ]
    for c in art.columns:
        distinct = str(c.distinct_count) if c.distinct_count is not None else "?"
        meas = c.distinct_measurement.value
        nulls = c.falsy.null_count if c.falsy.null_count is not None else "?"
        sens = ",".join(sorted({f.label.value for f in c.sensitivity})) or "-"
        lines.append(
            f"  {c.name:<28} {c.logical_type.value:<18} conf={c.confidence.value:<7} "
            f"nulls={nulls} distinct={distinct}({meas}) sens={sens}"
        )
    if art.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in art.warnings)
    if art.incomplete_checks:
        lines.append(f"incomplete: {', '.join(art.incomplete_checks)}")
    return "\n".join(lines)


@profile_app.command("diff")
@handle_errors
def profile_diff(
    profile_a: str,
    profile_b: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Diff two profiles: schema, type, and null-rate changes."""
    from qc.artifacts.profiles import find_profile, load_profile

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    a = load_profile(
        rt.state.find_profile_index(profile_a) or find_profile(rt.paths.profiles, profile_a)
    )
    b = load_profile(
        rt.state.find_profile_index(profile_b) or find_profile(rt.paths.profiles, profile_b)
    )
    changes: list[dict[str, Any]] = []
    cols_a = {c.name: c for c in a.columns}
    cols_b = {c.name: c for c in b.columns}
    for name in sorted(set(cols_a) | set(cols_b)):
        if name not in cols_a:
            changes.append({"column": name, "change": "added"})
        elif name not in cols_b:
            changes.append({"column": name, "change": "removed"})
        else:
            ca, cb = cols_a[name], cols_b[name]
            if ca.logical_type != cb.logical_type:
                changes.append(
                    {
                        "column": name,
                        "change": "type",
                        "from": ca.logical_type.value,
                        "to": cb.logical_type.value,
                    }
                )
            na = _null_rate(ca)
            nb = _null_rate(cb)
            if na is not None and nb is not None and abs(na - nb) > 0.05:
                changes.append(
                    {
                        "column": name,
                        "change": "null_rate",
                        "from": round(na, 4),
                        "to": round(nb, 4),
                    }
                )
    if a.row_count != b.row_count:
        changes.append({"change": "row_count", "from": a.row_count, "to": b.row_count})
    emit(
        {"from": a.profile_id, "to": b.profile_id, "changes": changes},
        opts=opts,
        text="\n".join(
            f"{c.get('column', '*')}: {c['change']}"
            + (f" {c.get('from')} -> {c.get('to')}" if "from" in c else "")
            for c in changes
        )
        or "no changes",
    )


def _null_rate(col: Any) -> float | None:
    null_count = col.falsy.null_count
    if null_count is None or col.row_count_seen == 0:
        return None
    rate: float = null_count / col.row_count_seen
    return rate


# ------------------------------------------------------------- qc policy *


@policy_app.command("list")
@handle_errors
def policy_list(
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """List available policies."""
    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    names = sorted(p.stem for p in rt.paths.policies.glob("*.yaml"))
    if not names:
        names = [rt.config.default_policy + " (built-in)"]
    emit(names, opts=opts, text="\n".join(names))


@policy_app.command("validate")
@handle_errors
def policy_validate(
    file: Path,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Validate a policy file (schema + semantics)."""
    from qc.config.policies import validate_policy_file

    opts = _opts(json_output, quiet, verbose, "text")
    policy = validate_policy_file(file)
    emit(
        {"valid": True, "name": policy.name, "version": policy.version},
        opts=opts,
        text=f"policy '{policy.name}' is valid",
    )


@policy_app.command("grant")
@handle_errors
def policy_grant(
    policy: str,
    source: str,
    dataset: Annotated[list[str], typer.Option("--dataset", help="Dataset to grant (repeatable)")],
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Grant a source read access to specific datasets in a policy file.

    Deny-by-default: this is the explicit authorization step required
    before any profiling or preview (SPEC 5.7).
    """
    import yaml

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    path = rt.policy_path(policy)
    if not path.is_file():
        from qc.config.policies import default_policy_yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_policy_yaml(), encoding="utf-8")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = raw.setdefault("sources", {})
    entry = sources.setdefault(source, {"access": "deny", "datasets": []})
    existing = set(entry.get("datasets", []))
    added = [d for d in dataset if d not in existing]
    entry["access"] = "read"
    entry.setdefault("datasets", []).extend(added)
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    # validate after mutation so a bad grant fails loudly
    from qc.config.policies import validate_policy_file

    validate_policy_file(path)
    emit(
        {"policy": policy, "source": source, "granted": added},
        opts=opts,
        text=f"granted {source}: {', '.join(added) or 'no new datasets'} in '{policy}'",
    )


@policy_app.command("apply")
@handle_errors
def policy_apply(
    policy: str,
    dataset: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Show how a policy applies to a dataset (read-only preview)."""
    from qc.security.policy import DecisionContext, Operation

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    engine = rt.engine(policy)
    source_name, _, _ = dataset.partition(".")
    decision = engine.check(
        Operation.DATASET_READ, DecisionContext(source=source_name, dataset=dataset)
    )
    emit(
        {
            "policy": policy,
            "dataset": dataset,
            "verdict": decision.verdict,
            "reason": decision.reason,
            "checks": decision.checks,
        },
        opts=opts,
        text=f"{dataset} under '{policy}': {decision.verdict} ({decision.reason})",
    )


# --------------------------------------------------------- qc sanitize *


def _sanitize_common(
    source: str,
    dataset: str,
    policy_name: str,
    limit: int,
    columns: list[str],
) -> tuple[Runtime, Any, Any, list[str]]:
    """Shared setup for preview/export: runtime, engine, connector, actions."""
    from qc.security.policy import DecisionContext, Operation

    rt = _runtime(None, None)
    cfg = rt.source(source)
    engine = rt.engine(policy_name)
    ctx = DecisionContext(source=source, dataset=dataset)
    engine.require(Operation.ROW_OUTPUT, ctx)
    return rt, cfg, engine, list(columns)


@sanitize_app.command("preview")
@handle_errors
def sanitize_preview(
    source: str,
    dataset: str,
    policy: str = typer.Option(..., "--policy"),
    limit: int = typer.Option(20, "--limit", min=1),
    columns: Annotated[list[str], typer.Option("--column")] = [],
    include_null_patterns: bool = typer.Option(False, "--include-null-patterns"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Generate a policy-transformed preview (raw rows denied by default)."""
    from qc.application.sanitize import preview_rows

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    result = arun(
        preview_rows(
            rt,
            source=source,
            dataset=dataset,
            policy_name=policy,
            limit=limit,
            columns=list(columns) or None,
            include_null_patterns=include_null_patterns,
            dry_run=dry_run,
        )
    )
    emit(
        result,
        opts=opts,
        text=(
            f"{len(result['rows'])} transformed row(s) "
            f"(policy: {result['policy']}, raw: {result['raw_output']})"
            + (f"\nblocked columns: {', '.join(result['blocked'])}" if result["blocked"] else "")
            + (
                "\n" + json.dumps(result["rows"], indent=2, ensure_ascii=False)
                if not opts.quiet
                else ""
            )
        ),
    )


@sanitize_app.command("export")
@handle_errors
def sanitize_export(
    source: str,
    dataset: str,
    policy: str = typer.Option(..., "--policy"),
    output: Path = typer.Option(..., "--output", help="Output file path"),
    format: str = typer.Option("jsonl", "--format", help="jsonl|csv"),
    limit: int | None = typer.Option(None, "--limit"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    manifest: bool = typer.Option(True, "--manifest/--no-manifest"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Export sanitized rows to JSONL or CSV with a manifest."""
    from qc.application.sanitize import export_rows

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    result = arun(
        export_rows(
            rt,
            source=source,
            dataset=dataset,
            policy_name=policy,
            output=Path(output),
            fmt=format,
            limit=limit,
            dry_run=dry_run,
            write_manifest=manifest,
        )
    )
    emit(
        result,
        opts=opts,
        text=(
            f"{'would write' if dry_run else 'wrote'} {result['rows']} row(s) "
            f"to {result['output']}"
            + (f"\nmanifest: {result['manifest']}" if result.get("manifest") else "")
        ),
    )


# ----------------------------------------------------------- qc generate *


@generate_app.command("plan")
@handle_errors
def generate_plan(
    profile_id: str,
    rows: int = typer.Option(..., "--rows", min=1),
    seed: int = typer.Option(0, "--seed"),
    locale: str = typer.Option("en_US", "--locale"),
    output: Path | None = typer.Option(None, "--output"),
    policy: str | None = typer.Option(None, "--policy"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Create a generation plan from a profile."""
    from qc.application.generation import create_plan

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    plan = create_plan(
        rt,
        profile_ref=profile_id,
        rows=rows,
        seed=seed,
        locale=locale,
        policy_name=policy,
        output=output,
    )
    emit(
        plan,
        opts=opts,
        text=f"plan {plan.plan_id}: {rows} rows, seed={seed} -> {plan.output_format}",
    )


@generate_app.command("run")
@handle_errors
def generate_run(
    plan_file: Path,
    output: Path | None = typer.Option(None, "--output"),
    report: Path | None = typer.Option(None, "--report"),
    seed: int | None = typer.Option(None, "--seed"),
    strict: bool = typer.Option(False, "--strict", help="Fail on soft fidelity misses"),
    allow_fallbacks: bool = typer.Option(False, "--allow-fallbacks"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Run a generation plan and write output + validation report."""
    from qc.application.generation import run_generation

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    result = arun(
        run_generation(
            rt,
            plan_file=Path(plan_file),
            output=Path(output) if output else None,
            report_path=Path(report) if report else None,
            seed=seed,
            strict=strict,
            allow_fallbacks=allow_fallbacks,
        )
    )
    rep = result["report"]
    if result["ok"]:
        emit(
            {"report": rep.json_safe(), "output": result["output"]},
            opts=opts,
            text=(
                f"generated {rep.rows_generated} row(s) -> {result['output']}\n"
                f"hard failures: {len(rep.hard_failures)} | "
                f"soft deviations: {len(rep.soft_failures)}\n"
                f"warnings: {'; '.join(rep.warnings[:2])}"
            ),
        )
        return
    text = f"generation failed: {len(rep.hard_failures)} hard constraint failure(s)\n" + "\n".join(
        f"  - {m.name}: {m.observed} vs {m.generated}" for m in rep.hard_failures
    )
    emit(rep, opts=opts, text=text)
    raise typer.Exit(code=ExitCode.VALIDATION_FAILURE)


@generate_app.command("validate")
@handle_errors
def generate_validate(
    output: Path,
    profile: str = typer.Option(..., "--profile"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Validate a generated file against its profile."""
    from qc.application.generation import validate_output

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    report = arun(validate_output(rt, output=Path(output), profile_ref=profile))
    emit(
        report,
        opts=opts,
        text=(
            f"validation {'FAILED' if report.hard_failures else 'passed'}: "
            f"{len(report.metrics)} metrics, {len(report.hard_failures)} hard, "
            f"{len(report.soft_failures)} soft failures"
        ),
    )
    if report.hard_failures:
        raise typer.Exit(code=ExitCode.VALIDATION_FAILURE)


# ------------------------------------------------------------- qc export *


@export_app.command("database")
@handle_errors
def export_database(
    output: Path,
    destination: str = typer.Option(..., "--destination", help="Destination source name"),
    table: str = typer.Option(..., "--table"),
    policy: str | None = typer.Option(None, "--policy"),
    mode: str = typer.Option("create", "--mode", help="create|append|replace"),
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Write a generated/sanitized file to an approved destination database."""
    from qc.application.exports import export_to_database

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    result = arun(
        export_to_database(
            rt,
            output=Path(output),
            destination=destination,
            table=table,
            policy_name=policy,
            mode=mode,
        )
    )
    emit(result, opts=opts, text=f"wrote {result['rows']} row(s) to {destination}.{table}")


# -------------------------------------------------------------- qc job *


@job_app.command("list")
@handle_errors
def job_list(
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """List recorded jobs."""
    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    jobs = rt.state.list_jobs()
    emit(
        [j.json_safe() for j in jobs],
        opts=opts,
        text="\n".join(f"{j.id}\t{j.kind}\t{j.status.value}" for j in jobs) or "no jobs",
    )


@job_app.command("show")
@handle_errors
def job_show(
    job_id: str,
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Show one job."""
    from qc.domain.errors import NotFoundError

    opts = _opts(json_output, quiet, verbose, "text")
    rt = _runtime(root, config)
    job = rt.state.get_job(job_id)
    if job is None:
        raise NotFoundError(f"job not found: {job_id}")
    emit(job, opts=opts, text=json.dumps(job.json_safe(), indent=2))


@app.command("doctor")
@handle_errors
def doctor_cmd(
    root: RootOpt = None,
    config: ConfigOpt = None,
    json_output: JsonOpt = False,
    quiet: QuietOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Environment and configuration diagnostics (secrets removed)."""
    from qc.connectors.registry import supported_connectors

    opts = _opts(json_output, quiet, verbose, "text")
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "status": "ok" if ok else "warn", "detail": detail})

    import platform

    check("python", platform.python_version() >= "3.12", platform.python_version())
    rt_ok = True
    try:
        rt = _runtime(root, config)
        check(
            "config",
            rt.paths.config.is_file(),
            str(rt.paths.config)
            if rt.paths.config.is_file()
            else "qc.toml not found (defaults used)",
        )
        try:
            pol = rt.load_policy_named()
            check("policy", True, f"loaded '{pol.name}' v{pol.version}")
        except QCError as exc:
            check("policy", False, exc.message)
        check("state", True, f"state db {rt.paths.state_db.name}")
        try:
            rt.sources.list()
            check("sources", True, f"{len(rt.sources.list())} configured")
        except QCError as exc:
            check("sources", False, exc.message)
        rt.close()
    except QCError as exc:
        rt_ok = False
        check("runtime", False, exc.message)

    for name, importable in supported_connectors().items():
        check(
            f"connector:{name}", importable, "available" if importable else "driver not installed"
        )

    # secrets sanity: env resolver works
    import os

    env_ready = any(k.startswith("QC_") for k in os.environ)
    check("secret_env", True, "env: resolver available" if env_ready else "env: resolver ready")

    failed = [c for c in checks if c["status"] != "ok" and "not installed" not in c["detail"]]
    payload = {"checks": checks, "healthy": not failed or rt_ok}
    lines = [
        f"{'ok  ' if c['status'] == 'ok' else 'warn'} {c['check']}: {c['detail']}" for c in checks
    ]
    emit(payload, opts=opts, text="\n".join(lines))
    if not rt_ok:
        raise typer.Exit(code=ExitCode.INVALID_ARGUMENTS)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
