"""A throwaway PostgreSQL 16 for the database tests: same setup script as the real server, an admin that is not a superuser,
and public owned by azure_pg_admin as on Azure. Tests never touch the real server. No Docker: those tests are skipped."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# tests never publish to the real Drive, and never add to the local records
_TEST_DATA = Path(tempfile.mkdtemp(prefix="dealadvisor-tests-"))
os.environ["LANGSMITH_TRACING"] = "false"     # tests never send traces
os.environ["EXTRACTION_MODE"] = "rules"       # tests that need the LLM path set it themselves
os.environ["STORAGE_BACKEND"] = "local"
os.environ["STORAGE_LOCAL_ROOT"] = str(_TEST_DATA / "published")
OWNER_PASSWORD, APP_PASSWORD = "owner-test", "app-test"
_PIPELINE_RESULTS: dict = {}


def _docker(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kwargs)


def _superuser_sql(name: str, db: str, sql: str) -> None:
    _docker("exec", name, "psql", "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql)


@pytest.fixture(autouse=True, scope="session")
def _records_in_a_temp_folder():
    from app import record
    record.DATA_DIR = _TEST_DATA / "opportunities"
    yield
    shutil.rmtree(_TEST_DATA, ignore_errors=True)


@pytest.fixture(autouse=True)
def _empty_document_cache():
    from app import doc_cache
    doc_cache.clear()


@pytest.fixture(scope="session")
def pg():
    """{"owner": url, "app": url} of a fresh server with db_setup.sql applied, no migrations."""
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("Docker is not available")
    import psycopg
    name = f"dealadvisor-test-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_HOST_AUTH_METHOD=md5",
            "-p", "127.0.0.1::5432", "postgres:16", "-c", "password_encryption=md5")
    try:
        port = _docker("port", name, "5432").stdout.strip().rsplit(":", 1)[1]
        for _ in range(60):     # TCP only answers once the image's init server has been replaced by the real one
            try:
                psycopg.connect(f"postgresql://postgres:test@127.0.0.1:{port}/postgres", connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        _superuser_sql(name, "postgres", "CREATE ROLE azure_pg_admin NOLOGIN")
        _superuser_sql(name, "postgres", "CREATE ROLE admin_user LOGIN CREATEDB CREATEROLE IN ROLE azure_pg_admin")
        _superuser_sql(name, "template1", "ALTER SCHEMA public OWNER TO azure_pg_admin")
        _docker("exec", "-i", "-e", f"DEALADVISOR_OWNER_PASSWORD={OWNER_PASSWORD}", "-e", f"DEALADVISOR_APP_PASSWORD={APP_PASSWORD}", name,
                "psql", "-q", "-U", "admin_user", "-d", "postgres", input=(ROOT / "infra" / "db_setup.sql").read_text())
        host = f"127.0.0.1:{port}/dealadvisor"
        yield {"owner": f"postgresql://dealadvisor_owner:{OWNER_PASSWORD}@{host}", "app": f"postgresql://dealadvisor_app:{APP_PASSWORD}@{host}"}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def cached_sample_pipeline(monkeypatch):
    """The sample's pipeline result, computed once per test session: it is slow, and each run adds a local record version."""
    from app import opportunities

    real = opportunities._pipeline

    def once(inputs, opp_id, client, project, confirmation, bind=None):
        if not all(d.bundled for d in inputs):
            return real(inputs, opp_id, client, project, confirmation, bind)
        if "sample" not in _PIPELINE_RESULTS:
            _PIPELINE_RESULTS["sample"] = real(inputs, opp_id, client, project, confirmation, bind)
        return _PIPELINE_RESULTS["sample"]

    monkeypatch.setattr(opportunities, "_pipeline", once)
