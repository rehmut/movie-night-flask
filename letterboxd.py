from __future__ import annotations

import re
from typing import Dict
from urllib.parse import urlparse

import requests
from flask import current_app

OG_TAG_PATTERN = re.compile(
    r'<meta\s+property="og:(?P<name>[^"]+)"\s+content="(?P<content>[^"]*)"',
    re.IGNORECASE,
)


class LetterboxdError(RuntimeError):
    """Raised when metadata cannot be retrieved from Letterboxd."""


def normalize_letterboxd_url(url: str) -> str:
    if not url:
        raise LetterboxdError("Letterboxd URL is required")
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    if not parsed.netloc.endswith("letterboxd.com"):
        raise LetterboxdError("URL must be from letterboxd.com")

    cleaned = parsed._replace(query="", fragment="").geturl()
    return cleaned.rstrip("/")


def fetch_metadata(letterboxd_url: str) -> Dict[str, str]:
    normalized = normalize_letterboxd_url(letterboxd_url)
    timeout = current_app.config["LETTERBOXD_TIMEOUT"]
    headers = {"User-Agent": current_app.config["LETTERBOXD_USER_AGENT"]}

    try:
        response = requests.get(normalized, timeout=timeout, headers=headers)
    except requests.RequestException as exc:
        raise LetterboxdError("Unable to reach Letterboxd") from exc

    if response.status_code >= 400:
        raise LetterboxdError(
            f"Failed to fetch Letterboxd page (status {response.status_code})"
        )

    html = response.text
    matches = OG_TAG_PATTERN.findall(html)

    data: Dict[str, str] = {}
    for name, content in matches:
        data[name.lower()] = content

    return {
        "title": data.get("title"),
        "synopsis": data.get("description"),
        "poster_url": data.get("image"),
        "canonical_url": normalized,
    }
