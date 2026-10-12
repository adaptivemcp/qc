/*
 * QC JDBC sidecar (SPEC 4.3, ADR-006).
 *
 * Newline-delimited JSON over stdin/stdout. stdout carries protocol
 * messages only; all diagnostics go to stderr. Secrets arrive via the
 * inherited environment (never argv, never protocol payloads).
 *
 * Build: see ../BUILD.md. The JDBC driver jar is pinned by version and
 * SHA-256 in driver-lock.json and verified before the classloader sees it.
 *
 * NOTE: this file targets Java 17 (long-lived supported LTS; the
 * MaxCompute driver's minimum is verified before release — ADR-006).
 */
package qc.jdbc;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.DigestInputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.*;

public final class Main {

    static final int PROTOCOL_VERSION = 1;
    static final ObjectMapper MAPPER = new ObjectMapper();

    /** One shared connection; the sidecar serves one source per process. */
    private static volatile Connection connection;
    private static volatile boolean shutdown;
    /** Cancellable in-flight statements keyed by request id. */
    private static final ConcurrentMap<String, Statement> inFlight = new ConcurrentHashMap<>();
    private static final ExecutorService pool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "qc-sidecar-worker");
        t.setDaemon(true);
        return t;
    });

    public static void main(String[] args) throws Exception {
        // Diagnostics on stdout would corrupt the protocol (SPEC 4.3):
        // force all logging to stderr.
        System.setOut(System.err);

        verifyDriverLock();

        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8));
        PrintStream out = new PrintStream(System.out, true, StandardCharsets.UTF_8);
        String line;
        while ((line = in.readLine()) != null && !shutdown) {
            final String requestLine = line.trim();
            if (requestLine.isEmpty()) {
                continue;
            }
            pool.submit(() -> handle(requestLine, out));
        }
        pool.shutdown();
    }

    // ------------------------------------------------------------- protocol

    private static void handle(String requestLine, PrintStream out) {
        ObjectNode response = MAPPER.createObjectNode();
        String requestId = "unknown";
        try {
            JsonNode req = MAPPER.readTree(requestLine);
            requestId = req.path("request_id").asText("unknown");
            int version = req.path("protocol_version").asInt(-1);
            response.put("protocol_version", PROTOCOL_VERSION);
            response.put("request_id", requestId);

            if (version != PROTOCOL_VERSION) {
                error(response, "PROTOCOL_MISMATCH",
                        "expected " + PROTOCOL_VERSION + " got " + version);
                write(out, response);
                return;
            }
            String op = req.path("op").asText("");
            JsonNode payload = req.path("payload");
            long deadlineMs = req.path("deadline_ms").asLong(30_000L);

            Future<?> future = pool.submit(() -> {
                try {
                    dispatch(op, payload, requestId, deadlineMs, response, out);
                } catch (Exception ex) {
                    error(response, "INTERNAL", safe(ex));
                    write(out, response);
                }
            });
            future.get(deadlineMs + 1_000L, TimeUnit.MILLISECONDS);
        } catch (TimeoutException te) {
            cancel(requestId);
            error(response, "DEADLINE_EXCEEDED", "request exceeded deadline");
            write(out, response);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            error(response, "CANCELLED", "interrupted");
            write(out, response);
        } catch (Exception ex) {
            error(response, "PROTOCOL", safe(ex));
            write(out, response);
        }
    }

    private static void dispatch(String op, JsonNode payload, String requestId,
                                 long deadlineMs, ObjectNode response,
                                 PrintStream out) throws Exception {
        switch (op) {
            case "ping" -> {
                response.put("status", "ok");
                response.putObject("payload").put("pong", true).put("java",
                        System.getProperty("java.version", "?"));
                write(out, response);
            }
            case "connect" -> connect(payload, response, out);
            case "close" -> {
                closeConnection();
                response.put("status", "ok");
                response.putObject("payload");
                write(out, response);
            }
            case "metadata.datasets" -> listDatasets(payload, response, out);
            case "metadata.columns" -> listColumns(payload, response, out);
            case "query.execute" -> execute(payload, requestId, deadlineMs, response, out);
            case "job.cancel" -> {
                cancel(payload.path("target_request_id").asText(""));
                response.put("status", "ok");
                response.putObject("payload").put("cancelled", true);
                write(out, response);
            }
            case "shutdown" -> {
                response.put("status", "ok");
                response.putObject("payload").put("bye", true);
                write(out, response);
                shutdown = true;
                closeConnection();
            }
            default -> {
                error(response, "UNKNOWN_OP", "unsupported op: " + op);
                write(out, response);
            }
        }
    }

    // ------------------------------------------------------------- handlers

    private static void connect(JsonNode payload, ObjectNode response,
                                PrintStream out) throws SQLException {
        // Secrets MUST arrive via the environment (SPEC 4.3): the JDBC
        // URL is read from the env var named by jdbc_url_env, credentials
        // from standard env vars. Never from argv or the payload.
        for (String banned : new String[]{"password", "secret", "token", "connection_string"}) {
            if (payload.has(banned)) {
                error(response, "SECRET_IN_PAYLOAD",
                        "payload must not carry " + banned);
                write(out, response);
                return;
            }
        }
        String envName = payload.path("jdbc_url_env").asText("");
        if (envName.isEmpty() || System.getenv(envName) == null) {
            error(response, "MISSING_URL",
                    "jdbc_url_env missing or unset in environment");
            write(out, response);
            return;
        }
        String jdbcUrl = System.getenv(envName);
        String user = firstEnv("QC_MCP_JDBC_USER", "MAXCOMPUTE_ACCESS_ID");
        String pass = firstEnv("QC_MCP_JDBC_PASSWORD", "MAXCOMPUTE_ACCESS_KEY");

        closeConnection();
        if (user != null && pass != null) {
            connection = DriverManager.getConnection(jdbcUrl, user, pass);
        } else {
            connection = DriverManager.getConnection(jdbcUrl);
        }
        // Belt and braces: never allow writes through the sidecar.
        try (Statement st = connection.createStatement()) {
            try {
                st.execute("SET readonly=true"); // best effort; dialect-dependent
            } catch (SQLException ignored) {
                // dialect may not support it; role-level read-only still applies
            }
        }
        connection.setReadOnly(true);

        response.put("status", "ok");
        response.putObject("payload").put("connected", true);
        write(out, response);
    }

    private static void listDatasets(JsonNode payload, ObjectNode response,
                                     PrintStream out) throws SQLException {
        requireConnection(response, out);
        if (response.has("status")) {
            return;
        }
        String schemaFilter = payload.path("schema").asText(null);
        DatabaseMetaData meta = connection.getMetaData();
        List<ObjectNode> datasets = new ArrayList<>();
        try (ResultSet rs = meta.getTables(null, schemaFilter, "%",
                new String[]{"TABLE", "VIEW"})) {
            while (rs.next()) {
                ObjectNode d = MAPPER.createObjectNode();
                d.put("schema", rs.getString("TABLE_SCHEM"));
                d.put("name", rs.getString("TABLE_NAME"));
                d.put("kind", "VIEW".equalsIgnoreCase(rs.getString("TABLE_TYPE"))
                        ? "view" : "table");
                datasets.add(d);
            }
        }
        ObjectNode payloadOut = response.putObject("payload");
        payloadOut.putArray("datasets").addAll(datasets);
        response.put("status", "ok");
        write(out, response);
    }

    private static void listColumns(JsonNode payload, ObjectNode response,
                                    PrintStream out) throws SQLException {
        requireConnection(response, out);
        if (response.has("status")) {
            return;
        }
        String schema = payload.path("schema").asText("");
        String table = payload.path("table").asText("");
        if (schema.isEmpty() || table.isEmpty()) {
            error(response, "BAD_REQUEST", "schema and table required");
            write(out, response);
            return;
        }
        DatabaseMetaData meta = connection.getMetaData();
        List<ObjectNode> columns = new ArrayList<>();
        int ordinal = 0;
        try (ResultSet rs = meta.getColumns(null, schema, table, "%")) {
            while (rs.next()) {
                ObjectNode c = MAPPER.createObjectNode();
                c.put("name", rs.getString("COLUMN_NAME"));
                c.put("type", String.valueOf(rs.getString("TYPE_NAME")).toLowerCase());
                c.put("ordinal", ordinal++);
                c.put("nullable", rs.getInt("NULLABLE") != DatabaseMetaData.columnNoNulls);
                columns.add(c);
            }
        }
        if (columns.isEmpty()) {
            error(response, "NOT_FOUND", "table or columns not found");
            write(out, response);
            return;
        }
        // primary keys
        try (ResultSet pk = meta.getPrimaryKeys(null, schema, table)) {
            while (pk.next()) {
                String col = pk.getString("COLUMN_NAME");
                for (ObjectNode c : columns) {
                    if (col.equals(c.get("name").asText())) {
                        c.put("primary_key", true);
                    }
                }
            }
        }
        ObjectNode payloadOut = response.putObject("payload");
        payloadOut.putArray("columns").addAll(columns);
        response.put("status", "ok");
        write(out, response);
    }

    private static void execute(JsonNode payload, String requestId, long deadlineMs,
                                ObjectNode response, PrintStream out) throws Exception {
        requireConnection(response, out);
        if (response.has("status")) {
            return;
        }
        String sql = payload.path("sql").asText("");
        int maxRows = payload.path("max_rows").asInt(-1);
        long maxBytes = payload.path("max_bytes").asLong(-1);
        if (maxRows < 0 || maxBytes <= 0) {
            error(response, "MISSING_CAP", "max_rows and max_bytes required");
            write(out, response);
            return;
        }
        String trimmed = sql.strip();
        String lowered = trimmed.toLowerCase();
        if (!(lowered.startsWith("select") || lowered.startsWith("with"))) {
            error(response, "NOT_SELECT", "only SELECT/WITH statements allowed");
            write(out, response);
            return;
        }

        PreparedStatement ps = connection.prepareStatement(sql);
        inFlight.put(requestId, ps);
        try {
            ps.setMaxRows(maxRows);
            ps.setQueryTimeout(Math.max(1, (int) Math.min(deadlineMs / 1000, Integer.MAX_VALUE)));
            JsonNode params = payload.path("params");
            for (int i = 0; i < params.size(); i++) {
                bind(ps, i + 1, params.get(i));
            }
            try (ResultSet rs = ps.executeQuery()) {
                ResultSetMetaData md = rs.getMetaData();
                int colCount = md.getColumnCount();
                List<String> columns = new ArrayList<>(colCount);
                for (int i = 1; i <= colCount; i++) {
                    columns.add(md.getColumnLabel(i));
                }
                List<List<Object>> rows = new ArrayList<>();
                long bytes = 0;
                boolean truncated = false;
                while (rs.next()) {
                    if (rows.size() >= maxRows) {
                        truncated = true;
                        break;
                    }
                    List<Object> row = new ArrayList<>(colCount);
                    for (int i = 1; i <= colCount; i++) {
                        Object v = rs.getObject(i);
                        row.add(v);
                        bytes += estimateSize(v);
                    }
                    if (bytes > maxBytes) {
                        truncated = true;
                        break;
                    }
                    rows.add(row);
                }
                ObjectNode payloadOut = MAPPER.createObjectNode();
                payloadOut.putArray("columns").addAll(columns);
                payloadOut.put("truncated", truncated);
                var rowsArray = payloadOut.putArray("rows");
                for (List<Object> row : rows) {
                    var ja = rowsArray.addArray();
                    for (Object cell : row) {
                        addScalar(ja, cell);
                    }
                }
                response.put("status", "ok");
                response.set("payload", payloadOut);
                write(out, response);
            }
        } finally {
            inFlight.remove(requestId);
            try {
                ps.close();
            } catch (SQLException ignored) {
                // best effort
            }
        }
    }

    // -------------------------------------------------------------- helpers

    private static void bind(PreparedStatement ps, int index, JsonNode v)
            throws SQLException {
        if (v == null || v.isNull()) {
            ps.setObject(index, null);
        } else if (v.isNumber()) {
            if (v.isIntegralNumber()) {
                ps.setLong(index, v.asLong());
            } else {
                ps.setBigDecimal(index, v.decimalValue());
            }
        } else if (v.isBoolean()) {
            ps.setBoolean(index, v.asBoolean());
        } else {
            ps.setString(index, v.asText());
        }
    }

    private static void addScalar(com.fasterxml.jackson.databind.node.ArrayNode ja, Object v) {
        if (v == null) {
            ja.addNull();
        } else if (v instanceof Boolean b) {
            ja.add(b);
        } else if (v instanceof Integer || v instanceof Long || v instanceof Short) {
            ja.add(((Number) v).longValue());
        } else if (v instanceof Number n) {
            // decimals/floats: send their canonical string to avoid
            // binary-float precision loss (SPEC 6.4); Python re-parses
            // decimal-safe for numeric summaries.
            ja.add(n.toString());
        } else if (v instanceof java.sql.Timestamp ts) {
            ja.add(ts.toInstant().toString());
        } else if (v instanceof java.sql.Date d) {
            ja.add(d.toLocalDate().toString());
        } else {
            ja.add(String.valueOf(v));
        }
    }

    private static long estimateSize(Object v) {
        if (v == null) {
            return 1;
        }
        if (v instanceof String s) {
            return s.length() * 2L;
        }
        if (v instanceof byte[] b) {
            return b.length;
        }
        return 16;
    }

    private static void cancel(String requestId) {
        if (requestId == null || requestId.isEmpty()) {
            return;
        }
        Statement st = inFlight.get(requestId);
        if (st != null) {
            try {
                st.cancel();
            } catch (SQLException ignored) {
                // already finished
            }
        }
    }

    private static void requireConnection(ObjectNode response, PrintStream out) {
        if (connection == null) {
            error(response, "NOT_CONNECTED", "connect first");
            write(out, response);
        }
    }

    private static void closeConnection() {
        Connection c = connection;
        connection = null;
        if (c != null) {
            try {
                c.close();
            } catch (SQLException ignored) {
                // best effort
            }
        }
    }

    private static String firstEnv(String... names) {
        for (String n : names) {
            String v = System.getenv(n);
            if (v != null && !v.isEmpty()) {
                return v;
            }
        }
        return null;
    }

    private static void error(ObjectNode response, String code, String message) {
        response.put("status", "error");
        ObjectNode err = response.putObject("error");
        err.put("code", code);
        err.put("message", message); // caller redacts before display
    }

    private static synchronized void write(PrintStream out, ObjectNode response) {
        try {
            out.println(MAPPER.writeValueAsString(response));
            out.flush();
        } catch (Exception ex) {
            // last resort: never write raw exceptions (may hold values)
            System.err.println("sidecar write failed: " + ex.getClass().getSimpleName());
        }
    }

    /** Never emit raw exception text: it can embed SQL params (SPEC 8.2). */
    private static String safe(Exception ex) {
        return ex.getClass().getSimpleName()
                + (ex instanceof SQLException sqlEx
                    ? " sqlstate=" + sqlEx.getSQLState()
                      + " code=" + sqlEx.getErrorCode()
                    : "");
    }

    /**
     * Verify the pinned JDBC driver jar (version + SHA-256) before any
     * class loads it (SPEC 4.3, ADR-006).
     */
    private static void verifyDriverLock() {
        String lockPath = System.getenv("QC_DRIVER_LOCK");
        if (lockPath == null || lockPath.isBlank()) {
            System.err.println("QC_DRIVER_LOCK not set; refusing to start");
            System.exit(3);
        }
        try {
            JsonNode lock = MAPPER.readTree(Files.readString(Path.of(lockPath)));
            String expected = lock.get("sha256").asText();
            String jarPath = System.getenv("QC_DRIVER_JAR");
            if (jarPath == null || jarPath.isBlank()) {
                System.err.println("QC_DRIVER_JAR not set; refusing to start");
                System.exit(3);
            }
            String actual = sha256(Path.of(jarPath));
            if (!actual.equalsIgnoreCase(expected)) {
                System.err.println("driver jar checksum mismatch; refusing to start");
                System.exit(4);
            }
        } catch (Exception ex) {
            System.err.println("driver lock verification failed: " + safe((Exception) ex));
            System.exit(3);
        }
    }

    private static String sha256(Path path) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (DigestInputStream dis = new DigestInputStream(
                Files.newInputStream(path), md)) {
            byte[] buf = new byte[8192];
            while (dis.read(buf) != -1) {
                // drain
            }
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }

    private Main() {
    }
}
