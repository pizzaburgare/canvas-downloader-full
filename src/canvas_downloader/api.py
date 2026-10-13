"""Minimal Canvas REST client authenticated with browser session cookies."""

from __future__ import annotations

import json
from typing import Any

import httpx


class CanvasError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


class CanvasAPI:
    def __init__(self, base_url: str, cookies: list[dict], user_agent: str, workers: int = 4):
        self.base = base_url.rstrip("/")
        jar = httpx.Cookies()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        self.client = httpx.Client(
            cookies=jar,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(120, connect=30),
            limits=httpx.Limits(max_connections=workers + 4),
        )

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base}{path}"

    @staticmethod
    def _parse(r: httpx.Response) -> Any:
        text = r.text
        # Canvas prefixes JSON with this anti-hijacking guard for session-authenticated requests.
        if text.startswith("while(1);"):
            text = text[len("while(1);"):]
        return json.loads(text)

    def _request(self, url: str, params: dict | None) -> httpx.Response:
        r = self.client.get(url, params=params, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise CanvasError(r.status_code, str(r.url))
        return r

    def get(self, path: str, **params) -> Any:
        return self._parse(self._request(self._url(path), params or None))

    def get_all(self, path: str, **params) -> list:
        """GET with Link-header pagination."""
        params.setdefault("per_page", 100)
        out: list = []
        url: str | None = self._url(path)
        first = True
        while url:
            r = self._request(url, params if first else None)
            first = False
            data = self._parse(r)
            if isinstance(data, list):
                out.extend(data)
            else:
                out.append(data)
            url = r.links.get("next", {}).get("url")
        return out


def fetch_courses(api: CanvasAPI) -> list[dict]:
    """All courses the user can see, including concluded ones."""
    seen: dict[int, dict] = {}
    for extra in ({}, {"enrollment_state": "completed"}):
        try:
            courses = api.get_all("/api/v1/courses", **{"include[]": ["term"]}, **extra)
        except CanvasError:
            continue
        for c in courses:
            if c.get("name") and not c.get("access_restricted_by_date"):
                seen.setdefault(c["id"], c)

    def sort_key(c):
        term = c.get("term") or {}
        return (term.get("start_at") or c.get("start_at") or c.get("created_at") or "", c.get("name", ""))

    return sorted(seen.values(), key=sort_key, reverse=True)
