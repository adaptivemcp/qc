"""Spreadsheet normalization tests (SPEC 6.7) + mocked sheet adapters."""

from __future__ import annotations

from typing import Any

import pytest

from qc.connectors.sheet_normalize import (
    KIND_BLANK,
    KIND_FORMULA_EMPTY,
    KIND_VALUE,
    NormalizedCell,
    SheetNormalization,
    normalize_rows,
    schema_from_headers,
    used_range,
)
from qc.domain.enums import LogicalType
from qc.domain.errors import InvalidArgumentsError, ValidationError
from qc.domain.models import DatasetRef


class TestOptions:
    def test_defaults(self) -> None:
        cfg = SheetNormalization.from_options({})
        assert cfg.header_row == 0
        assert cfg.merged_cells == "reject"
        assert cfg.duplicate_headers == "suffix"

    def test_option_parsing(self) -> None:
        cfg = SheetNormalization.from_options(
            {
                "sheet_header_row": "2",
                "sheet_skip_columns": "notes, temp ",
                "sheet_profile_formulas": "true",
                "sheet_locale": "de_DE",
                "sheet_data_range": "A1:F100",
            }
        )
        assert cfg.header_row == 2
        assert cfg.skip_columns == ["notes", "temp"]
        assert cfg.profile_formulas is True
        assert cfg.locale == "de_DE"
        assert cfg.data_range == "A1:F100"

    def test_invalid_merged_policy(self) -> None:
        with pytest.raises(InvalidArgumentsError):
            SheetNormalization.from_options({"merged_cells": "explode"})

    def test_negative_header_row(self) -> None:
        with pytest.raises(InvalidArgumentsError):
            SheetNormalization.from_options({"header_row": "-1"})


class TestNormalization:
    def test_falsy_variants_distinguishable(self) -> None:
        rows = [
            ["a", "b", "c", "d", "e"],
            [None, "", "   ", 0, False],
        ]
        kept, out, _ = normalize_rows(rows, config=SheetNormalization())
        assert kept == ["a", "b", "c", "d", "e"]
        row = out[0]
        assert row["a"].value is None and row["a"].kind == KIND_BLANK
        assert row["b"].value == "" and row["b"].kind == KIND_VALUE  # empty string cell
        assert row["c"].value == "   "  # whitespace-only preserved
        assert row["d"].value == 0
        assert row["e"].value is False

    def test_short_row_missing_cells(self) -> None:
        rows = [["x", "y", "z"], ["1"]]
        _kept, out, _ = normalize_rows(rows, config=SheetNormalization())
        assert out[0]["y"].kind == KIND_BLANK or out[0]["y"].value is None

    def test_blank_header_named(self) -> None:
        rows = [["name", None], ["alice", "1"]]
        kept, _out, warnings = normalize_rows(rows, config=SheetNormalization())
        assert "column_1" in kept
        assert any("blank header" in w for w in warnings)

    def test_duplicate_header_suffix(self) -> None:
        rows = [["id", "id"], ["1", "2"]]
        kept, out, warnings = normalize_rows(rows, config=SheetNormalization())
        assert kept == ["id", "id_2"]
        assert any("duplicate" in w for w in warnings)
        assert out[0]["id"].value == 1
        assert out[0]["id_2"].value == 2

    def test_duplicate_header_error(self) -> None:
        rows = [["id", "id"], ["1", "2"]]
        with pytest.raises(ValidationError):
            normalize_rows(rows, config=SheetNormalization(duplicate_headers="error"))

    def test_duplicate_header_first(self) -> None:
        rows = [["id", "id"], ["1", "2"]]
        kept, _out, _w = normalize_rows(rows, config=SheetNormalization(duplicate_headers="first"))
        assert kept[0] == "id"

    def test_header_row_offset(self) -> None:
        rows = [["title"], ["h1", "h2"], ["v1", "v2"]]
        kept, out, _ = normalize_rows(rows, config=SheetNormalization(header_row=1))
        assert kept == ["h1", "h2"]
        assert out[0]["h1"].value == "v1"

    def test_header_row_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            normalize_rows([["a"]], config=SheetNormalization(header_row=5))

    def test_stop_on_blank_row(self) -> None:
        rows = [["h"], ["1"], [""], ["3"]]
        _kept, out, warnings = normalize_rows(
            rows, config=SheetNormalization(stop_on_blank_row=True)
        )
        assert len(out) == 1
        assert any("stopped at blank row" in w for w in warnings)

    def test_skip_columns(self) -> None:
        rows = [["keep", "drop"], ["1", "secret"]]
        kept, out, _ = normalize_rows(rows, config=SheetNormalization(skip_columns=["drop"]))
        assert kept == ["keep"]
        assert "drop" not in out[0]

    def test_formula_empty_kind(self) -> None:
        # provider_types aligns with the full sheet: header row + data row
        rows = [["f"], [""]]
        _kept, out, _ = normalize_rows(
            rows,
            config=SheetNormalization(profile_formulas=True),
            provider_types=[["VALUE"], ["FORMULA_EMPTY"]],
        )
        assert out[0]["f"].kind == KIND_FORMULA_EMPTY

    def test_formula_text_kind(self) -> None:
        rows = [["f"], ['=A1&""']]
        _kept, out, _ = normalize_rows(
            rows,
            config=SheetNormalization(profile_formulas=True),
            provider_types=[["VALUE"], ["FORMULA"]],
        )
        assert out[0]["f"].kind == "formula"

    def test_number_coercion_and_locale(self) -> None:
        rows = [["n", "b", "d"], ["42", "TRUE", "00123"]]
        _kept, out, _ = normalize_rows(rows, config=SheetNormalization(coerce_types=True))
        assert out[0]["n"].value == 42
        assert out[0]["b"].value is True
        # leading zeros preserved: coercion must NOT destroy them
        assert out[0]["d"].value == "00123"

    def test_no_coercion(self) -> None:
        rows = [["n"], ["42"]]
        _kept, out, _ = normalize_rows(rows, config=SheetNormalization(coerce_types=False))
        assert out[0]["n"].value == "42"

    def test_dates_not_coerced(self) -> None:
        """Ambiguous dates must remain strings for the profiler (SPEC 7.6)."""
        rows = [["d"], ["03/04/2025"]]
        _kept, out, _ = normalize_rows(rows, config=SheetNormalization())
        assert out[0]["d"].value == "03/04/2025"

    def test_empty_sheet(self) -> None:
        kept, out, warnings = normalize_rows([], config=SheetNormalization())
        assert kept == [] and out == []
        assert warnings

    def test_used_range(self) -> None:
        assert used_range([["a", "b"], ["1", "2"]]) == "A1:B2"
        assert used_range([]) == "A1:A1"


