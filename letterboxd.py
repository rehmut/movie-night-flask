from __future__ import annotations

import re
from typing import Dict
from urllib.parse import unquote, urlparse

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
        raise LetterboxdError("Letterboxd-Link wird benötigt")
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in {"letterboxd.com", "www.letterboxd.com"} and not host.endswith(
        ".letterboxd.com"
    ):
        raise LetterboxdError("URL muss von letterboxd.com stammen")

    cleaned = parsed._replace(query="", fragment="").geturl().rstrip("/")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0] == "film":
        return cleaned + "/"
    return cleaned


def title_from_letterboxd_url(url: str) -> str | None:
    normalized = normalize_letterboxd_url(url)
    parsed = urlparse(normalized)
    parts = [part for part in parsed.path.split("/") if part]

    try:
        film_index = parts.index("film")
    except ValueError:
        return None

    if film_index + 1 >= len(parts):
        return None

    slug = unquote(parts[film_index + 1]).strip()
    if not slug:
        return None

    words = [word for word in slug.replace("-", " ").split() if word]
    return " ".join(word.capitalize() for word in words) or None


def fetch_metadata(letterboxd_url: str) -> Dict[str, str]:
    normalized = normalize_letterboxd_url(letterboxd_url)
    timeout = current_app.config["LETTERBOXD_TIMEOUT"]
    headers = {
        "User-Agent": current_app.config["LETTERBOXD_USER_AGENT"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        response = requests.get(normalized, timeout=timeout, headers=headers)
    except requests.RequestException as exc:
        raise LetterboxdError("Letterboxd ist nicht erreichbar") from exc

    if response.status_code == 403:
        raise LetterboxdError(
            "Letterboxd blockiert den automatischen Abruf. Der Link wurde trotzdem gespeichert."
        )

    if response.status_code >= 400:
        raise LetterboxdError(
            f"Letterboxd-Seite konnte nicht geladen werden (Status {response.status_code})"
        )

    html = response.text
    matches = OG_TAG_PATTERN.findall(html)

    data: Dict[str, str] = {}
    for name, content in matches:
        data[name.lower()] = content

    if not data.get("title"):
        title_match = re.search(r"<title>(?P<title>.*?)</title>", html, re.IGNORECASE)
        if title_match:
            # Clean up the title from Letterboxd's format
            title = title_match.group("title").replace(" – Letterboxd", "").strip()
            data["title"] = title

    return {
        "title": data.get("title"),
        "synopsis": data.get("description"),
        "poster_url": data.get("image"),
        "canonical_url": normalized,
    }
