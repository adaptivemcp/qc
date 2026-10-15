# SPEC.md

## 1. Project summary

**Working name:** QueryCleaner/QC (placeholder; rename before public release)

**Purpose:** Build a local-first data profiling, sanitization, and
synthetic-data generation system that exposes a consistent CLI and MCP
interface. It connects to relational databases and spreadsheet
providers, learns schema and observed data behavior through bounded
adaptive profiling, and produces sanitized or synthetic datasets
suitable for development, testing, documentation, and AI-agent context.

The product has two primary interfaces:

1.  **CLI** for interactive discovery, profiling, policy authoring,
    generation, validation, and batch jobs.
2.  **MCP server** for exposing approved operations to AI clients over
    stdio or Streamable HTTP.

Both interfaces call the same Python application services and domain
engine. A small JVM sidecar provides JDBC connectivity for drivers that
are impractical to use natively from Python, especially Alibaba
MaxCompute JDBC.

### 1.1 Goals

-   Connect to PostgreSQL, MySQL, Alibaba MaxCompute through its JDBC
    driver, Google Sheets, and Lark Sheets.
-   Discover schemas, column metadata, constraints, and spreadsheet
    structure.
-   Learn value formats, distributions, null and falsy-value behavior,
    outliers, rare values, and cross-column relationships using
    adaptive, budget-aware queries.
-   Detect likely sensitive fields and enforce explicit policies before
    data leaves the source boundary.
-   Support deterministic masking and hybrid synthetic generation using
    Faker, inferred formats, distributions, constraints, and
    relationships.
-   Validate generated data against learned profiles and report
    deviations.
-   Export JSONL, CSV, or synthetic records to a database.
-   Make profiling results and generation decisions inspectable,
    versioned, and reproducible.
-   Give MCP clients narrowly scoped tools with predictable output and
    privacy-safe defaults.
-   Run locally by default, with optional remote MCP hosting under
    explicit authentication and transport security.

### 1.2 Non-goals for the initial release

-   A general-purpose ETL or data integration platform.
-   A universal data catalog or governance portal.
-   Automatic production-data writes or in-place source mutation.
-   Guaranteed anonymization against every possible adversary.
-   Training a foundation model on source data.
-   Sending raw source records to a hosted LLM by default.
-   Automatically inferring business meaning with certainty.
-   A general-purpose spreadsheet editor.
-   Full support for every JDBC database in v1. The connector interface
    should permit later additions.

### 1.3 Core principles

-   **Read-only source access by default.**
-   **Privacy before convenience.** Raw values remain inside the trusted
    process unless a policy explicitly authorizes disclosure.
-   **Bounded work.** Every profiling operation has row, time, query,
    and byte budgets.
-   **Evidence-backed profiles.** Distinguish observed facts, inferred
    rules, and uncertain hypotheses.
-   **Deterministic reproduction.** Persist algorithm versions,
    configuration, and seeds where applicable.
-   **Explicit policy.** Sensitive-field handling and egress require
    clear policy decisions.
-   **Connector isolation.** Source-specific quirks stay in adapters.
-   **Validate every output.** Generation is complete only after
    validation and a report.

## 2. Terminology

-   **Source:** A configured database, spreadsheet, or other readable
    data system.
-   **Dataset:** A table, view, sheet, named range, or selected query
    result.
-   **Profile:** A versioned summary of schema and observed data
    properties.
-   **Observation:** Evidence collected from metadata, aggregate
    queries, or sampled records.
-   **Rule:** A confirmed or inferred constraint or pattern.
-   **Sensitive field:** A field that may contain personal,
    confidential, credential, or otherwise restricted data.
-   **Sanitization:** Transformation of source data through masking,
    redaction, generalization, tokenization, or synthetic replacement.
-   **Synthetic dataset:** Generated records designed to preserve
    selected statistical and structural properties without copying
    source records.
-   **Policy:** A declarative set of allowed operations, field handling,
    budgets, and disclosure rules.
-   **Egress:** Data leaving the local trusted boundary, including MCP
    responses, files, logs, traces, and external model calls.
-   **Profile confidence:** A calibrated indication of how strongly
    collected evidence supports an inferred property. It must not be
    presented as a mathematical probability unless the algorithm
    provides one.

## 3. User journeys

### 3.1 Profile a database table

1.  Configure a connection using a secret reference.
2.  Test connectivity and discover accessible datasets.
3.  Select a table or view.
4.  Run adaptive profiling under configured budgets.
5.  Review the profile, sensitive-field warnings, and unresolved
    questions.
6.  Save the profile artifact with its source fingerprint and algorithm
    version.

### 3.2 Prepare safe AI context

1.  Select a dataset and profile.
2.  Choose a policy that controls columns, masking, row limits, and
    output format.
3.  Generate a sanitized preview.
4.  Validate the preview and inspect the transformation report.
5.  Export the approved result or expose it through MCP.

### 3.3 Generate synthetic data

1.  Select one or more related datasets and their profiles.
2.  Choose row counts, seed, relationship-preservation mode, and output
    destination.
3.  Generate values from column strategies and joint constraints.
4.  Validate types, null rates, formats, uniqueness, referential
    integrity, and statistical tolerances.
5.  Review warnings and export JSONL, CSV, or to a separate destination
    database.

### 3.4 Use from an AI agent

1.  The agent lists approved sources and datasets.
2.  The agent requests a profile or a sanitized preview.
3.  Policy enforcement occurs in the server before source data is read
    into an MCP response.
4.  The server returns a bounded, structured result with provenance and
    warnings.
5.  Any operation that writes data, changes policy, or expands access
    requires explicit authorization and is unavailable by default.

## 4. Architecture

### 4.1 High-level components

``` text
                 ┌─────────────────────┐
                 │       CLI           │
                 └──────────┬──────────┘
                            │
                 ┌──────────▼──────────┐
                 │ Application services│
                 │ use cases / policy  │
                 └──────┬────────┬─────┘
                        │        │
             ┌──────────▼──┐  ┌──▼────────────┐
             │ Domain core │  │ MCP adapters  │
             │ profile /   │  │ stdio / HTTP  │
             │ sanitize /  │  └───────────────┘
             │ generate    │
             └──────┬──────┘
                    │
        ┌───────────▼─────────────┐
        │ Connector interfaces    │
        └──────┬──────┬──────┬────┘
               │      │      │
          Python DB  Sheets  JVM JDBC sidecar
          adapters   APIs    (MaxCompute first)
```

### 4.2 Modules

-   `domain/`: source-independent types, profiles, observations,
    constraints, policies, reports, and generation plans.
-   `application/`: use cases, orchestration, policy checks, budgets,
    and transaction boundaries.
-   `profiling/`: metadata discovery, samplers, adaptive query planner,
    detectors, distribution summaries, relationship discovery, and
    confidence calculation.
-   `sanitization/`: classification, masking, redaction, generalization,
    deterministic tokenization, and disclosure checks.
-   `generation/`: strategy registry, Faker providers, format
    generators, distribution samplers, relational generation, and
    validation.
-   `connectors/`: PostgreSQL, MySQL, Google Sheets, and Lark Sheets
    adapters.
