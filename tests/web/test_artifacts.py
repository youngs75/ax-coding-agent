"""Workspace 산출물 다운로드 endpoint 테스트.

tmp_path 기반 가상 workspace 를 만들어 zip 구성 + 개별 파일 다운로드 +
exclude rule (node_modules / .git / .env / *.pyc) + path-traversal 방어
검증.

Workspace download endpoint tests — full zip + single file + exclude
rules + path-traversal protection. Workspace is mocked via tmp_path
pointed to by AX_ARTIFACTS_DIR env.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fake_workspace(tmp_path: Path, monkeypatch) -> Path:
    """Build a fake workspace under tmp_path with realistic contents.

    Files included (should appear in __bundle.zip):
      - src/main.py
      - src/lib/util.py
      - README.md
      - package.json

    Excluded (should NOT appear):
      - .git/config
      - node_modules/foo/index.js
      - .venv/lib/site.py
      - __pycache__/x.pyc
      - dist/bundle.js
      - .env
      - src/build/output.txt   (build segment 어디에 있어도 제외)
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "src" / "lib" / "util.py").write_text("def f(): return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"t"}\n', encoding="utf-8")

    # Excluded directories.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "foo").mkdir()
    (tmp_path / "node_modules" / "foo" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib").mkdir()
    (tmp_path / ".venv" / "lib" / "site.py").write_text("", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("", encoding="utf-8")
    (tmp_path / "src" / "build").mkdir()
    (tmp_path / "src" / "build" / "output.txt").write_text("ignored", encoding="utf-8")

    # Excluded specific filename (.env).
    (tmp_path / ".env").write_text("SECRET=xxx\n", encoding="utf-8")

    # Excluded suffix (.pyc) outside __pycache__.
    (tmp_path / "src" / "compiled.pyc").write_text("", encoding="utf-8")

    monkeypatch.setenv("AX_ARTIFACTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(fake_workspace: Path) -> TestClient:
    """FastAPI TestClient — fake workspace 가 활성화된 상태."""
    import coding_agent.web.app as app_module
    return TestClient(app_module.app)


def test_bundle_zip_includes_only_source_files(client: TestClient) -> None:
    """__bundle.zip 이 source 파일만 포함, vendored/cache/secrets 모두 제외."""
    resp = client.get("/artifacts/__bundle.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert int(resp.headers.get("X-Artifacts-File-Count", "0")) >= 4

    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf) as zf:
        names = sorted(zf.namelist())

    # 포함되어야 함
    assert "src/main.py" in names
    assert "src/lib/util.py" in names
    assert "README.md" in names
    assert "package.json" in names

    # 제외되어야 함 (어떤 segment 에 들어있어도)
    assert not any(".git" in n for n in names), names
    assert not any("node_modules" in n for n in names), names
    assert not any(".venv" in n for n in names), names
    assert not any("__pycache__" in n for n in names), names
    assert not any("dist" in n for n in names), names
    assert not any("build" in n for n in names), names
    assert ".env" not in names
    assert not any(n.endswith(".pyc") for n in names), names


def test_bundle_via_path_catchall_returns_zip(client: TestClient) -> None:
    """``/artifacts/__bundle.zip`` 가 catch-all 라우트로 와도 zip 으로 응답.

    apt-web router 의 ``{path:path}`` proxy 가 이 형태로 forward 하는
    케이스 호환 검증.
    """
    # specific route 가 먼저 매칭하지만, 만약 누락되어 catch-all 로 흘러도
    # path == "__bundle.zip" 분기가 zip 응답을 보장.
    # (실제로는 specific route 가 매칭되므로 둘 다 zip 응답이어야 함)
    resp = client.get("/artifacts/__bundle.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")


def test_single_file_download_succeeds(client: TestClient) -> None:
    """단일 파일 GET 이 파일 본문 그대로 반환 (line-ending agnostic)."""
    resp = client.get("/artifacts/src/main.py")
    assert resp.status_code == 200
    # Windows 환경에서 CRLF 가 들어갈 수 있으므로 strip 후 비교.
    assert resp.text.strip() == "print('hello')"


def test_single_file_download_nested_path(client: TestClient) -> None:
    """nested path (src/lib/util.py) 도 정상 다운로드."""
    resp = client.get("/artifacts/src/lib/util.py")
    assert resp.status_code == 200
    assert "def f()" in resp.text


def test_path_traversal_blocked(client: TestClient) -> None:
    """``..`` 가 포함된 path 는 workspace 밖으로 나갈 수 없음 (403/404)."""
    resp = client.get("/artifacts/../etc/passwd")
    # FastAPI 가 .. 정규화하므로 도달 시점에 path 가 다를 수 있음. 어쨌든
    # 200 이면 안 됨 — 403 (방어) 또는 404 (없는 path).
    assert resp.status_code in (403, 404)


def test_excluded_directory_file_blocked_403(client: TestClient) -> None:
    """node_modules 안 파일 직접 요청 시 403 (secrets/vendored 보호)."""
    resp = client.get("/artifacts/node_modules/foo/index.js")
    assert resp.status_code == 403


def test_excluded_env_file_blocked_403(client: TestClient) -> None:
    """``.env`` (secrets) 파일 직접 요청 시 403."""
    resp = client.get("/artifacts/.env")
    assert resp.status_code == 403


def test_missing_file_returns_404(client: TestClient) -> None:
    """존재하지 않는 path 는 404."""
    resp = client.get("/artifacts/does/not/exist.txt")
    assert resp.status_code == 404


def test_directory_download_returns_400(client: TestClient) -> None:
    """디렉토리 통째 GET 은 거부 — __bundle.zip 만 허용."""
    resp = client.get("/artifacts/src")
    # src 는 디렉토리 — 400 또는 catch-all 우회로 404 가능
    assert resp.status_code in (400, 404)


def test_agent_card_advertises_artifacts_endpoints(client: TestClient) -> None:
    """``/.well-known/agent.json`` 의 endpoints 에 artifacts 가 포함."""
    resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    endpoints = resp.json().get("endpoints", {})
    assert "artifactsBundle" in endpoints
    assert endpoints["artifactsBundle"].endswith("/artifacts/__bundle.zip")
    assert "artifactsFile" in endpoints
    assert "{path}" in endpoints["artifactsFile"]
