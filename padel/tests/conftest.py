import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ROOT))

from playtomic import club_from_tenant


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def club():
    return club_from_tenant(load_fixture("sample_tenant.json"))
