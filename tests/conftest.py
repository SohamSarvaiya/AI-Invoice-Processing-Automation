import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="invoice-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["EXTRACTION_MODE"] = "rules"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "auto"
os.environ["API_KEY"] = ""
os.environ["RETRY_BASE_DELAY"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_llm_override():
    from app.services import llm

    llm.set_override(None)
    yield
    llm.set_override(None)


@pytest.fixture(autouse=True)
def fresh_db():
    from app import models  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pdf(tmp_path):
    from scripts.generate_samples import make_pdf

    def _make(name="inv.pdf", **over):
        return make_pdf(tmp_path / name, **over)

    return _make


def upload(client, path, source="test"):
    with open(path, "rb") as f:
        return client.post("/invoices/upload", files={"file": (Path(path).name, f)}, data={"source": source})