class TestSchema:
    def test_schema_from_normalized(self) -> None:
        rows = [["age", "name"], ["34", "alice"], ["", "bob"]]
        kept, out, _ = normalize_rows(rows, config=SheetNormalization())
        schema = schema_from_headers(
            kept,
            out,
            dataset=DatasetRef(source_id="s", name="Sheet1"),
            fingerprint="gs_x",
            sheet_name="Sheet1",
        )
        types = {c.name: c.logical_type for c in schema.columns}
        assert types["age"] == LogicalType.INTEGER
        assert types["name"] == LogicalType.STRING
        assert schema.row_estimate == 2


# --------------------------------------------------------------- mocked HTTP


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHTTP:
    """Records requests; serves canned responses keyed by path substring."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []

    async def get(
        self, path: str, params: dict | None = None, headers: dict | None = None
    ) -> FakeResponse:
        self.requests.append({"path": path, "params": params, "headers": headers})
        return self._match(path)

    async def post(
        self, path: str, json: dict | None = None, headers: dict | None = None
    ) -> FakeResponse:
        self.requests.append({"path": path, "json": json, "headers": headers})
        return self._match(path)

    def _match(self, path: str) -> FakeResponse:
        for key, value in self.routes.items():
            if key in path:
                if isinstance(value, FakeResponse):
                    return value
                return FakeResponse(200, value)
        return FakeResponse(404, {"error": {"message": "not found"}})

    async def aclose(self) -> None:
        pass


GOOGLE_ROUTES = {
    "sheets/query": {},  # unused for google
}

GOOGLE_META = {
    "spreadsheetId": "abc123",
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "People",
                "gridProperties": {"rowCount": 100, "columnCount": 10},
            }
        },
    ],
}

GOOGLE_VALUES = {
    "range": "'People'!A:Z",
    "values": [
        ["name", "age", "email", "score"],
        ["alice", 34, "alice@example.com", 0.5],
        ["bob", "", "bob@example.com", None],
        [None, 0, "carol@example.com", 0.25],
        ["carol", "00123", "dave@example.com", 0.75],
    ],
}


@pytest.fixture
def google_source():  # type: ignore[no-untyped-def]
    from qc.connectors.google_sheets import GoogleSheetsConnector
    from qc.domain.models import SourceConfig

    config = SourceConfig(
        name="gsheet",
        connector="google-sheets",
        connection_ref="env:QC_TEST_GS_TOKEN",
        options={"spreadsheet_id": "abc123"},
    )
    http = FakeHTTP(
        {
            "/values/": GOOGLE_VALUES,
            "/abc123": GOOGLE_META,
        }
    )
    return GoogleSheetsConnector(http=http), config


@pytest.fixture
def lark_source():  # type: ignore[no-untyped-def]
    from qc.connectors.lark_sheets import LarkSheetsConnector
    from qc.domain.models import SourceConfig

    config = SourceConfig(
        name="lsheet",
        connector="lark-sheets",
        connection_ref="env:QC_TEST_LARK_TOKEN",
        options={"spreadsheet_id": "shtcn123"},
    )
    routes = {
        "sheets/query": {
            "code": 0,
            "msg": "ok",
            "data": {
                "sheets": [
                    {
                        "sheet_id": "abc001",
                        "title": "People",
                        "grid_properties": {"row_count": 100, "column_count": 10},
                    },
                ],
            },
        },
        "values_batch_get": {
            "code": 0,
            "msg": "ok",
            "data": {
                "value_ranges": [
                    {"range": "abc001!A:Z", "values": GOOGLE_VALUES["values"]},
                ],
            },
        },
    }
    return LarkSheetsConnector(http=FakeHTTP(routes)), config


@pytest.mark.asyncio
class TestGoogleSheetsMocked:
    async def test_list_datasets(self, google_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "fake-token")
        connector, config = google_source
        datasets = await connector.list_datasets(config)
        assert len(datasets) == 1
        assert datasets[0].name == "People"
        # token never persisted; auth header present
        auth = connector._http.requests[0]["headers"]["Authorization"]
        assert auth == "Bearer fake-token"

    async def test_get_schema(self, google_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "fake-token")
        connector, config = google_source
        datasets = await connector.list_datasets(config)
        schema = await connector.get_schema(config, datasets[0])
        assert schema.column_names == ["name", "age", "email", "score"]
        assert schema.source_fingerprint.startswith("gs_")

    async def test_null_count_preserves_falsy(self, google_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "fake-token")
        connector, config = google_source
        from qc.connectors.base import ProfileQuery
        from qc.domain.models import DatasetRef

        ref = DatasetRef(source_id=config.id, name="People", locator="abc123:People")
        res = await connector.execute_profile_query(
            config, ProfileQuery(kind="null_count", dataset=ref, params={"column": "age"})
        )
        # age column: 34, "", 0, "00123" -> one blank (None row name col is separate)
        # values row-wise for age: 34, "", 0, "00123": no SQL-null age cells,
        # but "" is an empty string (distinct from null)
        assert res.data["null_count"] == 0
        assert res.data["empty_string_count"] == 1

    async def test_row_count(self, google_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "fake-token")
        connector, config = google_source
        from qc.connectors.base import ProfileQuery
        from qc.domain.models import DatasetRef

        ref = DatasetRef(source_id=config.id, name="People")
        res = await connector.execute_profile_query(
            config, ProfileQuery(kind="row_count", dataset=ref)
        )
        assert res.data["row_count"] == 4

    async def test_sample_rows_kinds(self, google_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "fake-token")
        connector, config = google_source
        from qc.connectors.base import SampleRequest
        from qc.domain.models import DatasetRef

        ref = DatasetRef(source_id=config.id, name="People")
        rows = []
        async for row in connector.sample_rows(config, SampleRequest(dataset=ref, limit=10)):
            rows.append(row)
        assert len(rows) == 4
        # third data row has a blank name cell preserved as None + kind
        assert rows[2].values["name"] is None
        assert rows[2].cell_kinds["name"] == "blank"

    async def test_401_maps_to_auth_error(self, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "bad")
        from qc.connectors.google_sheets import GoogleSheetsConnector
        from qc.domain.errors import SourceError
        from qc.domain.models import SourceConfig

        http = FakeHTTP({"": FakeResponse(401, {})})
        connector = GoogleSheetsConnector(http=http)
        config = SourceConfig(
            name="g",
            connector="google-sheets",
            connection_ref="env:QC_TEST_GS_TOKEN",
            options={"spreadsheet_id": "x"},
        )
        with pytest.raises(SourceError) as exc:
            await connector.list_datasets(config)
        assert "permission denied" in exc.value.message

    async def test_rate_limit_maps_to_source_error(self, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_GS_TOKEN", "tok")
        from qc.connectors.google_sheets import GoogleSheetsConnector
        from qc.domain.errors import SourceError
        from qc.domain.models import SourceConfig

        http = FakeHTTP({"": FakeResponse(429, {})})
        connector = GoogleSheetsConnector(http=http)
        config = SourceConfig(
            name="g",
            connector="google-sheets",
            connection_ref="env:QC_TEST_GS_TOKEN",
            options={"spreadsheet_id": "x"},
        )
        with pytest.raises(SourceError) as exc:
            await connector.list_datasets(config)
        assert "rate limited" in exc.value.message


@pytest.mark.asyncio
class TestLarkSheetsMocked:
    async def test_provider_id_separate_from_name(self, lark_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_LARK_TOKEN", "fake-tenant-token")
        connector, config = lark_source
        datasets = await connector.list_datasets(config)
        assert datasets[0].name == "People"  # display name
        assert datasets[0].provider_id == "abc001"  # provider id

    async def test_schema_and_queries(self, lark_source, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_LARK_TOKEN", "fake-tenant-token")
        connector, config = lark_source
        datasets = await connector.list_datasets(config)
        schema = await connector.get_schema(config, datasets[0])
        assert schema.column_names == ["name", "age", "email", "score"]

        from qc.connectors.base import ProfileQuery

        res = await connector.execute_profile_query(
            config,
            ProfileQuery(kind="top_k", dataset=datasets[0], params={"column": "name", "k": 5}),
        )
        keys = [c["key"] for c in res.data["categories"]]
        assert "alice" in keys
        # NULL name cell appears as a falsy group
        assert any(c["falsy"] for c in res.data["categories"])

    async def test_business_error_code(self, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_LARK_TOKEN", "sekrit-lark-value-99")
        from qc.connectors.lark_sheets import LarkSheetsConnector
        from qc.domain.errors import SourceError
        from qc.domain.models import SourceConfig

        http = FakeHTTP({"": FakeResponse(200, {"code": 99991663, "msg": "token invalid"})})
        connector = LarkSheetsConnector(http=http)
        config = SourceConfig(
            name="l",
            connector="lark-sheets",
            connection_ref="env:QC_TEST_LARK_TOKEN",
            options={"spreadsheet_id": "x"},
        )
        with pytest.raises(SourceError) as exc:
            await connector.list_datasets(config)
        assert "token" in exc.value.message.lower()
        # the actual token value must never be echoed back
        assert "sekrit-lark-value-99" not in exc.value.message

    async def test_rate_limit_maps(self, monkeypatch) -> None:
        monkeypatch.setenv("QC_TEST_LARK_TOKEN", "sekrit-lark-value-99")
        from qc.connectors.lark_sheets import LarkSheetsConnector
        from qc.domain.errors import SourceError
        from qc.domain.models import SourceConfig

        http = FakeHTTP({"": FakeResponse(429, {})})
        connector = LarkSheetsConnector(http=http)
        config = SourceConfig(
            name="l",
            connector="lark-sheets",
            connection_ref="env:QC_TEST_LARK_TOKEN",
            options={"spreadsheet_id": "x"},
        )
        with pytest.raises(SourceError) as exc:
            await connector.list_datasets(config)
        assert "rate limited" in exc.value.message


def test_normalized_cell_falsy_flags() -> None:
    assert NormalizedCell(value=None, kind=KIND_BLANK).is_falsy_variant
    assert NormalizedCell(value="", kind=KIND_FORMULA_EMPTY).is_falsy_variant
    assert not NormalizedCell(value="", kind=KIND_VALUE).is_falsy_variant
