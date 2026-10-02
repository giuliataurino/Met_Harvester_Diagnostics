"""Offline test: fake cache + mocked MetClient, exercises every endpoint."""
import json, threading, time, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server

SAMPLE = {
    "objectID": 254904, "accessionNumber": "56.171.49", "accessionYear": "1956", "isPublicDomain": True,
    "department": "Greek and Roman Art", "objectName": "Kylix", "title": "Terracotta kylix", "culture": "Greek, Attic",
    "period": "Archaic", "objectDate": "ca. 520 BCE", "objectBeginDate": -530, "objectEndDate": -510, "medium": "Terracotta",
    "classification": "Vases", "creditLine": "Fletcher Fund, 1956", "country": "", "primaryImageSmall": "https://example.invalid/x.jpg",
    "additionalImages": [], "constituents": [], "tags": [{"term": "Dionysus", "AAT_URL": "http://vocab.getty.edu/page/aat/300343813"}],
    "measurements": [{"elementName": "Overall", "elementMeasurements": {"Height": 10.8}}], "objectURL": "https://www.metmuseum.org/art/collection/search/254904",
}


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
        return json.loads(r.read()) if "json" in r.headers.get("Content-Type", "") else r.read().decode()


def post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_everything(tmp_path: Path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    (cache_dir / "deadbeef.json").write_text(json.dumps(SAMPLE))
    (cache_dir / "ids.json").write_text(json.dumps({"total": 1, "objectIDs": [254904]}))
    perseus = tmp_path / "perseus.ttl"
    perseus.write_text('@prefix stg: <https://aa.perseus.org/staging#> .\n'
                       '<urn:a> stg:institution "Metropolitan Museum of Art" ; stg:accessionNumber "56.171.49" .\n'
                       '<urn:b> stg:institution "Metropolitan Museum of Art" ; stg:accessionNumber "99.1.1" .\n')
    cache = server.Cache(cache_dir); assert cache.refresh() == 1
    harvest = server.Harvest(cache)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cache, harvest))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    assert "<title>Met harvest diagnostics" in get(port, "/")
    assert get(port, "/api/status")["in_cache"] == 1
    objs = get(port, "/api/objects?q=kylix"); assert objs["total"] == 1
    assert get(port, "/api/objects?missing=country")["total"] == 1
    assert get(port, "/api/objects?missing=title")["total"] == 0
    d = get(port, "/api/object/254904")
    assert d["triples"] > 20 and "stg:objectnumber" in d["turtle"] and d["nodes"]["measurements"] == 1
    aat = {a["field"]: bool(a["uri"]) for a in d["aat"]}
    assert aat["culture"] and aat["medium"]  # "Greek" and "Terracotta" are in aat_mappings
    cov = get(port, "/api/coverage"); assert cov["objects"] == 1 and cov["fields"]["title"] == 1
    p = get(port, f"/api/perseus?path={perseus}"); assert p["matched"] == 1 and p["unmatched"] == ["99.1.1"]

    # mocked harvest: three ids - one already cached, one new, one 404
    import requests
    (cache_dir / "254904.json").write_text(json.dumps(SAMPLE))
    def fake_get(self, path, params=None):
        oid = int(path.rsplit("/", 1)[1])
        if oid == 7:
            resp = requests.Response(); resp.status_code = 404
            raise requests.HTTPError(response=resp)
        return dict(SAMPLE, objectID=oid)
    with patch.object(server.MetClient, "object_ids", lambda self, d: [254904, 2, 7]), \
         patch.object(server.MetClient, "_get", fake_get):
        assert post(port, "/api/harvest?department=13&limit=3")["started"]
        for _ in range(50):
            s = get(port, "/api/status")
            if not s["running"]: break
            time.sleep(0.1)
    assert (s["done"], s["fetched"], s["cached"], s["failed"]) == (3, 1, 1, 1)
    assert s["skipped"][0]["objectID"] == 7
    assert get(port, "/api/objects")["total"] == 2
    assert (cache_dir / "2.json").exists()  # new record written to the cache as {id}.json
    # live compare, with the Met API mocked: the Met changed creditLine and added an unknown key
    live = dict(SAMPLE, creditLine="Fletcher Fund, 1956 (updated)", newKey="x", metadataDate="2026-10-01T00:00:00Z")
    class R:
        status_code = 200
        def json(self): return live
    with patch.object(server.requests, "get", lambda *a, **k: R()):
        d = get(port, "/api/live/254904")
    assert d["status"] == 200
    assert {x["field"] for x in d["cache_diff"]} == {"creditLine", "newKey", "metadataDate"}
    assert d["model_unknown_keys"] == ["newKey"]
    assert {x["field"] for x in d["model_changed"]} == set()   # "1956" -> 1956 counts as equivalent
    assert d["unstaged"] == [] and d["triples"] > 20
    srv.shutdown()
