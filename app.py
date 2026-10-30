"""Hub de candidatures — API FastAPI + board statique.

Lancement : ./.venv/bin/uvicorn app:app --host <tailscale-ip> --port 8765
(ou ./start.sh). Aucune authentification applicative : le service n'écoute QUE
sur l'interface Tailscale, donc seul le tailnet y accède.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import db
import ingest
import worker

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CV_OUTPUT = config.CV_OUTPUT_DIR
INBOX = config.INBOX_PATH


class OfferIn(BaseModel):
    title: str = Field(min_length=2)
    company: str | None = None
    url: str | None = None
    location: str | None = None
    contract: str | None = None
    description: str | None = None
    score: int | None = None
    matched: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    source: str = "manual"


class OfferPatch(BaseModel):
    status: str | None = None
    notes: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    contract: str | None = None
    score: int | None = None
    follow_up_at: str | None = None
    matched: list[str] | None = None
    gaps: list[str] | None = None
    description: str | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    ingest.ingest_inbox()
    worker.start()
    threading.Thread(target=_watch_inbox, name="inbox-watch", daemon=True).start()
    try:
        yield
    finally:
        _watch_stop.set()
        worker.stop()


app = FastAPI(title="Job Hub", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

_watch_stop = threading.Event()


def _stats(offers: list[dict]) -> dict:
    by = {s: 0 for s in db.STATUSES}
    for o in offers:
        by[o["status"]] = by.get(o["status"], 0) + 1
    active = sum(by[s] for s in ("applied", "interview", "offer"))
    return {
        "total": len(offers),
        "by_status": by,
        "active": active,
        "ready": sum(1 for o in offers if (o.get("run") or {}).get("state") == "done"),
    }


def _board():
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM offers ORDER BY (score IS NULL), score DESC, found_at DESC"
        ).fetchall()
        offers = []
        for r in rows:
            run = db.latest_run(c, r["id"])
            offers.append(db.row_to_offer(r, run))
    return {"offers": offers, "stats": _stats(offers)}


def _watch_inbox() -> None:
    """Ingestion continue : le cron écrit inbox.jsonl, le board se met à jour seul."""
    last = 0.0
    while not _watch_stop.is_set():
        try:
            if INBOX.exists():
                m = INBOX.stat().st_mtime
                if m > last:
                    ingest.ingest_inbox()
                    last = m  # seulement après succès : une erreur sera réessayée
        except Exception as e:
            print(f"[ingest] {type(e).__name__}: {e}", flush=True)
        _watch_stop.wait(20)


@app.get("/", response_class=HTMLResponse)
def index():
    """Sert la page en versionnant les assets par leur mtime : sans ça le
    navigateur garde l'ancien CSS/JS après une modif."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in ("style.css", "app.js"):
        try:
            v = int((STATIC / f).stat().st_mtime)
        except OSError:
            v = 0
        html = html.replace(f'/static/{f}"', f'/static/{f}?v={v}"')
    return html


@app.get("/api/health")
def health():
    """Sonde de vie : ne divulgue aucun chemin absolu du serveur."""
    return {
        "ok": True,
        "db": db.DB_PATH.name,
        "db_ready": db.DB_PATH.exists(),
        "cv_output_ready": CV_OUTPUT.is_dir(),
    }


@app.get("/api/board")
def board():
    return _board()


@app.get("/api/offers/{oid}")
def get_offer(oid: int):
    with db.conn() as c:
        r = c.execute("SELECT * FROM offers WHERE id = ?", (oid,)).fetchone()
        if not r:
            raise HTTPException(404, "offre inconnue")
        events = [dict(e) for e in c.execute(
            "SELECT * FROM events WHERE offer_id = ? ORDER BY id DESC LIMIT 40", (oid,)
        ).fetchall()]
        run = db.latest_run(c, oid)
    out = db.row_to_offer(r, run)
    out["events"] = events
    return out


@app.post("/api/offers")
def create_offer(payload: OfferIn):
    try:
        oid, created = db.upsert_offer(payload.model_dump(), respect_dismissed=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if oid is None:
        return {"id": None, "created": False, "dismissed": True}
    return {"id": oid, "created": created}


@app.patch("/api/offers/{oid}")
def patch_offer(oid: int, payload: OfferPatch):
    data = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    with db.conn() as c:
        r = c.execute("SELECT * FROM offers WHERE id = ?", (oid,)).fetchone()
        if not r:
            raise HTTPException(404, "offre inconnue")
        status = data.pop("status", None)
        if data:
            sets = []
            vals = []
            for k, v in data.items():
                sets.append(f"{k} = ?")
                vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v)
            sets.append("updated_at = ?")
            vals.extend([db.now(), oid])
            c.execute(f"UPDATE offers SET {', '.join(sets)} WHERE id = ?", vals)
        if status:
            db.set_status(c, oid, status)
    return {"ok": True}


@app.delete("/api/offers/{oid}")
def delete_offer(oid: int):
    """Supprime la carte ET retient l'URL : sans ça, le prochain import la
    recrée et elle réapparaît dans « Nouvelles offres »."""
    try:
        dismissed = db.delete_offer(oid)
    except KeyError:
        raise HTTPException(404, "offre inconnue")
    return {"ok": True, "dismissed": dismissed}


@app.post("/api/offers/{oid}/tailor")
def tailor(oid: int):
    with db.conn() as c:
        r = c.execute("SELECT id FROM offers WHERE id = ?", (oid,)).fetchone()
        if not r:
            raise HTTPException(404, "offre inconnue")
    rid = worker.enqueue(oid)
    return {"run_id": rid}


@app.get("/api/runs/{rid}/log")
def run_log(rid: int, lines: int = 60):
    """Dernières lignes du log d'un run (`lines` borné : la réponse lit un fichier)."""
    lines = max(1, min(lines, 500))
    with db.conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "run inconnu")
    p = Path(r["log_path"]) if r["log_path"] else None
    if not p or not p.exists():
        return {"lines": [], "state": r["state"]}
    txt = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": txt[-lines:], "state": r["state"], "total": len(txt)}


@app.get("/api/sites")
def sites():
    """Job boards à ouvrir à la main (éditable dans data/sites.json)."""
    p = ROOT / "data" / "sites.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


@app.post("/api/ingest")
def do_ingest():
    return ingest.ingest_inbox()


@app.get("/files/{path:path}")
def files(path: str):
    target = (CV_OUTPUT / path).resolve()
    if not target.is_relative_to(CV_OUTPUT) or not target.is_file():
        raise HTTPException(404, "fichier inconnu")
    return FileResponse(target, filename=target.name)


def _redact(text: str) -> str:
    """Retire le chemin du foyer des messages renvoyés au client."""
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


@app.exception_handler(Exception)
async def _err(request: Request, exc: Exception):
    # Trace complète côté serveur (journal systemd), message expurgé côté client.
    traceback.print_exc()
    return JSONResponse(
        {"error": _redact(f"{type(exc).__name__}: {exc}")}, status_code=500
    )
