"""Schéma + accès SQLite du hub de candidatures.

Une seule base, un seul fichier (data/hub.db), mode WAL : le worker d'adaptation
écrit pendant que l'API lit, sans verrou bloquant.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("HUB_DB", "/home/paul/job-hub/data/hub.db"))
CV_TAILOR = Path("/home/paul/cv-tailor")

# Statuts du board, dans l'ordre d'affichage.
STATUSES = ["new", "shortlist", "applied", "interview", "rejected", "offer"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT,
    title       TEXT NOT NULL,
    company     TEXT,
    location    TEXT,
    contract    TEXT,
    seniority   TEXT,
    score       INTEGER,
    matched     TEXT,          -- JSON list
    gaps        TEXT,          -- JSON list
    description TEXT,
    source      TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    notes       TEXT,
    applied_at  TEXT,
    follow_up_at TEXT,
    found_at    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_offers_url ON offers(url) WHERE url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status, score DESC);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id    INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    state       TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
    started_at  TEXT,
    finished_at TEXT,
    output_dir  TEXT,
    files       TEXT,          -- JSON dict
    manifest    TEXT,          -- JSON du manifest produit par l'agent
    error       TEXT,
    log_path    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_offer ON runs(offer_id, id DESC);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER REFERENCES offers(id) ON DELETE CASCADE,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_offer ON events(offer_id, id DESC);

-- Pierres tombales : une offre supprimée ne doit pas revenir au prochain import.
CREATE TABLE IF NOT EXISTS dismissed (
    url TEXT PRIMARY KEY,
    at  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def _loads(v):
    if not v:
        return []
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return []


def row_to_offer(r: sqlite3.Row, run: dict | None = None) -> dict:
    d = dict(r)
    d["matched"] = _loads(d.get("matched"))
    d["gaps"] = _loads(d.get("gaps"))
    d["run"] = run
    return d


def row_to_run(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["files"] = _loads(d.get("files")) or {}
    m = d.get("manifest")
    d["manifest"] = _loads(m) or (json.loads(m) if m and m.strip().startswith("{") else None)
    return d


def log_event(c: sqlite3.Connection, offer_id: int, kind: str, message: str = "") -> None:
    c.execute(
        "INSERT INTO events (offer_id, at, kind, message) VALUES (?,?,?,?)",
        (offer_id, now(), kind, message),
    )


def upsert_offer(data: dict, respect_dismissed: bool = True) -> tuple[int | None, bool]:
    """Insère une offre ou complète l'existante (clé = url).

    Retourne (offer_id, created); offer_id vaut None si l'URL a été supprimée
    par Paul (pierre tombale) — l'import automatique l'ignore alors pour
    toujours. Un ajout manuel passe respect_dismissed=False et lève la pierre.
    """
    url = (data.get("url") or "").strip() or None
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("title requis")
    ts = now()
    if url:
        with conn() as c:
            tomb = c.execute("SELECT 1 FROM dismissed WHERE url = ?", (url,)).fetchone()
            if tomb and respect_dismissed:
                return None, False
            if tomb:
                c.execute("DELETE FROM dismissed WHERE url = ?", (url,))
    fields = {
        "url": url,
        "title": title,
        "company": (data.get("company") or "").strip() or None,
        "location": (data.get("location") or "").strip() or None,
        "contract": (data.get("contract") or "").strip() or None,
        "seniority": (data.get("seniority") or "").strip() or None,
        "score": data.get("score"),
        "matched": json.dumps(data.get("matched") or [], ensure_ascii=False),
        "gaps": json.dumps(data.get("gaps") or [], ensure_ascii=False),
        "description": (data.get("description") or "").strip() or None,
        "source": (data.get("source") or "manual").strip(),
    }
    with conn() as c:
        existing = None
        if url:
            existing = c.execute("SELECT * FROM offers WHERE url = ?", (url,)).fetchone()
        if existing:
            # Ne jamais écraser une saisie manuelle ; on complète les trous.
            upd = {}
            for k, v in fields.items():
                if v in (None, "", "[]") or k in ("url", "source"):
                    continue
                cur = existing[k]
                if cur in (None, "", "[]"):
                    upd[k] = v
            if upd:
                sets = ", ".join(f"{k} = ?" for k in upd)
                c.execute(
                    f"UPDATE offers SET {sets}, updated_at = ? WHERE id = ?",
                    (*upd.values(), ts, existing["id"]),
                )
            return existing["id"], False
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = c.execute(
            f"INSERT INTO offers ({cols}, status, found_at, created_at, updated_at) "
            f"VALUES ({marks}, 'new', ?, ?, ?)",
            (*fields.values(), data.get("found_at") or ts, ts, ts),
        )
        oid = int(cur.lastrowid)
        log_event(c, oid, "found", f"Source: {fields['source']}" + (f" · score {fields['score']}" if fields["score"] else ""))
        return oid, True


def set_status(c: sqlite3.Connection, offer_id: int, status: str, message: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"statut inconnu: {status}")
    ts = now()
    row = c.execute("SELECT status, applied_at FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if not row:
        raise KeyError(offer_id)
    applied = row["applied_at"]
    if status == "applied" and not applied:
        applied = ts
    c.execute(
        "UPDATE offers SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
        (status, applied, ts, offer_id),
    )
    if row["status"] != status:
        log_event(c, offer_id, "status", message or f"{row['status']} → {status}")


def latest_run(c: sqlite3.Connection, offer_id: int) -> dict | None:
    r = c.execute(
        "SELECT * FROM runs WHERE offer_id = ? ORDER BY id DESC LIMIT 1", (offer_id,)
    ).fetchone()
    return row_to_run(r) if r else None


def active_run(c: sqlite3.Connection) -> dict | None:
    r = c.execute(
        "SELECT * FROM runs WHERE state IN ('queued','running') ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return row_to_run(r) if r else None
