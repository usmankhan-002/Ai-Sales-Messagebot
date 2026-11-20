import pytest

from app import config, db


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """Tests must never write to the real Neon database from .env.

    Storage tests opt in to a throwaway SQLite file via the `sqlite_db` fixture.
    """
    monkeypatch.setattr(config.settings, "database_url", "")
    db.reset_engine()
    yield
    db.reset_engine()


@pytest.fixture
def sqlite_db(monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{tmp_path / 'ai_bot.db'}")
    db.reset_engine()
    assert db.init_db()
    yield
    db.reset_engine()