-   `jdbc_bridge/`: JVM sidecar and protocol client for JDBC-backed
    sources.
-   `artifacts/`: versioned profile and policy serialization, artifact
    validation, and migration.
-   `exports/`: JSONL, CSV, and destination database writers.
-   `mcp/`: MCP tool schemas, resource templates if useful,
    authorization mapping, and transport setup.
-   `cli/`: Typer-based commands and terminal presentation.
-   `security/`: secret resolution, redaction, policy evaluation,
    authentication, authorization, audit events, and egress controls.
-   `observability/`: structured logs, metrics, operation IDs, and
    diagnostic reports.
-   `tests/`: unit, property-based, connector contract, integration,
    security, and end-to-end tests.

### 4.3 Runtime model

The Python process owns application logic. The JVM sidecar owns JDBC
driver loading, connection lifecycle, JDBC metadata access,
parameterized query execution, and result serialization. Communication
should use newline-delimited JSON over local stdio or a local
authenticated socket. The first implementation may use a subprocess with
stdin/stdout pipes.

Rules for the bridge:

-   stdout carries protocol messages only; diagnostics go to stderr.
-   Requests and responses carry protocol version, request ID, status,
    and structured errors.
-   No SQL is accepted from the bridge without validation by the Python
    connector.
-   Query parameters are passed separately from SQL.
-   Secrets are passed through an inherited protected environment, file
    descriptor, or short-lived local secret channel; avoid command-line
    arguments.
-   Apply request deadlines, maximum result bytes, maximum rows, and
    cancellation.
-   Pin and verify the JDBC driver version and checksum.
-   Do not log query result values or connection strings.
-   Treat the bridge as a trusted local process and restrict its
    filesystem and network access where practical.

### 4.4 Technology choices

-   Python 3.12 or later, pinned and tested in CI.
-   `pyproject.toml` with a lockfile and reproducible builds.
-   Typer for CLI.
-   Official MCP Python SDK, pinned to a tested version.
-   Pydantic for boundary validation and artifact schemas.
-   SQLAlchemy Core or a small DB-API abstraction for PostgreSQL and
    MySQL.
-   `psycopg` for PostgreSQL; `mysql-connector-python` or PyMySQL for
    MySQL, selected after connector tests.
-   Java 17 or the minimum version supported by the selected MaxCompute
    JDBC driver.
-   Faker for locale-aware fake values.
-   Hypothesis for property-based tests.
-   pytest for test execution.
-   `cryptography` only if encrypted local artifact storage is
    implemented; use established OS credential stores or injected secret
    managers for credentials.
-   JSON as the canonical artifact serialization format.
-   SQLite for optional local state and job history; source records and
    raw samples must never be persisted there by default.

Exact dependency versions must be pinned in the lockfile and reviewed
during release.

## 5. Domain model

All domain models require explicit schema versions. Use UTC timestamps
in ISO 8601 form. IDs should be opaque UUIDs or stable hashes where
appropriate.

### 5.1 SourceConfig

``` json
{
  "id": "source_opaque_id",
  "name": "analytics-readonly",
  "connector": "postgres",
  "connection_ref": "env:DATA_SOURCE_ANALYTICS",
  "default_schema": "public",
  "read_only": true,
  "allowed_datasets": ["public.orders", "public.customers"],
  "policy_ref": "policy_default",
  "created_at": "2026-01-01T00:00:00Z"
}
```

`connection_ref` points to a secret resolver. Plaintext passwords,
tokens, and full connection URLs with credentials must never be written
to config files or artifacts.

### 5.2 DatasetRef

Fields: `source_id`, `namespace`, `name`, `kind` (`table`, `view`,
`sheet`, `named_range`, `query_result`), optional stable provider ID,
and a connector-specific opaque locator. MCP clients should receive only
datasets permitted by policy.

### 5.3 DatasetSchema

-   Dataset identity and source fingerprint.
-   Ordered columns.
-   Primary-key candidates and declared keys.
-   Foreign-key relationships where available.
-   Constraints and defaults.
-   Source type and normalized logical type.
-   Schema version and discovery timestamp.

### 5.4 ColumnProfile

Required fields:

-   `column_id`, `name`, `ordinal`.
-   `source_type`, `logical_type`, and `type_confidence`.
-   `nullable_declared` and `null_count` when known.
-   `row_count_seen`, `distinct_estimate`, and cardinality method.
-   `empty_string_count`, `whitespace_only_count`, `zero_count`,
    `false_count`, and other type-appropriate falsy statistics when
    measurable.
-   Length minimum, maximum, quantiles, and histogram.
-   Numeric minimum, maximum, quantiles, precision, scale, and histogram
    where applicable.
-   Date/time range and detected formats where applicable.
-   Top categories with disclosure controls.
-   Format patterns and frequency summaries.
-   Candidate sensitivity labels and confidence.
-   Constraints, anomalies, evidence references, and confidence.
-   Sampling method, sample size, and query cost.
-   Generation strategy recommendation.

Counts must distinguish exact values from estimates and unknown values.
Never silently convert unknown to zero.

### 5.5 Observation

Each observation records:

-   `id`, `dataset_id`, optional `column_id`.
-   `kind`: metadata, aggregate, sample, detector, relationship,
    anomaly.
-   `method` and algorithm version.
-   Result summary, without raw values unless an explicit local-only
    policy allows them.
-   `observed_at`, row/query/byte cost, and evidence scope.
-   Confidence and limitations.

Raw samples must be ephemeral by default. Persist patterns and aggregate
summaries rather than raw values.

### 5.6 ProfileArtifact

-   `schema_version`.
-   `profile_id`, `created_at`, `tool_version`.
-   Source and schema fingerprints.
-   Profile configuration and query budget.
-   Dataset and column profiles.
-   Relationship graph.
-   Sensitivity findings.
-   Evidence and uncertainty summaries.
-   Algorithm versions and deterministic settings.
-   Warnings and incomplete checks.

A profile artifact should be safe to share only after applying a
profile-disclosure policy. Column names and category examples can
themselves be sensitive.

### 5.7 SanitizationPolicy

Policy fields:

-   Allowed sources and datasets.
-   Allowed columns and excluded columns.
-   Per-column action: `keep`, `mask`, `redact`, `generalize`,
    `tokenize`, `synthetic`, `drop`, or `manual_review`.
-   Sensitivity labels and allowed destinations.
-   Maximum rows, bytes, runtime, and query count.
-   Whether aggregate profiling may scan all rows.
-   Whether any raw samples may be returned.
-   Egress destinations and allowed transports.
-   Logging and audit settings.
-   Export and write permissions.
-   Policy version, owner, and approval timestamp.

Default policy: deny raw sample disclosure, deny writes, deny external
model calls, allow bounded metadata and aggregate profiles, and require
explicit field handling before row-level export.

### 5.8 GenerationPlan and GenerationReport

`GenerationPlan` records dataset profiles, row counts, seed, strategy
overrides, relationship graph, null-rate behavior, uniqueness
requirements, output target, and budget.

`GenerationReport` records counts generated, validation results,
constraint violations, distribution distances, preserved and
approximated properties, strategy fallbacks, warnings, runtime, and
seed. Do not include raw source values in reports.

