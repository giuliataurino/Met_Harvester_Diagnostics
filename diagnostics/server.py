"""
Met harvest diagnostics: a local web UI for inspecting what the Met Collection API returns
for Greek and Roman Art objects and what the staging mapping makes of it.

    pip install -r requirements.txt
    python server.py [--cache data/met/objects] [--port 8765]

Then open http://localhost:8765. Self-contained: the client, record model, staging mapping and
AAT tables live in ./metdiag (copied from paa_graph_kb; see README for the drift caveat).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from rdflib import Graph

from metdiag import aat as aat_mappings
from metdiag.client import API, MetClient
from metdiag.models import MetObject
from metdiag.staging import EXS, STG, met_object_to_staging

HERE = Path(__file__).parent
GREEK_AND_ROMAN_ART = 13
AAT_FIELDS = (("culture", "cultures"), ("objectName", "object_types"),
              ("classification", "classifications"), ("medium", "materials"))


def aat_term(field: str, value: str) -> str:
    return value.split(",")[0].strip() if field == "culture" else value


# --------------------------------------------------------------------------- cache index
class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[int, dict] = {}
        self.files: dict[int, Path] = {}
        self.lock = threading.Lock()

    def refresh(self) -> int:
        found = {}
        files = {}
        for f in self.path.glob("*.json") if self.path.exists() else []:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and "objectID" in data:
                found[data["objectID"]] = data
                files[data["objectID"]] = f
        with self.lock:
            self.records, self.files = found, files
        return len(found)

    def add(self, rec: dict) -> None:
        with self.lock:
            self.records[rec["objectID"]] = rec

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.records.values())


# --------------------------------------------------------------------------- harvest thread
class Harvest:
    def __init__(self, cache: Cache):
        self.cache = cache
        self.state = {"running": False, "paused": False, "done": 0, "total": 0, "fetched": 0,
                      "cached": 0, "failed": 0, "skipped": [], "started": None, "department": None, "error": None}
        self.stop_flag = False
        self.thread: threading.Thread | None = None

    def start(self, department: int, limit: int | None) -> bool:
        if self.state["running"]:
            return False
        self.stop_flag = False
        self.state.update(running=True, paused=False, done=0, total=0, fetched=0, cached=0, failed=0,
                          skipped=[], started=time.time(), department=department, error=None)
        self.thread = threading.Thread(target=self._run, args=(department, limit), daemon=True)
        self.thread.start()
        return True

    def _run(self, department: int, limit: int | None) -> None:
        client = MetClient(self.cache.path)
        try:
            ids = client.object_ids(department)
            if limit:
                ids = ids[:limit]
            self.state["total"] = len(ids)
            for oid in ids:
                while self.state["paused"] and not self.stop_flag:
                    time.sleep(0.3)
                if self.stop_flag:
                    break
                in_cache = client.cache_path(oid).exists()
                try:
                    raw = client.get_object(oid)
                    MetObject.model_validate(raw)  # surface validation problems here, as the harvester would
                    self.cache.add(raw)
                    self.state["cached" if in_cache else "fetched"] += 1
                except Exception as e:  # noqa: BLE001 - diagnostics: record and continue
                    self.state["failed"] += 1
                    if len(self.state["skipped"]) < 200:
                        self.state["skipped"].append({"objectID": oid, "error": str(e)[:200]})
                self.state["done"] += 1
        except Exception as e:  # noqa: BLE001
            self.state["error"] = str(e)
        finally:
            self.state["running"] = False


# --------------------------------------------------------------------------- diagnostics
def present(v) -> bool:
    return not (v is None or v == "" or v == [] or v == {})


def object_detail(raw: dict) -> dict:
    obj = MetObject.model_validate(raw)
    g = Graph()
    g.bind("stg", STG)
    g.bind("exs", EXS)
    met_object_to_staging(g, obj)
    s = EXS[f"o/met:{obj.objectID}"]
    staged_props = sorted({str(p).split("#")[-1] for p in g.predicates(s)})
    aat = []
    for field, category in AAT_FIELDS:
        value = getattr(obj, field)
        if value:
            aat.append({"field": field, "term": aat_term(field, value),
                        "uri": aat_mappings.get_aat_uri(aat_term(field, value), category=category)})
    return {
        "raw": raw,
        "fields": {k: present(v) for k, v in raw.items()},
        "turtle": g.serialize(format="turtle"),
        "triples": len(g),
        "staged_properties": staged_props,
        "aat": aat,
        "nodes": {"images": len(list(g.objects(s, STG.hasImage))),
                  "measurements": len(list(g.objects(s, STG.hasMeasurement))),
                  "persons": len(list(g.objects(s, STG.hasPerson))),
                  "subjects": len(list(g.objects(s, STG.subject)))},
    }


def live_compare(object_id: int, cached: dict | None) -> dict:
    """Fetch the object straight from the Met (no MetClient, no cache) and compare it with
    what the harvester holds: the cached JSON, the MetObject view of it, and the staged triples."""
    r = requests.get(f"{API}/objects/{object_id}", timeout=30, headers={"User-Agent": "met-diagnostics"})
    out = {"status": r.status_code, "api": None, "cached": cached}
    if r.status_code != 200:
        out["error"] = f"API returned HTTP {r.status_code}"
        return out
    api = r.json()
    out["api"] = api

    # 1. cache vs live API: fields whose value differs (stale cache, or the Met edited the record)
    if cached:
        keys = sorted(set(api) | set(cached))
        out["cache_diff"] = [{"field": k, "api": api.get(k), "cached": cached.get(k)}
                             for k in keys if api.get(k) != cached.get(k)]
        out["cache_metadataDate"] = cached.get("metadataDate")
    out["api_metadataDate"] = api.get("metadataDate")

    # 2. live API vs model: keys the model has no field for, and values the model changed
    obj = MetObject.model_validate(api)
    dumped = json.loads(obj.model_dump_json())
    out["model_unknown_keys"] = sorted(k for k in api if k not in MetObject.model_fields)
    out["model_changed"] = [{"field": k, "api": api.get(k), "model": dumped.get(k)}
                            for k in MetObject.model_fields
                            if api.get(k) not in (None, "", [], {}) and dumped.get(k) != api.get(k)
                            and not _equivalent(api.get(k), dumped.get(k))]

    # 3. model vs staging: filled model fields that produced no triple at all
    g = Graph(); met_object_to_staging(g, obj)
    ttl = g.serialize(format="turtle")
    out["turtle"] = ttl
    out["triples"] = len(g)
    out["unstaged"] = [k for k, v in dumped.items() if present(v) and not _value_in_turtle(v, ttl)]
    return out


def _equivalent_loose(a, b) -> bool:
    """For comparing a raw API value with a model-dumped one: blanks and ints normalise."""
    blank = (None, "", [], {})
    return (a in blank and b in blank) or _equivalent(a, b)


def _equivalent(a, b) -> bool:
    """Same data despite model normalisation: '1956' == 1956, blanks pruned from lists,
    and sub-records that only gained absent optional keys as null."""
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, int):
        return a.strip() == str(b)
    if isinstance(a, list) and isinstance(b, list):
        a2 = [x for x in a if x not in ("", None)]
        b2 = [x for x in b if x not in ("", None)]
        return len(a2) == len(b2) and all(_equivalent(x, y) for x, y in zip(a2, b2))
    if isinstance(a, dict) and isinstance(b, dict):
        return all(_equivalent_loose(a.get(k), b.get(k)) for k in set(a) | set(b))
    return False


def _value_in_turtle(v, ttl: str) -> bool:
    if isinstance(v, bool):
        return True  # booleans are always staged
    if isinstance(v, (int, float)):
        return str(v) in ttl
    if isinstance(v, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", v):  # xsd:dateTime is reserialised; match the date part
            return v[:10] in ttl
        return v.replace('"', '\\"')[:40] in ttl
    if isinstance(v, list):
        return any(_value_in_turtle(x, ttl) for x in v) if v else True
    if isinstance(v, dict):
        return any(_value_in_turtle(x, ttl) for x in v.values()) if v else True
    return True


def coverage(records: list[dict]) -> dict:
    n = len(records)
    fields: Counter = Counter()
    terms: dict[str, Counter] = {f: Counter() for f, _ in AAT_FIELDS}
    for r in records:
        for k, v in r.items():
            if present(v):
                fields[k] += 1
        for f, _ in AAT_FIELDS:
            if present(r.get(f)):
                terms[f][aat_term(f, r[f])] += 1
    aat = {}
    for f, category in AAT_FIELDS:
        rows = [{"term": t, "count": c, "uri": aat_mappings.get_aat_uri(t, category=category)}
                for t, c in terms[f].most_common()]
        mapped_objects = sum(r["count"] for r in rows if r["uri"])
        aat[f] = {"distinct": len(rows), "objects": sum(r["count"] for r in rows), "mapped_objects": mapped_objects,
                  "unmapped": [r for r in rows if not r["uri"]][:40]}
    return {"objects": n, "fields": {k: fields.get(k, 0) for k in sorted(fields)}, "aat": aat}


def norm(acc: str) -> str:
    return re.sub(r"\s+", "", acc).lower()


def perseus_matches(records: list[dict], perseus_ttl: Path) -> dict:
    g = Graph().parse(perseus_ttl, format="turtle")
    perseus = {}
    for s in g.subjects(STG.institution):
        if "Metropolitan" in str(g.value(s, STG.institution)):
            acc = g.value(s, STG.accessionNumber)
            if acc:
                perseus[norm(str(acc))] = str(acc)
    met = {norm(r["accessionNumber"]): r["objectID"] for r in records if present(r.get("accessionNumber"))}
    matched = {k: v for k, v in perseus.items() if k in met}
    unmatched = sorted(v for k, v in perseus.items() if k not in met)
    return {"perseus_met_objects": len(perseus), "matched": len(matched), "unmatched": unmatched,
            "sample": [{"accession": perseus[k], "objectID": met[k]} for k in list(matched)[:20]]}


# --------------------------------------------------------------------------- HTTP
def make_handler(cache: Cache, harvest: Harvest):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE), **kw)

        def log_message(self, *a):  # quiet
            pass

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                self.path = "/index.html"
                return super().do_GET()
            if u.path == "/api/status":
                return self.send_json({**harvest.state, "in_cache": len(cache.records), "cache_dir": str(cache.path)})
            if u.path == "/api/objects":
                recs = cache.snapshot()
                text = q.get("q", "").lower()
                if text:
                    keys = ("title", "objectName", "culture", "medium", "accessionNumber", "period", "classification")
                    recs = [r for r in recs if any(text in str(r.get(k, "")).lower() for k in keys) or text == str(r["objectID"])]
                if q.get("missing"):
                    recs = [r for r in recs if not present(r.get(q["missing"]))]
                if q.get("images") == "1":
                    recs = [r for r in recs if present(r.get("primaryImageSmall"))]
                recs.sort(key=lambda r: r["objectID"])
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 60))
                page = [{k: r.get(k) for k in ("objectID", "title", "objectName", "culture", "objectDate",
                                                "accessionNumber", "primaryImageSmall")} for r in recs[off:off + lim]]
                return self.send_json({"total": len(recs), "items": page})
            m = re.fullmatch(r"/api/object/(\d+)", u.path)
            if m:
                rec = cache.records.get(int(m.group(1)))
                if not rec:
                    return self.send_json({"error": "not in cache"}, 404)
                try:
                    return self.send_json(object_detail(rec))
                except Exception as e:  # noqa: BLE001
                    return self.send_json({"error": f"staging failed: {e}", "raw": rec}, 500)
            m = re.fullmatch(r"/api/live/(\d+)", u.path)
            if m:
                oid = int(m.group(1))
                try:
                    return self.send_json(live_compare(oid, cache.records.get(oid)))
                except requests.RequestException as e:
                    return self.send_json({"error": f"could not reach the Met API: {e}"}, 502)
                except Exception as e:  # noqa: BLE001
                    return self.send_json({"error": str(e)}, 500)
            if u.path == "/api/coverage":
                return self.send_json(coverage(cache.snapshot()))
            if u.path == "/api/perseus":
                p = Path(q.get("path", ""))
                if not p.exists():
                    return self.send_json({"error": f"file not found: {p}"}, 404)
                return self.send_json(perseus_matches(cache.snapshot(), p))
            return super().do_GET()

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/harvest":
                ok = harvest.start(int(q.get("department", GREEK_AND_ROMAN_ART)),
                                   int(q["limit"]) if q.get("limit") else None)
                return self.send_json({"started": ok})
            if u.path == "/api/pause":
                harvest.state["paused"] = not harvest.state["paused"]
                return self.send_json({"paused": harvest.state["paused"]})
            if u.path == "/api/stop":
                harvest.stop_flag = True
                return self.send_json({"stopping": True})
            if u.path == "/api/refresh":
                return self.send_json({"in_cache": cache.refresh()})
            return self.send_json({"error": "unknown endpoint"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Diagnostics UI for Met Greek and Roman Art harvesting.")
    ap.add_argument("--cache", type=Path, default=Path(os.getenv("MET_OBJ_DIR", "data/met/objects")))
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    cache = Cache(args.cache)
    print(f"indexing {args.cache} ... {cache.refresh()} records")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cache, Harvest(cache)))
    print(f"open http://localhost:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
