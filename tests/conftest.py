import pytest


@pytest.fixture(autouse=True)
def _no_machine_brand(monkeypatch):
    """Machine-local brand assets (~/.sofit/assets) must not leak into tests."""
    monkeypatch.setenv("SOFIT_BRAND", "off")


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch, tmp_path):
    """Tests must never prune or mutate the user's real media/transcript cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
