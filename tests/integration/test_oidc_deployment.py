"""Live end-to-end: a running `qc mcp --transport http` in OIDC mode (SPEC 12.4, ADR-003).

The unit and HTTP tests call the verifier directly. This starts the **actual server process**
against a real identity provider, obtains a real token, and asserts the deployment boundary:
a valid token gets `200`, a token minted for another audience gets `401`, and no token gets
`401`.

Skipped unless the environment names a provider, a password-grant client and its user:

    QC_OIDC_ISSUER=https://idp.example.com/realms/qc
    QC_OIDC_AUDIENCE=qc-mcp
    QC_OIDC_CLIENT_ID=qc-mcp
    QC_OIDC_CLIENT_SECRET=...
    QC_OIDC_USERNAME=alice
    QC_OIDC_PASSWORD=...
    QC_OIDC_WRONG_CLIENT_ID=other-mcp          # a client without the QC audience mapper
    QC_OIDC_WRONG_CLIENT_SECRET=...

See `docs/operations.md` for the measured Keycloak run.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ISSUER = os.environ.get("QC_OIDC_ISSUER")
AUDIENCE = os.environ.get("QC_OIDC_AUDIENCE")
CLIENT_ID = os.environ.get("QC_OIDC_CLIENT_ID")
CLIENT_SECRET = os.environ.get("QC_OIDC_CLIENT_SECRET")
USERNAME = os.environ.get("QC_OIDC_USERNAME")
PASSWORD = os.environ.get("QC_OIDC_PASSWORD")
WRONG_ID = os.environ.get("QC_OIDC_WRONG_CLIENT_ID")
WRONG_SECRET = os.environ.get("QC_OIDC_WRONG_CLIENT_SECRET")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all([ISSUER, AUDIENCE, CLIENT_ID, CLIENT_SECRET, USERNAME, PASSWORD, WRONG_ID]),
        reason="set QC_OIDC_* to run the live deployment test",
    ),
]

REPO = Path(__file__).resolve().parents[2]
REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "live-test", "version": "0"},
    },
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _token(client_id: str, secret: str) -> str:
    response = httpx.post(
        f"{ISSUER}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": secret,
            "username": USERNAME,
            "password": PASSWORD,
            "scope": "qc:mcp",
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("qc-oidc-live")
    subprocess.run(
        [sys.executable, "-m", "qc.cli.app", "init", "--root", str(root)],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    config = root / "qc.toml"
    text = config.read_text()
    port = _free_port()
    text = text.replace("[mcp.http]\nenabled = false", '[mcp.http]\nenabled = true\nmode = "oidc"')
    text = text.replace("port = 8765", f"port = {port}")
    text += (
        "\n[mcp.http.oidc]\n"
        f'issuer_url = "{ISSUER}"\n'
        f'audience = "{AUDIENCE}"\n'
        'required_scopes = ["qc:mcp"]\n'
    )
    config.write_text(text)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "qc.cli.app",
            "mcp",
            "--transport",
            "http",
            "--root",
            str(root),
        ],
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            httpx.post(url, json=REQUEST, timeout=1)
            break
        except httpx.HTTPError:
            if process.poll() is not None:
                output = process.stdout.read().decode() if process.stdout else ""
                pytest.fail(f"server exited early:\n{output}")
            time.sleep(0.3)
    try:
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


def _initialize(url: str, token: str | None) -> httpx.Response:
    headers = {
        "accept": "application/json, text/event-stream",
        **({"authorization": f"Bearer {token}"} if token else {}),
    }
    return httpx.post(url, json=REQUEST, headers=headers, timeout=10)


def test_a_real_token_is_accepted_by_the_running_server(deployment: str) -> None:
    response = _initialize(deployment, _token(str(CLIENT_ID), str(CLIENT_SECRET)))

    assert response.status_code == 200
    # SSE framing or plain JSON, depending on the transport's answer mode.
    assert '"protocolVersion"' in response.text


def test_a_token_for_another_audience_is_refused_by_the_running_server(deployment: str) -> None:
    wrong = _token(str(WRONG_ID), str(WRONG_SECRET))

    assert _initialize(deployment, wrong).status_code == 401


def test_no_token_is_refused_by_the_running_server(deployment: str) -> None:
    assert _initialize(deployment, None).status_code == 401
