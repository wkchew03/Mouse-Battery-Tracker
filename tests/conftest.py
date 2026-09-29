import pytest


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path, monkeypatch):
    """Keep the user's real files (merges.json, adopted.json) out of every test."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