### 5.9 AuditEvent

Record actor, operation, source/dataset IDs, policy version, decision,
counts, duration, result status, and correlation ID. Never record
credentials, raw rows, or unredacted SQL parameters.

## 6. Connector interface

All connectors must implement a shared asynchronous application-facing
contract. Synchronous drivers may run in a bounded thread pool or behind
a subprocess.

Illustrative Python interface:

``` python
from typing import Protocol, AsyncIterator

class DataConnector(Protocol):
    async def test_connection(self, config: "SourceConfig") -> "ConnectionTest": ...
    async def list_datasets(self, config: "SourceConfig") -> list["DatasetRef"]: ...
    async def get_schema(
        self, config: "SourceConfig", dataset: "DatasetRef"
    ) -> "DatasetSchema": ...
    async def estimate_size(
        self, config: "SourceConfig", dataset: "DatasetRef"
    ) -> "SizeEstimate": ...
    async def execute_profile_query(
        self, config: "SourceConfig", query: "ProfileQuery"
    ) -> "ProfileQueryResult": ...
    async def sample_rows(
        self, config: "SourceConfig", request: "SampleRequest"
    ) -> AsyncIterator["EphemeralRow"]: ...
    async def close(self) -> None: ...
```

A separate optional `WritableConnector` interface provides
`write_rows()` for approved destination databases. Source connectors
must remain read-only unless the user explicitly configures a distinct
destination role.

### 6.1 Shared connector requirements

-   Parameterized queries wherever supported.
-   Identifier quoting through connector-specific functions.
-   Read-only credentials recommended and validated where possible.
-   Query timeout and cancellation.
-   Bounded result rows and bytes.
-   Connection pooling with configurable limits.
-   Retry only transient, idempotent operations.
-   Clear capability declarations, including support for metadata,
    aggregates, sampling, cancellation, and writes.
-   Structured errors with safe messages and provider-specific codes.
-   No raw-row logging.
-   Pagination for APIs and sheets.
-   Rate limiting and backoff for provider APIs.
-   Deterministic ordering when sampling or pagination requires it.
-   Explicit timezone and decimal handling.
-   Connector contract tests run against every adapter.

### 6.2 PostgreSQL

-   Discover schemas, tables, views, column types, nullability,
    defaults, primary/foreign keys, indexes, and constraints from
    catalogs and `information_schema`.
-   Use `pg_stats` only when privileges and statistics availability
    permit; mark results as estimates.
-   Use `TABLESAMPLE` only as an optional strategy, documenting that it
    can be block-biased and may miss rare values.
-   Use bounded `ORDER BY` sampling with a stable key where available.
    Avoid unbounded random sorts on large tables.
-   Run profiling in read-only transactions where practical.
-   Support SSL verification and explicit connect/query timeouts.

### 6.3 MySQL

-   Discover schema metadata from `information_schema`.
-   Support MySQL versions declared in the connector compatibility
    matrix.
-   Use bounded samples and aggregate queries.
-   Handle unsigned integers, `DECIMAL`, `BIT`, JSON columns, zero dates
    where encountered, and server SQL mode differences.
-   Use a read-only account and TLS verification where supported.

### 6.4 Alibaba MaxCompute through JDBC

-   Use the official MaxCompute JDBC driver compatible with the selected
    Java runtime.
-   Verify current driver documentation, authentication modes, endpoint
    requirements, and SQL dialect during implementation.
-   The Python connector talks to the JVM sidecar using the bridge
    protocol.
-   Support catalog/schema/table discovery to the extent exposed by the
    driver.
-   Map MaxCompute SQL types to logical types without losing decimal
    precision or timestamp semantics.
-   Budget scan bytes and query cost when the service exposes such
    estimates.
-   Avoid repeated full-table scans. Prefer metadata, partition pruning,
    aggregates, and bounded queries.
-   Make partition filters configurable and require confirmation when a
    profiling plan could scan an unexpectedly large dataset.
-   Treat sampling syntax as a capability that must be tested against
    the actual MaxCompute dialect; do not assume PostgreSQL or MySQL
    sampling syntax.
-   Record the submitted query fingerprint, estimated cost if available,
    actual cost if available, duration, and returned row count.
-   Keep driver diagnostics on stderr and redact credentials.

### 6.5 Google Sheets

-   Use the official Google Sheets API with OAuth or service-account
    authentication, according to deployment policy.
-   Request only necessary spreadsheet scopes.
-   Discover spreadsheet, tab, grid dimensions, header rows, and named
    ranges.
-   Support explicit header-row selection and configurable handling for
    duplicate or blank headers.
-   Distinguish blank cells, missing cells, formula cells, formula
    results, and strings such as `"0"` or `"FALSE"` where the API
    response permits it.
-   Read in bounded ranges with pagination or range partitioning.
-   Do not modify source sheets.
-   Never persist access tokens in profile artifacts.

### 6.6 Lark Sheets

-   Use the official Lark Open Platform APIs and document the required
    app scopes and tenant/user authorization modes.
-   Keep provider-specific identifiers separate from display names.
-   Discover spreadsheet, sheet, range, headers, and cell value types
    using the provider's metadata.
-   Respect API limits, pagination, tenant boundaries, and token expiry.
-   Support explicit field/range allowlists.
-   Do not modify source sheets.
-   Build contract tests around actual API response formats and error
    handling.

### 6.7 Spreadsheet normalization

Spreadsheets have weaker schemas than databases. The user must be able
to configure:

-   Header row index.
-   Data range or automatic used range.
-   Columns to skip.
-   Locale for numbers and dates.
-   Whether formulas or displayed values are profiled.
-   Whether merged cells should be rejected, expanded, or treated as
    missing.
-   Blank-row termination behavior.
-   Duplicate header resolution.
-   Type coercion policy.

Automatic inference must preserve the original cell representation
alongside a normalized value during the in-memory profiling session.
Empty string, blank cell, missing cell, whitespace-only string, numeric
zero, boolean false, and formula returning an empty string must remain
distinguishable whenever the source API permits.

## 7. Adaptive profiling engine

### 7.1 Objectives

The profiler must maximize useful information while respecting source
cost, latency, and privacy budgets. It should use cheap metadata and
aggregate queries first, then spend additional budget on unresolved
questions.

### 7.2 Profile stages

1.  **Preflight:** validate source permissions, policy, dataset
    allowlist, query budget, and connector capabilities.
2.  **Metadata:** fetch schema, constraints, partitions, row estimates,
    and provider metadata.
3.  **Cheap aggregates:** collect row count or estimate, null counts,
    empty/whitespace counts where supported, min/max, and simple
    length/type summaries.
4.  **Representative sampling:** select a bounded sample using
    stable-key stratification, partition-aware sampling, or
    provider-supported sampling.
5.  **Pattern inference:** infer logical types, formats, category
    distributions, numeric/date ranges, and likely identifiers.
6.  **Edge-case probes:** target nulls, falsy values, rare categories,
    malformed patterns, extremes, duplicate candidates, and mixed-type
    cases.
