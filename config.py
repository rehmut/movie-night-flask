import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _build_database_url(raw_url: str | None) -> str:
    if not raw_url:
        return f"sqlite:///{BASE_DIR / 'movie_night.db'}"

    url = raw_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = _build_database_url(
        os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "movienight")
    LETTERBOXD_TIMEOUT = float(os.environ.get("LETTERBOXD_TIMEOUT", "8"))
    LETTERBOXD_USER_AGENT = os.environ.get(
        "LETTERBOXD_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
