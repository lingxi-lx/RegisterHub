"""Regression tests for RegisterHub Community v1.3.1 polish:
- unified version metadata across core, /health, /api/meta, and /api/console/meta
- /api/services/proxies/import bridge into Built-in Proxy Pool (zero 404)
- CLI --from-har skeleton extraction
- run_web.bat CRLF & run_web.sh LF line-ending contract
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from core import __version__
from web import server as web_server
from web import studio_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent


def _start_ephemeral():
    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def _req_json(method: str, url: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_unified_version_and_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    srv, base = _start_ephemeral()
    try:
        st_h, health = _req_json("GET", f"{base}/health")
        assert st_h == 200
        assert health == {"ok": True, "edition": "community", "version": __version__}

        st_m, meta = _req_json("GET", f"{base}/api/meta")
        assert st_m == 200
        assert meta["version"] == __version__

        st_c, cmeta = _req_json("GET", f"{base}/api/console/meta")
        assert st_c == 200
        assert cmeta["version"] == __version__
    finally:
        srv.shutdown()
        srv.server_close()


def test_legacy_services_proxy_import_bridges_to_builtin_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    studio_bridge._BUILTIN_PROXY_STORES.clear()

    srv, base = _start_ephemeral()
    try:
        st, res = _req_json(
            "POST",
            f"{base}/api/services/proxies/import",
            {"text": "http://demo_u:secret_pw@proxy-a.example.com:8080\nsocks5://proxy-b.example.com:1080"},
        )
        assert st == 200
        assert res["ok"] is True
        assert res["added"] == 2
        assert res["total"] == 2
        assert "secret_pw" not in json.dumps(res)
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_from_har_generates_skeleton(tmp_path):
    har_file = tmp_path / "sample.har"
    out_py = tmp_path / "acme_proto.py"
    har_payload = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://api.example.com/v1/auth/signup",
                        "postData": {"text": json.dumps({"email": "u@example.com", "password": "pw"})},
                    },
                    "response": {
                        "status": 200,
                        "content": {
                            "mimeType": "application/json",
                            "text": json.dumps({"api_key": "sk-demo-1234567890abcdef"}),
                        },
                    },
                }
            ]
        }
    }
    har_file.write_text(json.dumps(har_payload), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--from-har", str(har_file), "--site", "acme", "--har-out", str(out_py)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert out_py.is_file()
    code = out_py.read_text(encoding="utf-8")
    assert "AcmeProtocolAdapter" in code
    assert "/v1/auth/signup" in code


def test_launchers_line_endings():
    for bat in REPO_ROOT.glob("*.bat"):
        raw = bat.read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b""), f"{bat.name} must use CRLF"
    sh = (REPO_ROOT / "run_web.sh").read_bytes()
    assert b"\r\n" not in sh, "run_web.sh must use LF"
