from __future__ import annotations

import csv
import io
import json
from typing import Iterator

from .storage import Storage

CSV_COLS = ["domain", "url", "business_names", "city", "rubric", "phones",
            "http_status", "final_url", "note", "target_url", "analysis_status"]


def iter_jsonl(db: Storage, f: dict) -> Iterator[str]:
    for r in db.export_sites(f):
        yield json.dumps(r, ensure_ascii=False) + "\n"


def iter_csv(db: Storage, f: dict) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    yield buf.getvalue()
    for r in db.export_sites(f):
        buf.seek(0); buf.truncate()
        w.writerow([("; ".join(r[c]) if isinstance(r[c], list) else r[c]) for c in CSV_COLS])
        yield buf.getvalue()
