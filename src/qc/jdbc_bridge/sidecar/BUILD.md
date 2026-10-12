# QC JDBC sidecar

JVM process that owns JDBC driver loading, connections, metadata, and
parameterized query execution for sources that are impractical to drive
natively from Python (Alibaba MaxCompute first — SPEC 4.3, ADR-006).

## Protocol

Newline-delimited JSON over stdin/stdout (`qc/jdbc_bridge/protocol.py`):

- `protocol_version: 1`, `request_id`, `op`, `deadline_ms`, `payload`
- responses: `status: ok|error`, structured `error{code,message,detail}`
- **stdout carries protocol messages only**; diagnostics go to stderr
- SQL is only ever `SELECT`/`WITH`, always with explicit `max_rows` and
  `max_bytes` caps and a deadline; parameters are a scalar list bound
  with `PreparedStatement`
- secrets arrive via the inherited environment (`QC_MCP_JDBC_URL_*`,
  `QC_MCP_JDBC_USER`, `QC_MCP_JDBC_PASSWORD`) — never argv, never payload
- `job.cancel` cancels the in-flight statement for a request id

## Build

Requires **Java 17+** and the pinned driver jar (see `driver-lock.json`).

```bash
# 1. fetch + verify the driver (pin version + sha256 in driver-lock.json)
sha256sum odps-jdbc-driver.jar    # must match driver-lock.json

# 2. compile (Jackson required on the classpath for JSON)
javac -cp "lib/*" -d build $(find src -name '*.java')

# 3. run — QC launches this itself; manual smoke test:
export QC_DRIVER_LOCK=$PWD/driver-lock.json
export QC_DRIVER_JAR=$PWD/odps-jdbc-driver.jar
export QC_MCP_JDBC_URL='jdbc:odps://endpoint!project'
export QC_MCP_JDBC_USER='...' QC_MCP_JDBC_PASSWORD='...'
echo '{"protocol_version":1,"request_id":"r1","op":"ping","payload":{}}' | \
  java -cp build:lib/* qc.jdbc.Main
```

The sidecar **refuses to start** when `QC_DRIVER_LOCK`/`QC_DRIVER_JAR`
are missing or the jar checksum does not match.

## Security posture

- read-only JDBC connection (`Connection.setReadOnly` + dialect best effort)
- SELECT/WITH only; caps enforced before execution
- credentials only in environment; payload key scan rejects smuggled secrets
- no query values or connection strings logged; SQLSTATE/errno only
- trusted local process: run with restricted filesystem/network rights
  where practical (systemd sandbox, container, `DynamicUser=`)

## Live testing

Python-side contract tests run against `tests/contract/fake_sidecar.py`
(no JVM needed). Live MaxCompute tests are opt-in:

```bash
export QC_TEST_MAXCOMPUTE_DSN='jdbc:odps://...'
export QC_DRIVER_LOCK=... QC_DRIVER_JAR=...
uv run pytest tests/integration -m integration -k maxcompute
```
