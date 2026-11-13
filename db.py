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

import config

DB_PATH = config.DB_PATH
CV_TAILOR = config.CV_TAILOR_DIR

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

# Colonnes ajoutées après coup : `init_db()` les crée si la base les précède.
# Toute migration du hub reste additive — un restart ne doit rien détruire.
MIGRATIONS = {
    "apply_url": "TEXT",        # URL de candidature (site, ATS, board)
    "apply_kind": "TEXT",       # site|ats|board|manual
    "apply_confidence": "TEXT",  # high|medium|low
    "apply_alts": "TEXT",       # JSON list : pistes écartées, pour arbitrer
    "apply_state": "TEXT",      # idle|queued|running|found|not_found|failed|manual
    "apply_at": "TEXT",
    "apply_note": "TEXT",       # une phrase : pourquoi ce lien
    "url_state": "TEXT",        # ok|gone|unknown|NULL : l'annonce d'origine répond-elle encore ?
    "url_checked_at": "TEXT",
    "ft_state": "TEXT",         # démarches France Travail : voir FT_STATES
    "ft_at": "TEXT",
    "ft_note": "TEXT",          # question posée, ou raison de l'échec
}

# Résultats possibles d'une vérification de l'URL d'origine (voir liveness.py).
URL_STATES = ("ok", "gone", "closed", "unknown")


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
        have = {r["name"] for r in c.execute("PRAGMA table_info(offers)")}
        for name, coltype in MIGRATIONS.items():
            if name not in have:
                c.execute(f"ALTER TABLE offers ADD COLUMN {name} {coltype}")


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
    d["apply"] = {
        "state": d.pop("apply_state", None) or "idle",
        "url": d.pop("apply_url", None),
        "kind": d.pop("apply_kind", None),
        "confidence": d.pop("apply_confidence", None),
        "alternatives": _loads(d.pop("apply_alts", None)),
        "note": d.pop("apply_note", None),
        "at": d.pop("apply_at", None),
    }
    # Démarches France Travail : même forme que `apply`, l'état vit sur l'offre.
    d["ft"] = {
        "state": d.pop("ft_state", None) or "idle",
        "note": d.pop("ft_note", None),
        "at": d.pop("ft_at", None),
    }
    # Vérification de l'annonce d'origine : toujours présentes, même à None.
    d["url_state"] = d.get("url_state")
    d["url_checked_at"] = d.get("url_checked_at")
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


def delete_offer(oid: int) -> bool:
    """Supprime une offre ET retient son URL (pierre tombale).

    Sans la tombe, le prochain import automatique recrée la carte. Retourne
    True si une URL a été retenue ; lève KeyError si l'offre n'existe pas.
    """
    with conn() as c:
        r = c.execute("SELECT url FROM offers WHERE id = ?", (oid,)).fetchone()
        if not r:
            raise KeyError(oid)
        if r["url"]:
            c.execute("INSERT OR IGNORE INTO dismissed (url, at) VALUES (?, ?)",
                      (r["url"], now()))
        c.execute("DELETE FROM offers WHERE id = ?", (oid,))
    return bool(r["url"])


def latest_run(c: sqlite3.Connection, offer_id: int) -> dict | None:
    r = c.execute(
        "SELECT * FROM runs WHERE offer_id = ? ORDER BY id DESC LIMIT 1", (offer_id,)
    ).fetchone()
    return row_to_run(r) if r else None


# États d'une recherche de lien de candidature. `found`/`manual` gardent une URL,
# `queued`/`running` ne touchent pas à celle qui existe déjà.
APPLY_STATES = ("idle", "queued", "running", "found", "not_found", "failed", "manual")


