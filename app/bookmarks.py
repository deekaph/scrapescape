import json
from urllib.parse import urlparse


def parse_chrome_bookmarks(filepath: str) -> list[dict]:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        raise ValueError(f"bookmarks.txt is not valid JSON — re-export your Chrome bookmarks")
    except FileNotFoundError:
        raise ValueError("bookmarks.txt not found")

    urls = []
    _walk(data.get("roots", {}), urls)
    return urls


def _walk(node, results):
    if isinstance(node, dict):
        if node.get("type") == "url":
            results.append({"url": node["url"], "name": node.get("name", "")})
        for child in node.get("children", []):
            _walk(child, results)
        for key in ("bookmark_bar", "other", "synced"):
            if key in node:
                _walk(node[key], results)


def get_domain_summary(bookmarks: list[dict]) -> dict:
    domains = {}
    for bm in bookmarks:
        try:
            host = urlparse(bm["url"]).hostname or "unknown"
            # Simplify to base domain
            parts = host.split(".")
            if len(parts) >= 2:
                domain = ".".join(parts[-2:])
            else:
                domain = host
        except Exception:
            domain = "unknown"
        domains.setdefault(domain, []).append(bm)
    return domains


def filter_bookmarks(bookmarks: list[dict], domains: list[str]) -> list[dict]:
    if not domains:
        return bookmarks
    filtered = []
    for bm in bookmarks:
        try:
            host = urlparse(bm["url"]).hostname or ""
            if any(d in host for d in domains):
                filtered.append(bm)
        except Exception:
            continue
    return filtered
