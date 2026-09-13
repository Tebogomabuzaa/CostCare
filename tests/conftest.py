import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Must be set before the app (and its settings) are imported.
_TMP = tempfile.mkdtemp(prefix="costcare-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GOOGLE_MAPS_API_KEY"] = ""
os.environ["ADMIN_API_KEY"] = ""
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "adminpass123"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(client):
    from app.database import SessionLocal

    with SessionLocal() as session:
        yield session


@pytest.fixture
def admin_client(client):
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/login", data={"email": "admin@test.local", "password": "adminpass123"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin"
        yield c


@pytest.fixture
def user_client(client):
    import uuid

    from app.main import app

    with TestClient(app) as c:
        email = f"traveller-{uuid.uuid4().hex[:8]}@test.local"
        r = c.post("/signup", data={"name": "Traveller", "email": email, "password": "password123"},
                   follow_redirects=False)
        assert r.status_code == 303
        c.email = email
        yield c