7.  **Relationship probes:** discover or validate keys, dependencies,
    join relationships, and correlated fields.
8.  **Sensitivity detection:** classify likely personal data, secrets,
    identifiers, and free-text risk.
9.  **Confidence and stopping:** decide whether more probes offer enough
    expected value for their cost.
10. **Artifact finalization:** save evidence summaries, budget usage,
    uncertainty, and profile version.

### 7.3 Query planner

Each candidate probe has:

-   Expected information gain.
-   Estimated query cost and latency.
-   Privacy risk.
-   Required connector capabilities.
-   Budget cost.
-   Dependency on previous observations.

Choose probes using a configurable score such as:

`priority = expected_information_gain * uncertainty_weight / max(estimated_cost, cost_floor)`

Apply privacy and budget gates before ranking. This score is a
heuristic, not a statistical guarantee. The planner must log why it
selected or skipped each probe.

Stop when any condition applies:

-   Query, row, byte, runtime, or cost budget is exhausted.
-   Remaining candidates fall below a configurable utility threshold.
-   Confidence targets are met for required properties.
-   The user cancels the job.
-   The source starts returning repeated transient failures.
-   A safety policy blocks further access.

### 7.4 Sampling strategies

Support at least:

-   Stable-key ordered sampling.
-   Random sampling when the source can perform it within budget.
-   Stratified sampling by partition or known category.
-   Rare-value targeted queries based on observed candidates.
-   Time-window sampling for time-series data.
-   Connector-provided sampling where verified.
-   Explicit user-provided predicates and ranges.

Always disclose sampling limitations. A sample cannot establish absence
of rare events. If the source cannot guarantee representative sampling,
mark representativeness as uncertain.

### 7.5 Null and falsy-value analysis

For every column, preserve distinctions between:

-   SQL `NULL`.
-   Empty string `""`.
-   Whitespace-only string.
-   Numeric zero.
-   Boolean `false`.
-   String values `"0"`, `"false"`, `"null"`, and `"None"`.
-   Missing spreadsheet cell.
-   Formula cell with empty displayed result.
-   JSON `null`, absent JSON property, empty array, and empty object.
-   NaN and positive/negative infinity where the source type permits
    them.
-   Sentinel values such as `-1`, `9999`, `"N/A"`, or `"unknown"` when
    observed.

Do not classify sentinel values as missing without evidence or user
policy. Report both raw and normalized interpretations. For database
aggregate queries, account for SQL three-valued logic and
connector-specific type coercion.

### 7.6 Type and format inference

Infer a logical type independently of the declared source type.
Supported logical types include:

-   string, categorical string, free text.
-   integer, decimal, floating point.
-   boolean.
-   date, time, timestamp, timestamp with timezone.
-   UUID and identifier-like string.
-   email, phone, URL, IP address.
-   postal code, currency-like value, country/region code.
-   JSON object/array.
-   binary or unsupported opaque value.

Inference should retain ambiguity. A column containing `"00123"` should
not automatically become an integer because leading zeros may be
meaningful. Dates such as `03/04/2025` require locale or ambiguity
metadata. Large integers and decimal values must avoid floating-point
loss.

### 7.7 Distribution and anomaly summaries

Use exact counts where feasible; otherwise use documented approximate
algorithms such as HyperLogLog for cardinality and reservoir sampling
for bounded samples. Keep the algorithm name and error characteristics
in the profile.

Possible summaries:

-   Null and falsy rates.
-   Approximate distinct count.
-   Top-k category counts with rare-category suppression.
-   Length and numeric histograms.
-   Quantiles and min/max.
-   Pattern frequencies.
-   Duplicate candidate rates.
-   Format violations.
-   Cross-column dependency candidates.
-   Temporal gaps and seasonality hints.
-   Referential integrity estimates.

Avoid exposing rare category values in MCP results by default. Apply
minimum group sizes or suppress small groups when summaries could
disclose individuals.

### 7.8 Relationship discovery

Prioritize declared constraints. Infer candidate keys from uniqueness
and nullability, then verify with bounded checks. Discover candidate
foreign keys using compatible types and sampled overlap. Treat inferred
relationships as hypotheses until verified.

Cross-column dependencies may be inferred from functional dependency
tests or mutual information approximations, subject to budget. Do not
infer that correlation means causation or that a dependency always
holds.

### 7.9 Profile quality and confidence

Confidence labels: `high`, `medium`, `low`, `unknown`, with documented
mapping rules per detector. Confidence should depend on sample size,
evidence type, coverage, consistency, and connector limitations.

Each profile must state: - What was measured exactly. - What was
estimated. - What was inferred. - What was not checked. - Which budgets
limited coverage.

## 8. Sanitization and privacy

### 8.1 Threat model

Assume source data may contain personal information,
business-confidential values, credentials, tokens, and rare combinations
that identify people. Assume an MCP client may request excessive data or
attempt prompt injection through source content. Treat database
contents, sheet cells, metadata comments, and free text as untrusted
data.

### 8.2 Safe defaults

-   Read-only source access.
-   No raw row output from MCP by default.
-   No hosted LLM calls by default.
-   No raw source values in logs, telemetry, exception messages, crash
    reports, or profile artifacts.
-   No source mutation.
-   Explicit dataset and column allowlists for sensitive sources.
-   Row, byte, query, and time limits.
-   Secret redaction in all diagnostics.
-   Policy checks before every tool operation and before serializing
    every result.
-   Remote MCP requires authentication, authorization, TLS, and explicit
    allowed-source configuration.
-   Deny-by-default for write operations and remote egress.

### 8.3 Sensitivity detection

Use layered detection:

1.  Schema/name heuristics.
2.  Format detectors for emails, phones, addresses, government
    identifiers, access tokens, and common credential formats.
3.  Entropy and token-pattern heuristics for secrets.
4.  Configurable organization-specific detectors.
5.  Optional local classifiers, disabled unless configured.

Detection results must include label, evidence type, confidence, and
recommended handling. Avoid logging matched values. Detection is a
warning and policy input, not proof that a column is safe when no match
is found.

### 8.4 Transformation actions

-   **Keep:** only for approved non-sensitive fields.
-   **Mask:** replace selected characters or sections.
-   **Redact:** replace a value with a fixed marker or null.
-   **Generalize:** reduce precision, such as age bands or date
    truncation.
-   **Tokenize:** replace values with stable pseudonyms using a secret
    key. Tokenization remains linkable and is not anonymization.
-   **Synthetic:** generate a replacement value from learned patterns
    and constraints.
-   **Drop:** exclude a field.
-   **Manual review:** block export until a human resolves the field.

Use cryptographically secure keyed HMAC or an approved tokenization
scheme for stable pseudonyms. Never use a plain hash for low-entropy
identifiers. Keep tokenization secrets outside artifacts and source
control.

### 8.5 Synthetic data privacy

Synthetic generation does not automatically guarantee privacy. To reduce
leakage:

-   Never copy complete source rows as a generation shortcut.
-   Avoid replaying raw samples as examples in MCP output.
-   Use aggregates and inferred formats for generation.
-   Suppress rare categories or map them into an `OTHER` bucket
    according to policy.
