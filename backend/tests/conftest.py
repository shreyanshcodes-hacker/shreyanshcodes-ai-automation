import sys
from pathlib import Path
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "studio.db"
    token_file = storage_dir / "tokens" / "youtube_oauth.json"

    monkeypatch.setenv("OUTPUT_DIR", str(storage_dir))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", str(token_file))

    from app import config, youtube
    from app.security import rate_limiter

    rate_limiter.reset()
    monkeypatch.setattr(config.settings, "output_dir", str(storage_dir))
    monkeypatch.setattr(config.settings, "database_path", str(db_path))
    monkeypatch.setattr(config.settings, "youtube_token_file", str(token_file))
    youtube._RUNTIME_KILL_SWITCH = None
    yield storage_dir
