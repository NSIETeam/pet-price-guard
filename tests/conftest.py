import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["PPG_API_KEY"] = "test-secret"
os.environ["PPG_COLLECTOR"] = "demo"

import app.core as core
import app.main as main


@pytest.fixture()
def database(tmp_path, monkeypatch):
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    test_session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(core, "engine", test_engine)
    monkeypatch.setattr(core, "SessionLocal", test_session)
    monkeypatch.setattr(main, "SessionLocal", test_session)
    monkeypatch.setattr(main, "verify_schema", lambda: None)
    core.init_db()
    return test_session


@pytest.fixture()
def client(database):
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"X-API-Key": "test-secret"}