-   Avoid exact reproduction of rare combinations.
-   Support minimum category counts and privacy review thresholds.
-   Report the use of source-derived distributions.
-   Offer an optional privacy-risk review that flags near-duplicates and
    rare combinations.
-   Document that empirical-distribution synthesis can leak information
    and does not provide differential privacy.
-   Do not claim differential privacy unless a reviewed DP algorithm
    with a defined privacy budget is implemented and tested.

### 8.6 LLM integration boundary

The initial release does not need an LLM to profile or generate ordinary
fields. If an optional model-assisted rule suggestion feature is added
later:

-   It must be disabled by default.
-   Only policy-approved, sanitized metadata and pattern descriptions
    may be sent.
-   Raw rows, secrets, and free-text samples remain blocked unless
    explicitly approved.
-   The model returns proposed rules, never executable SQL or policy
    changes.
-   Validate all proposals with typed schemas and deterministic local
    checks.
-   Record model/provider, prompt template version, and decision
    provenance without recording sensitive prompt contents.
-   Human approval is required before broadening access or changing
    policy.

## 9. Synthetic generation engine

### 9.1 Strategy registry

Every column generation strategy implements:

``` python
class GenerationStrategy(Protocol):
    def can_generate(self, column: "ColumnProfile") -> bool: ...
    def plan(self, column: "ColumnProfile", context: "GenerationContext") -> "StrategyPlan": ...
    def generate(self, plan: "StrategyPlan", context: "GenerationContext") -> object: ...
```

Strategies, in preferred order:

1.  Explicit user-defined strategy.
2.  Declared schema constraints.
3.  Strongly inferred format generator.
4.  Semantic Faker provider.
5.  Learned categorical/distribution sampler.
6.  Generic type-aware generator.
7.  Fail closed with a clear unsupported-field error.

### 9.2 Generation behavior

-   Use a seeded pseudorandom generator for reproducibility.
-   Persist seed and generator versions.
-   Preserve observed null rate within a configurable tolerance, subject
    to policy and minimum sample quality.
-   Preserve empty-string, whitespace-only, zero, and false rates
    separately when evidence supports them.
-   Preserve declared and approved inferred uniqueness.
-   Generate values within observed ranges, with optional configurable
    tail behavior.
-   Preserve string lengths and format distributions when useful.
-   Keep identifiers syntactically plausible while avoiding original
    identifiers.
-   Preserve date/time timezone rules and precision.
-   Generate related records in dependency order.
-   Allow circular references through a staged key-allocation process.
-   Support deterministic generation for repeatable tests.
-   Never rely on Faker defaults alone when the profile contains a more
    specific pattern.

### 9.3 Faker and format generation

Examples: - Email: generate using a reserved or configured synthetic
domain, never real addresses by default. - Phone: use provider-supported
fictional/test ranges where available; otherwise mark the values as
synthetic and avoid claiming they are guaranteed unassigned. - Names and
addresses: use Faker locale selection, with locale inferred only when
confidence is sufficient. - Identifiers: generate format-compatible
synthetic IDs; preserve prefixes/check digits where constraints are
known. - Dates: sample from inferred temporal ranges and preserve
timezone behavior. - Enum/categorical values: sample from approved
categories after rare-value handling. - Free text: default to redaction
or a generic synthetic placeholder. Avoid sending free text to an LLM. -
JSON: generate recursively from inferred shape, with field-level policy
and type constraints. - Currency and decimals: preserve scale, bounds,
and configured rounding rules.

### 9.4 Cross-column and relational consistency

-   Enforce primary-key uniqueness.
-   Enforce foreign-key references when the relationship is known.
-   Preserve configured functional dependencies, such as
    `country -> currency`, only when approved and sufficiently
    supported.
-   Generate dates in valid order for known pairs such as
    `created_at <= updated_at`.
-   Enforce numeric constraints such as `quantity >= 0`.
-   Allow user-authored constraints with a safe, typed expression
    format.
-   Avoid arbitrary Python evaluation or executable expressions in
    profiles and policies.

### 9.5 Distribution fidelity

Measure generation fidelity using relevant metrics: - Null/falsy rate
absolute error. - Numeric quantile differences and normalized histogram
distance. - Categorical total variation distance, with rare categories
grouped. - String-length distribution distance. - Format-validity
rate. - Uniqueness and referential-integrity rates. - Cross-column
constraint violation rate.

Set configurable tolerances by logical type and sample quality. A report
must distinguish a failed hard constraint from a soft distribution
mismatch.

### 9.6 Generation failure behavior

If a required strategy cannot satisfy a hard constraint within a bounded
number of attempts, stop and report the column/constraint. Never
silently weaken a hard constraint. A user may explicitly permit a
documented fallback strategy.

## 10. Output and persistence

### 10.1 JSONL

-   One record per line.
-   UTF-8 encoding.
-   Stable field ordering when a schema is known.
-   JSON null preserved.
-   Decimal values serialized without precision loss, preferably as JSON
    numbers only when safe and otherwise according to an explicit schema
    policy.
-   Output metadata stored in a sidecar manifest, never injected as fake
    data rows.

### 10.2 CSV

-   UTF-8 by default.
-   Configurable delimiter, quoting, newline, and header.
-   Explicit null serialization policy because CSV cannot intrinsically
    distinguish null from empty string.
-   Formula-injection protection for spreadsheet-facing CSV: cells
    beginning with `=`, `+`, `-`, or `@` must be escaped under a
    configurable safe-export mode.
-   Manifest records null encoding and any escaping transformations.

### 10.3 Database destination

-   Separate source and destination configurations.
-   Destination must be explicitly configured and approved.
-   Never write back to the source by default.
-   Require table/schema allowlists and a write policy.
-   Support create-new-table by default; append or replace require
    explicit confirmation.
-   Use transactions where supported.
-   Validate schema mapping before writing.
-   Handle generated keys, foreign-key ordering, and rollback.
-   Record destination row counts and validation results.
-   Do not include credentials in manifests.

### 10.4 Artifact layout

Example:

``` text
project/
  qc.toml
  policies/
    default.yaml
  profiles/
    <dataset-fingerprint>.profile.json
  reports/
    <job-id>.report.json
  output/
    synthetic.jsonl
    synthetic.manifest.json
```

The config file may contain non-secret references and defaults. Raw
samples, credentials, and tokens must never be stored in these
artifacts. Apply restrictive file permissions to local state.

## 11. CLI specification

Executable name: `qc`.

All commands support `--config`, `--output`, `--format`, `--json`,
`--quiet`, and `--verbose` where meaningful. Exit codes must be
documented and stable.

### 11.1 Commands

``` text
qc init
qc source add <name> --type <postgres|mysql|maxcompute|google-sheets|lark-sheets>
qc source test <name>
qc source list
qc source inspect <name>
qc dataset list <source>
qc dataset schema <source> <dataset>
qc profile run <source> <dataset> [options]
qc profile show <profile-id>
qc profile diff <profile-a> <profile-b>
qc policy list
qc policy validate <file>
qc policy apply <policy> <dataset>
qc sanitize preview <source> <dataset> --policy <policy>
qc sanitize export <source> <dataset> --policy <policy> --format <jsonl|csv>
qc generate plan <profile-id> --rows <count>
qc generate run <plan-file> --output <path>
qc generate validate <output> --profile <profile-id>
qc export database <output> --destination <source> --table <name>
qc job list
qc job show <job-id>
qc doctor
qc version
```

