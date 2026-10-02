from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = API_ROOT.parent.parent


@pytest.fixture(scope="session")
def api_root() -> Path:
    return API_ROOT


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
