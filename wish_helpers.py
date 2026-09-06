import re
import unicodedata
from urllib.parse import urlparse


def name_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def title_key(value):
    return " ".join(re.findall(r"\w+", name_key(value)))


def film_keys(title, url=""):
    keys = {"title:" + title_key(title)}
    if url:
        keys.add("film:" + urlparse(url).path.rstrip("/").casefold())
    return keys


def same_film(title, url, other_title, other_url):
    if url and other_url:
        return (
            urlparse(url).path.rstrip("/").casefold()
            == urlparse(other_url).path.rstrip("/").casefold()
        )

    def split_title(value):
        match = re.search(r"\s+\(?(\d{4})\)?$", value.strip())
        if match:
            return title_key(value[: match.start()]), match.group(1)
        return title_key(value), None

    name, year = split_title(title)
    other_name, other_year = split_title(other_title)
    return name == other_name and (not year or not other_year or year == other_year)