### 11.2 Important flags

`profile run`: - `--max-queries` - `--max-rows` - `--max-bytes` -
`--max-runtime` - `--max-cost` when supported - `--sample-size` -
`--columns` - `--where` only through a safe typed predicate interface,
not arbitrary concatenated SQL - `--profile-output` -
`--sensitivity-scan` - `--allow-full-scan` with an explicit confirmation
for potentially expensive scans

`sanitize preview` and `sanitize export`: - `--policy` - `--columns` -
`--limit` - `--include-null-patterns` - `--dry-run` - `--manifest`

`generate run`: - `--rows` - `--seed` - `--locale` - `--policy` -
`--strict` - `--report` - `--allow-fallbacks` only when explicitly
requested

### 11.3 Exit codes

-   `0`: success.
-   `1`: general operation failure.
-   `2`: invalid arguments or configuration.
-   `3`: authentication or authorization failure.
-   `4`: policy denied the operation.
-   `5`: budget exhausted or operation cancelled.
-   `6`: source/connector error.
-   `7`: validation failure.
-   `8`: unsupported capability or type.
-   `9`: destination write failure.

Human-readable errors go to stderr. Machine-readable output must be
valid JSON when `--json` is selected.

## 12. MCP server specification

### 12.1 Transports

-   **stdio:** default for local AI clients.
-   **Streamable HTTP:** optional remote or shared deployment mode.
-   Remote mode must require authentication, TLS termination, per-client
    authorization, request limits, and explicit source allowlists.
-   Never expose a server with unrestricted source discovery or query
    execution.

### 12.2 Tool design

Tools should be task-oriented, typed, bounded, and policy-aware. The
server validates every input, enforces budgets, and sanitizes every
result.

Initial tools:

1.  `list_sources`
    -   Returns only sources visible to the caller.
    -   Never returns credentials or full connection strings.
2.  `list_datasets`
    -   Input: `source_id`, optional namespace, optional search prefix,
        bounded page size and cursor.
    -   Returns permitted dataset identifiers and types.
3.  `get_dataset_schema`
    -   Input: `source_id`, `dataset_id`.
    -   Returns schema metadata and constraints allowed by policy.
    -   Sensitive column names may be redacted by policy.
4.  `profile_dataset`
    -   Input: `source_id`, `dataset_id`, optional profile settings
        within server-defined maxima.
    -   Returns a profile summary, coverage, warnings, and profile ID.
    -   Cannot override server budgets or privacy policy.
5.  `get_profile`
    -   Input: `profile_id`.
    -   Returns a policy-filtered profile artifact summary.
6.  `preview_sanitized_data`
    -   Input: source/dataset or profile ID, policy ID, bounded row
        count, optional approved column list.
    -   Returns only policy-approved transformed rows.
    -   Raw rows are denied by default.
7.  `generate_synthetic_data`
    -   Input: profile ID, bounded row count, seed optional, output
        format, strategy overrides allowed by policy.
    -   Default response returns a summary and artifact reference.
        Inline records require an explicit small response limit and
        approved fields.
8.  `validate_synthetic_data`
    -   Input: generated artifact reference and profile ID.
    -   Returns validation metrics and violations, without source
        values.
9.  `get_job_status`
    -   Input: job ID.
    -   Returns status, progress, safe diagnostics, and cancellation
        availability.
10. `cancel_job`
    -   Input: job ID owned by the caller or explicitly authorized.
    -   Cancels the job and closes resources where supported.
11. `get_policy_summary`
    -   Input: policy ID.
    -   Returns effective permissions and restrictions without secrets.

### 12.3 Tools excluded from v1

-   Arbitrary SQL execution.
-   Arbitrary Python execution.
-   Policy mutation through MCP.
-   Source creation or credential management through MCP.
-   Source writes.
-   Direct export to public URLs.
-   Any tool that returns unrestricted raw rows.

### 12.4 MCP output envelope

Use structured content where supported. Return a consistent payload:

``` json
{
  "status": "completed",
  "job_id": "job_opaque_id",
  "data": {},
  "warnings": [],
  "provenance": {
    "profile_id": "profile_opaque_id",
    "policy_version": "1",
    "tool_version": "0.1.0"
  },
  "truncated": false
}
```

Errors must use stable error codes such as `POLICY_DENIED`,
`BUDGET_EXCEEDED`, `SOURCE_UNAVAILABLE`, `UNSUPPORTED_CAPABILITY`,
`VALIDATION_FAILED`, and `NOT_FOUND`. Error text must not disclose
secrets, internal network details, raw values, or unapproved schema
details.

### 12.5 MCP resources and prompts

QC publishes one resource: the adaptive policy document
`dev.adaptivemcp://tools-metadata` (`application/yaml`), from the MCP
`tools-metadata` extension (SEP-2133). It carries per-tool *suggestions*
(risk, owner, tags, `require_approval`) authored by QC. It is advisory:
static, free of learned client data and budgets, and clients that ignore
it are unaffected. QC never learns from it and cannot be reconfigured
through it.

Decision (2026-10-15): the optional read-only resources
`qc://profiles/{profile_id}`, `qc://jobs/{job_id}`,
`qc://policies/{policy_id}/summary`, and optional prompt templates, are
declined for now. Each resource would duplicate a tool that already returns the
same data while adding a second surface to authorize (below); a prompt template
would restate guidance that already lives in tool descriptions and server
instructions. Revisit when a client has a concrete need.

Resources must enforce the same authorization and disclosure policy as
tools. Avoid embedding sensitive values in resource URIs. Server-side
enforcement remains authoritative regardless of client-facing conveniences.

### 12.6 MCP security

-   Validate tool arguments against strict schemas.
-   Enforce authorization on every call, including resources.
-   Bound response rows, bytes, and execution time.
-   Apply per-client rate limits in remote mode.
-   Prevent tool arguments from changing source allowlists or policy
    enforcement.
-   Treat source text as untrusted content, never as instructions.
-   Do not allow data content to trigger follow-up network calls or tool
    execution.
-   Include correlation IDs in safe audit events.
-   Avoid returning raw SQL unless explicitly authorized and redacted.
-   Ensure cancellation closes cursors and connections.

## 13. Configuration

Use TOML for general application configuration and YAML or JSON for
policies. Validate all configuration at startup.

Example `qc.toml`:

``` toml
version = 1
default_policy = "strict-local"
state_dir = "~/.local/share/qc"
profile_dir = "~/.local/share/qc/profiles"

[budgets]
max_queries = 100
max_rows = 100000
max_bytes = 67108864
max_runtime_seconds = 300
max_inline_mcp_rows = 20
max_inline_mcp_bytes = 65536

[security]
raw_sample_persistence = false
raw_mcp_rows = false
external_model_calls = false
source_writes = false
redact_logs = true

[mcp.stdio]
enabled = true

[mcp.http]
enabled = false
host = "127.0.0.1"
port = 8765
require_auth = true
```

