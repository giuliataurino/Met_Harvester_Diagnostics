"""Minimal Met Collection API client: retries, polite pacing, one JSON file per object."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://collectionapi.metmuseum.org/public/collection/v1"


class MetClient:
    def __init__(self, cache_dir: Path, rate: float = 20):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.interval = 1 / rate
        self._last = 0.0
        self.http = requests.Session()
        self.http.mount("https://", HTTPAdapter(max_retries=Retry(
            total=5, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))))
        self.http.headers["User-Agent"] = "met-diagnostics"

    def _get(self, path: str, params: dict | None = None) -> dict:
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.http.get(f"{API}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def object_ids(self, department_id: int) -> list[int]:
        """Never cached, so reruns see new accessions. /objects lists a department completely;
        /v1.1/search would stop at the Met's 10,000-result paging limit."""
        return self._get("objects", {"departmentIds": department_id}).get("objectIDs") or []

    def cache_path(self, object_id: int) -> Path:
        return self.cache_dir / f"{object_id}.json"

    def get_object(self, object_id: int) -> dict:
        """Raw record, from cache if present; a 404 raises requests.HTTPError."""
        p = self.cache_path(object_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        data = self._get(f"objects/{object_id}")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def fetch_live(self, object_id: int) -> requests.Response:
        """Uncached call for the Live API tab."""
        return self.http.get(f"{API}/objects/{object_id}", timeout=30)
