import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'movie_night.db'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "movienight")
    LETTERBOXD_TIMEOUT = float(os.environ.get("LETTERBOXD_TIMEOUT", "8"))
    LETTERBOXD_USER_AGENT = os.environ.get(
        "LETTERBOXD_USER_AGENT",
        "Mozilla/5.0 (compatible; MovieNightBot/1.0)",
    )