Example policy:

``` yaml
version: 1
name: strict-local
defaults:
  source_access: deny
  raw_row_output: deny
  source_writes: deny
  external_egress: deny
  profile_disclosure: aggregate_only
  max_rows: 10000
  max_bytes: 16777216
  max_runtime_seconds: 120

sources:
  analytics-readonly:
    access: read
    datasets:
      - public.orders
    columns:
      customer_email:
        action: synthetic
      customer_name:
        action: synthetic
      order_id:
        action: synthetic
      created_at:
        action: generalize
      total_amount:
        action: keep
```

The examples are illustrative. Implementation must define exact
semantics, precedence, schema validation, and migration behavior before
release. A policy must never grant more access than the server's own
configured ceiling.

## 14. API and artifact compatibility

-   Every serialized artifact includes `schema_version`.
-   Semantic changes require a schema version increment.
-   Provide explicit migrations for supported older versions.
-   Unknown future versions must fail safely with an actionable error.
-   Stable public CLI commands and MCP tool schemas follow semantic
    versioning rules.
-   The profile algorithm version is separate from the artifact schema
    version.
-   Source fingerprints must avoid storing credentials or sensitive
    sample content. Use stable metadata and a keyed digest where raw
    schema names could be sensitive.
-   Reproducibility requires the same profile artifact, generation
    configuration, seed, and generator version. Source-dependent
    behavior may still vary if a profile changes.

## 15. Observability and operations

-   Structured logs with timestamps, severity, operation ID, source ID,
    and safe error code.
-   Metrics: query count, rows scanned/returned, bytes processed,
    elapsed time, retry count, budget utilization, generated rows,
    validation failures, and connector health.
-   No row values, credentials, access tokens, raw query parameters, or
    unredacted sensitive column values in logs.
-   Local diagnostics should include versions and capability checks,
    with secrets removed.
-   Support cancellation and graceful shutdown.
-   Ensure temporary files are removed on success, failure, and
    cancellation.
-   Remote service deployment should provide health and readiness checks
    without exposing source metadata.
-   Retain audit events according to a configurable policy.

## 16. Testing strategy

### 16.1 Unit tests

Cover: - Type normalization and decimal/timestamp precision. - Null,
empty, whitespace, zero, false, and sentinel handling. - Format
inference ambiguity. - Sensitivity detection and policy precedence. -
Masking, redaction, generalization, tokenization, and synthetic
replacement. - Distribution calculations and confidence rules. -
Deterministic generation from seeds. - Constraint solvers and
referential integrity. - CSV null encoding and formula injection. -
Artifact schema validation and migration. - MCP response redaction and
output limits.

### 16.2 Property-based tests

Use Hypothesis to generate values and schemas. Properties include: - No
invalid generated value violates a hard type constraint. - Primary keys
remain unique when uniqueness is required. - Foreign keys reference
generated parent keys. - Output never exceeds configured row/byte
limits. - Policy-denied columns never appear in outputs. - Secrets and
raw samples never appear in logs or error strings. - Repeating
generation with the same profile, seed, and versions yields identical
output where deterministic behavior is promised. - Empty/falsy
categories remain distinct according to the profile model.

### 16.3 Connector contract tests

Run the same suite against each connector: - Connect and close. -
Discover datasets and schemas. - Handle missing permissions. - Respect
deadlines and cancellation. - Enforce row/byte bounds. - Handle empty
datasets and null-heavy columns. - Preserve decimal, boolean, date, and
timezone types. - Recover from transient errors without duplicate side
effects. - Redact credentials and source values in diagnostics.

### 16.4 Integration tests

-   PostgreSQL and MySQL via disposable containers in CI.
-   MaxCompute using a dedicated test project or mocked JDBC contract
    tests plus an opt-in live integration suite.
-   Google Sheets and Lark Sheets using mocked HTTP responses for
    routine CI and opt-in tenant-backed tests for release qualification.
-   MCP stdio protocol integration with a reference client.
-   MCP HTTP authentication, authorization, rate limits, and TLS
    configuration tests.
-   Database destination rollback and failure handling.

### 16.5 Security tests

-   Attempt to access a disallowed dataset.
-   Attempt to request raw samples.
-   Attempt to override budgets through MCP arguments.
-   Prompt injection content in cells and comments.
-   Credential leakage in exceptions and logs.
-   Path traversal in artifact references.
-   SQL injection through dataset names and predicates.
-   CSV formula injection.
-   Malicious or oversized spreadsheet ranges.
-   Unauthorized destination writes.
-   Cross-client job and artifact access in HTTP mode.

### 16.6 Quality and performance tests

Create synthetic benchmark datasets with: - 10, 100, and 500 columns. -
Null-heavy and highly sparse data. - High-cardinality identifiers. -
Rare categories. - Malformed values. - JSON and free-text columns. -
Large tables with partitions. - Correlated columns and relational
constraints.

Measure query count, bytes read, runtime, memory, profile completeness,
generation fidelity, and privacy warnings. Establish baseline budgets in
CI and publish benchmark methodology.

## 17. Milestones

### M0: Foundations

Deliver: - Repository structure and packaging. - Domain models and
versioned schemas. - Configuration loader and policy engine. - Secret
resolver abstraction. - Logging redaction. - CI, linting, type checks,
and unit test harness. - Architecture decision records for connector and
MCP boundaries.

Exit criteria: - Reproducible development setup. - No plaintext
credentials in sample configuration. - Policy-denied operations fail
closed.

### M1: Profiling core and local files

Deliver: - Logical type system. - Adaptive profiling planner and
budgets. - Null/falsy and format detectors. - Profile artifact
serialization. - JSONL and CSV fixture connectors for internal testing
only. - CLI `init`, `profile`, `profile show`, and `doctor`.

Exit criteria: - Profiles report measured versus inferred values. -
Profiling obeys row/query/byte/time budgets. - Edge-case fixtures pass.

### M2: PostgreSQL and MySQL

Deliver: - Read-only connectors. - Metadata discovery and adaptive query
implementations. - Connector contract suite. - Connection configuration
and diagnostics.

Exit criteria: - Live integration tests pass against supported
versions. - Large-table profiling avoids accidental full scans by
default. - Decimal, null, date/time, and falsy semantics are covered.

### M3: Spreadsheet connectors

Deliver: - Google Sheets and Lark Sheets adapters. -
OAuth/service-account or tenant token configuration. - Range and header
inference. - Spreadsheet-specific blank/formula handling.

Exit criteria: - Pagination, rate limiting, and token refresh are
tested. - Source sheets remain unchanged. - Blank and empty values
retain distinguishable representations where available.

### M4: JDBC sidecar and MaxCompute

Deliver: - Versioned bridge protocol. - JVM process lifecycle, timeouts,
cancellation, and diagnostics. - MaxCompute schema mapping and profiling
capability matrix. - Scan-budget warnings.

Exit criteria: - Bridge contract tests pass. - Credentials remain
outside command-line arguments and logs. - Live test project verifies
supported driver/auth/query behavior before general availability.

### M5: Sanitization and synthetic generation