def set_apply(c: sqlite3.Connection, offer_id: int, state: str, *,
              url: str | None = None, kind: str | None = None,
              confidence: str | None = None, alternatives: list | None = None,
              note: str | None = None) -> None:
    """Met à jour le lien de candidature d'une offre.

    Un changement de statut de recherche (`queued`, `running`) ne doit pas
    effacer l'URL déjà trouvée : on ne touche alors qu'à `apply_state`.
    """
    if state not in APPLY_STATES:
        raise ValueError(f"état de recherche inconnu: {state}")
    ts = now()
    if state in ("queued", "running"):
        c.execute(
            "UPDATE offers SET apply_state = ?, updated_at = ? WHERE id = ?",
            (state, ts, offer_id),
        )
        return
    c.execute(
        "UPDATE offers SET apply_state = ?, apply_url = ?, apply_kind = ?,"
        " apply_confidence = ?, apply_alts = ?, apply_note = ?, apply_at = ?,"
        " updated_at = ? WHERE id = ?",
        (state, url, kind, confidence,
         json.dumps(alternatives or [], ensure_ascii=False), note, ts, ts, offer_id),
    )
    if state == "found":
        log_event(c, offer_id, "apply_link", note or url or "")
    elif state == "manual":
        log_event(c, offer_id, "apply_link", f"lien saisi à la main : {url}")
    elif state in ("not_found", "failed"):
        log_event(c, offer_id, f"apply_link_{state}", note or "")


def apply_state(c: sqlite3.Connection, offer_id: int) -> str:
    r = c.execute("SELECT apply_state FROM offers WHERE id = ?", (offer_id,)).fetchone()
    return (r["apply_state"] if r and r["apply_state"] else "idle")


def next_apply_job(c: sqlite3.Connection) -> dict | None:
    """Prochaine offre à traiter, la plus ancienne d'abord."""
    r = c.execute(
        "SELECT * FROM offers WHERE apply_state = 'queued' ORDER BY updated_at ASC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


# --- Démarches France Travail (voir ftdemarches.py) ------------------------
# `awaiting_go` = question posée sur Telegram ; `filling` = agent au travail ;
# `blocked` = pas de bot configuré (échec fermé) ; `declined`/`expired` = pas
# d'accord ; `partial` = certaines démarches déjà présentes.
FT_STATES = ("idle", "queued", "awaiting_go", "filling", "done", "partial",
             "declined", "expired", "blocked", "failed")


def ft_state(c: sqlite3.Connection, offer_id: int) -> str:
    r = c.execute("SELECT ft_state FROM offers WHERE id = ?", (offer_id,)).fetchone()
    return (r["ft_state"] if r and r["ft_state"] else "idle")


def set_ft(c: sqlite3.Connection, offer_id: int, state: str, note: str | None = None) -> None:
    """Met à jour l'état des démarches France Travail d'une offre."""
    if state not in FT_STATES:
        raise ValueError(f"état de démarches inconnu: {state}")
    c.execute(
        "UPDATE offers SET ft_state = ?, ft_note = COALESCE(?, ft_note), ft_at = ?,"
        " updated_at = ? WHERE id = ?",
        (state, note, now(), now(), offer_id),
    )
    if state in ("done", "partial", "failed", "declined", "expired", "blocked"):
        log_event(c, offer_id, f"ft_{state}", (note or "")[:400])


def next_ft_job(c: sqlite3.Connection) -> dict | None:
    """Prochaine offre dont les démarches sont à créer."""
    r = c.execute(
        "SELECT * FROM offers WHERE ft_state = 'queued' ORDER BY updated_at ASC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


def set_url_state(offer_id: int, state: str) -> None:
    """Enregistre le résultat de la vérification de l'URL d'origine.

    Ne touche pas à `updated_at` : vérifier une annonce n'est pas la modifier.
    """
    if state not in URL_STATES:
        raise ValueError(f"état d'URL inconnu: {state}")
    with conn() as c:
        c.execute(
            "UPDATE offers SET url_state = ?, url_checked_at = ? WHERE id = ?",
            (state, now(), offer_id),
        )


def offers_to_check(statuses: str | tuple[str, ...] = "new") -> list[tuple[int, str]]:
    """(id, url) des offres de ces statuts qui ont une URL http(s) à vérifier."""
    if isinstance(statuses, str):
        statuses = (statuses,)
    if not statuses:
        return []
    marks = ",".join("?" * len(statuses))
    with conn() as c:
        rows = c.execute(
            f"SELECT id, url FROM offers WHERE status IN ({marks}) AND url IS NOT NULL ORDER BY id",
            tuple(statuses),
        ).fetchall()
    return [(r["id"], r["url"]) for r in rows
            if r["url"].strip().lower().startswith(("http://", "https://"))]


def active_run(c: sqlite3.Connection) -> dict | None:
    r = c.execute(
        "SELECT * FROM runs WHERE state IN ('queued','running') ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return row_to_run(r) if r else None
