# Met harvester diagnostics

A local web UI for checking the paa_graph_kb Met harvester based on what The Met Collection Public API returns for Greek and Roman Art objects. 
No dependency on `paa_graph_kb`.

## Setup

```bash
pip install -r requirements.txt        # requests, pydantic, rdflib
python server.py --cache data/met/objects
```

Open <http://localhost:8765>. Records are cached as `data/met/objects/<objectID>.json`.

## What it shows

**Harvest** — fetch a department (13 = Greek and Roman Art) with an optional limit; pause, stop,
re-index. Cached records are read from disk; only new ids hit the API, at ~20 requests/s. Failed ids
are listed with their error, including any record the model refuses.

**Objects** — browse the cache as an image grid; search; filter to objects where a chosen field is
empty. Open one to see the raw API record next to the Turtle it stages to, a presence strip for all
58 documented fields, the AAT lookups made for culture, object name, classification and medium, and
counts of image, measurement, person and subject nodes.

**Coverage** — across the whole cache: how often each field is filled, the share of objects whose
terms resolve to an AAT URI, and the most frequent terms that don't.

**Perseus links** — point it at a Perseus staging TTL to see how many Perseus objects held by the
Met match an accession number in the cache (whitespace and case ignored) and which don't.

**Live API** — call the Met directly for any id, bypassing the cache, and compare: cache vs live
(stale copies, museum edits), live vs model (unknown keys, values changed by validation), model vs
staging (filled fields that produced no triple).

## Relationship to paa_graph_kb

`metdiag/` holds copies of four pieces from that repo so this tool runs on its own:

| file | copied from |
|---|---|
| `metdiag/models.py` | `models/met/models.py` |
| `metdiag/staging.py` | `met_object_to_staging()` in `harvesters/met_harvester.py` |
| `metdiag/aat.py` | `vocabularies/aat_mappings.py` (verbatim) |
| `metdiag/client.py` | a smaller client with the same behaviour (`/objects` listing, per-object cache) |

The cost of standing alone is drift: if the repo changes its mapping or AAT tables, this tool keeps
showing the old behaviour until the copies are refreshed. Each file says where it came from.

The cache layout also differs: the repo's base client names files by request hash; this tool uses
`<objectID>.json`. The Objects/Coverage views read either, since they identify records by content,
so `--cache /path/to/paa_graph_kb/data/met/objects` works for inspection. Harvesting through this
tool writes `<objectID>.json` files, which the repo's client will not recognise as cached.

## Test

```bash
pip install -r requirements-dev.txt
pytest test_server.py
```