Deliver: - Sensitivity detectors. - Per-column policy actions. - Faker
and format strategy registry. - Distribution and relational
generation. - Validation reports. - JSONL and CSV export. - Database
destination writer.

Exit criteria: - Hard constraints are enforced or generation fails
explicitly. - Generated values satisfy configured format and
relationship constraints. - Raw samples do not appear in persisted
artifacts or reports. - Fidelity metrics and limitations appear in
reports.

### M6: MCP and remote deployment

Deliver: - MCP stdio tools. - Streamable HTTP mode with authentication
and authorization. - Job lifecycle and cancellation. - Structured tool
outputs and safe errors. - Rate limits and response caps.

Exit criteria: - Reference-client integration passes. - No arbitrary
SQL, source writes, or policy mutation tools are exposed. - Unauthorized
requests and budget override attempts fail safely.

### M7: Hardening and release

Deliver: - Security review and threat-model review. - Performance
benchmarks. - Documentation and connector compatibility matrix. -
Upgrade/migration policy. - Signed or verifiable release artifacts where
practical. - Example workflows and reproducible demos.

Exit criteria: - Release acceptance criteria pass. - Known limitations
and privacy claims are documented. - Installation and upgrade tested on
supported platforms.

## 18. Acceptance criteria

The v1 release is acceptable when all of the following hold.

### Connectivity and discovery

-   [ ] PostgreSQL and MySQL connect with read-only credentials and
    discover schemas, tables, columns, and declared constraints.
-   [ ] MaxCompute works through the pinned, verified JDBC driver via
    the JVM bridge.
-   [ ] Google Sheets and Lark Sheets discover permitted spreadsheets,
    tabs, and configured ranges.
-   [ ] Connector failures return actionable, redacted errors.
-   [ ] Each connector passes the common contract suite.

### Profiling

-   [ ] Profiles distinguish SQL null, empty string, whitespace-only
    string, zero, false, string representations, and spreadsheet blanks
    where source semantics allow.
-   [ ] Type inference preserves ambiguous identifiers, leading zeros,
    decimal precision, and date ambiguity.
-   [ ] Adaptive profiling records probes, costs, coverage, confidence,
    and unresolved questions.
-   [ ] Query, row, byte, runtime, and provider cost budgets are
    enforced.
-   [ ] Large datasets do not trigger an unbounded scan without explicit
    approval.
-   [ ] Rare categories and potentially identifying aggregates are
    subject to disclosure controls.
-   [ ] Profiles are versioned, inspectable, and reproducible.

### Sanitization and generation

-   [ ] Policies support keep, mask, redact, generalize, tokenize,
    synthetic, drop, and manual review.
-   [ ] Strict defaults deny raw MCP rows, source writes, and external
    model calls.
-   [ ] Synthetic generation supports Faker, inferred formats, learned
    distributions, null/falsy behavior, and cross-column constraints.
-   [ ] Same seed and versions produce repeatable output where promised.
-   [ ] Validation reports hard constraint failures and soft fidelity
    deviations separately.
-   [ ] The generator never silently relaxes a hard constraint.
-   [ ] Synthetic data is never marketed as anonymous solely because it
    was generated.

### Outputs

-   [ ] JSONL preserves record structure and JSON null.
-   [ ] CSV documents null encoding and protects against spreadsheet
    formula injection in safe-export mode.
-   [ ] Database export targets an explicitly approved destination and
    supports rollback where transactions permit.
-   [ ] Manifests contain provenance and configuration but no secrets or
    raw samples.

### CLI and MCP

-   [ ] CLI supports source management, discovery, profiling, policy
    validation, sanitization preview/export, generation, validation, and
    job inspection.
-   [ ] MCP stdio and optional authenticated HTTP modes share the same
    application services and policy checks.
-   [ ] MCP tools have strict schemas, response limits, authorization
    checks, safe errors, and cancellation.
-   [ ] No arbitrary SQL/Python execution, source writes, or policy
    mutation is exposed through MCP in v1.
-   [ ] Tool output cannot disclose data excluded by policy.

### Quality and operations

-   [ ] Unit, property-based, connector, integration, and security tests
    run in CI.
-   [ ] Logs and errors pass secret-leak tests.
-   [ ] Temporary data is cleaned up after success, failure, and
    cancellation.
-   [ ] Documentation states supported versions, required permissions,
    query-cost behavior, limitations, and privacy boundaries.

## 19. Open decisions to resolve during implementation

These decisions should be captured as short Architecture Decision
Records (ADRs), with a default selected by the technical lead:

1.  **Project name and license:** default to a permissive OSS license
    such as Apache-2.0, subject to dependency and organizational review.
2.  **MCP SDK and protocol version:** pin the selected stable SDK and
    test its exact tool/resource behavior.
3.  **Remote authentication:** prefer integration with an established
    OIDC-capable gateway or deployment identity layer rather than custom
    password authentication.
4.  **Secret backend:** start with environment references and OS
    credential stores; add cloud secret managers as separate adapters.
5.  **Local state:** SQLite for job metadata and profile indexing, with
    restrictive permissions; no raw source rows.
6.  **Privacy enhancements:** keep differential privacy out of v1 until
    a precise mechanism, threat model, budget accounting, and validation
    plan exist.
7.  **MaxCompute compatibility:** verify the current official driver,
    supported Java versions, authentication modes, and dialect before
    freezing the bridge contract.
8.  **Spreadsheet formula semantics:** default to profiling
    displayed/evaluated values where supported, while recording formula
    presence without exporting formula text unless policy permits.
9.  **Profile retention:** default to indefinite retention only for
    sanitized aggregate artifacts; make retention configurable and
    provide deletion commands.
10. **Packaging:** publish a Python package and CLI, plus a versioned
    JVM sidecar distribution. Provide a container image for remote MCP
    deployments.

## 20. Documentation deliverables

The repository should include:

-   `README.md`: purpose, quickstart, supported sources, CLI/MCP
    examples, limitations, and security defaults.
-   `SPEC.md`: this specification.
-   `SECURITY.md`: threat model, reporting process, data handling, and
    supported deployment modes.
-   `docs/connectors.md`: permissions and configuration for each source.
-   `docs/profiling.md`: algorithms, sampling limitations, budgets, and
    confidence interpretation.
-   `docs/policies.md`: policy schema and examples.
-   `docs/synthetic-data.md`: generation strategies, fidelity metrics,
    privacy limitations, and seed reproducibility.
-   `docs/mcp.md`: supported transports, tools, authentication, and
    client setup.
-   `docs/operations.md`: deployment, logging, upgrades, backup, and
    retention.
-   `docs/adr/`: architecture decision records.

## 21. Definition of done for any feature

A feature is done only when it includes: - Typed implementation and
validation. - Unit tests and relevant property-based tests. - Policy and
privacy review. - Bounded execution and cancellation behavior. - Safe
logs and structured errors. - Documentation and examples. - Connector
capability handling where applicable. - A clear failure mode and
acceptance test.

The central product contract is simple: **learn data behavior through
bounded, inspectable evidence; enforce privacy policy before disclosure;
generate data with explicit constraints; validate the result; and make
every important decision traceable.**
