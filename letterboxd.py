from __future__ import annotations

import re
import json
from typing import Dict
from urllib.parse import unquote, urlparse, quote_plus
from bs4 import BeautifulSoup
from wish_helpers import title_key

from curl_cffi import requests
from flask import current_app

class LetterboxdError(RuntimeError):
    """Raised when metadata cannot be retrieved from Letterboxd."""


class FilmChoicesRequired(LetterboxdError):
    def __init__(self, choices):
        super().__init__("Bitte den passenden Film auswaehlen.")
        self.choices = choices


def search_metadata(title):
    query = re.sub(r"\s*\(?\d{4}\)?$", "", title).strip()
    try:
        response = requests.get(
            "https://letterboxd.com/search/films/" + quote_plus(query) + "/",
            timeout=current_app.config["LETTERBOXD_TIMEOUT"],
            impersonate="chrome",
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        # Letterboxd serves result rows separately from the search-page shell.
        loader = soup.select_one('.load-more-search[data-url]')
        if loader and loader["data-url"].startswith("/s/search/films/"):
            response = requests.get(
                "https://letterboxd.com" + loader["data-url"],
                timeout=current_app.config["LETTERBOXD_TIMEOUT"], impersonate="chrome",
            )
            response.raise_for_status()
    except requests.RequestException as exc:
        raise LetterboxdError("Die automatische Filmsuche ist gerade nicht erreichbar.") from exc
    soup = BeautifulSoup(response.text, "html.parser")
    choices = []
    seen = set()
    for link in soup.select('a[href^="/film/"]'):
        path = link.get("href", "")
        if not re.fullmatch(r"/film/[^/]+/", path) or path in seen:
            continue
        article = link.find_parent("article")
        heading = article.find("h2") if article else None
        label = heading.get_text(" ", strip=True) if heading else link.get_text(" ", strip=True)
        if not label:
            continue
        seen.add(path)
        choices.append({"title": label, "canonical_url": "https://letterboxd.com" + path})
    if re.search(r"\s+\(?\d{4}\)?$", title):
        matches = [item for item in choices if title_key(item["title"]) == title_key(title)]
    else:
        matches = [item for item in choices if title_key(re.sub(r"\s+\(?\d{4}\)?$", "", item["title"])) == title_key(title)]
    if len(matches) == 1:
        return fetch_metadata(matches[0]["canonical_url"])
    if choices:
        raise FilmChoicesRequired(choices[:12])
    raise LetterboxdError("Kein Film gefunden. Bitte einen Letterboxd-Link angeben.")


def normalize_letterboxd_url(url: str) -> str:
    if not url:
        raise LetterboxdError("Letterboxd-Link wird benötigt")
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme not in {"http", "https"} or host not in {"letterboxd.com", "www.letterboxd.com"}:
        raise LetterboxdError("URL muss von letterboxd.com stammen")

    cleaned = parsed._replace(scheme="https", netloc="letterboxd.com", query="", fragment="").geturl().rstrip("/")
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
    try:
        response = requests.get(normalized, timeout=timeout, impersonate="chrome")
    except requests.RequestException as exc:
        raise LetterboxdError("Letterboxd ist nicht erreichbar") from exc

    if response.status_code == 403:
        raise LetterboxdError(
            "Letterboxd blockiert den automatischen Abruf momentan."
        )

    if response.status_code >= 400:
        raise LetterboxdError(
            f"Letterboxd-Seite konnte nicht geladen werden (Status {response.status_code})"
        )

    soup = BeautifulSoup(response.text, "html.parser")
    data = {tag["property"][3:]: tag.get("content") for tag in soup.select('meta[property^="og:"]')}
    poster = None
    for script in soup.select('script[type="application/ld+json"]'):
        raw = (script.string or "").strip().removeprefix("/* <![CDATA[ */").removesuffix("/* ]]> */").strip()
        try:
            structured = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for entity in structured if isinstance(structured, list) else [structured]:
            if isinstance(entity, dict) and entity.get("@type") == "Movie":
                candidate = entity.get("image")
                if isinstance(candidate, str) and "film-poster" in candidate:
                    poster = candidate
    if not poster:
        image = soup.select_one('img[src*="/film-poster/"]')
        poster = image.get("src") if image else None
    if not data.get("title") and soup.title:
        data["title"] = soup.title.get_text().replace(" – Letterboxd", "").strip()

    return {
        "title": data.get("title"),
        "synopsis": data.get("description"),
        "poster_url": poster,
        "canonical_url": normalized,
    }
