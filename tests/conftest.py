import sys
from pathlib import Path

import pytest

# The publish scripts import their siblings (showhome, seo, autolog) by name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "publish"))


@pytest.fixture(autouse=True)
def _no_machine_brand(monkeypatch):
    """Machine-local brand assets (~/.sofit/assets) must not leak into tests."""
    monkeypatch.setenv("SOFIT_BRAND", "off")
