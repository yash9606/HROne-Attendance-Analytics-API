"""Integration tests against a disposable local database.

The suite forces MONGO_URI to localhost unless TEST_MONGO_URI is set, and it
uses the database name hrone_attendance_test. That database is dropped at the
start and end of the session. It never reads or writes attendance_db.
"""

import os

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

os.environ["MONGO_URI"] = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ["MONGO_DB"] = "hrone_attendance_test"

from app.main import app, client, db  # noqa: E402

TEST_DB = "hrone_attendance_test"


@pytest.fixture(scope="session")
def mongo_ready():
    probe = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=2000)
    try:
        probe.admin.command("ping")
    except PyMongoError as exc:
        pytest.skip(
            f"MongoDB is not available at {os.environ['MONGO_URI']}: {exc}. "
            "Start MongoDB 7 and rerun pytest."
        )
    finally:
        probe.close()


@pytest.fixture(scope="session")
def api(mongo_ready):
    if db.name != TEST_DB:
        raise RuntimeError(f"Refusing to use unexpected database {db.name}")
    client.drop_database(TEST_DB)
    with TestClient(app) as test_client:
        yield test_client
        if db.name == TEST_DB:
            client.drop_database(TEST_DB)


@pytest.fixture(autouse=True)
def clean_collections(api):
    if db.name != TEST_DB:
        raise RuntimeError(f"Refusing to clean unexpected database {db.name}")
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
