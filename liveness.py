"""Vérification que les annonces « nouvelles » répondent encore, en tâche de fond.

Une annonce disparue (404/410) n'a plus d'intérêt ; un site qui refuse les
robots (403, 429, captcha…) ou ne répond pas reste « non vérifiable » — on ne
conclut jamais à la disparition sans une réponse explicite du serveur.

Un seul lot à la fois : un clic pendant qu'une vérification tourne ne lance
rien de plus (même schéma de thread unique que linkhunt).
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import db

# Un vrai navigateur : beaucoup de job boards refusent les clients « python-urllib ».
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
MAX_WORKERS = 6

_lock = threading.Lock()
_thread: threading.Thread | None = None
_started_at: str | None = None
_queued = 0


def classify(status: int | None) -> str:
    """Code HTTP → ok | gone | unknown. None = timeout, DNS, connexion refusée…"""
    if status is None:
        return "unknown"
    if 200 <= status <= 399:
        return "ok"
    if status in (404, 410):
        return "gone"
    return "unknown"


def probe(url: str, timeout: float = 8.0) -> tuple[str, int | None]:
    """GET de l'URL (redirections suivies), en-têtes seulement. Ne lève jamais."""
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return "unknown", None
    status: int | None = None
    try:
        req = urllib.request.Request(url.strip(), method="GET", headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            status = resp.status
        finally:
            resp.close()  # on ne lit pas le corps : seul le code nous intéresse
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            e.close()
        except Exception:
            pass
    except Exception:
        status = None
    return classify(status), status


def _check_one(offer_id: int, url: str) -> None:
    try:
        state, _status = probe(url)
        db.set_url_state(offer_id, state)
    except Exception as e:  # un hôte mort ne doit pas arrêter le lot
        print(f"[liveness] offre {offer_id}: {type(e).__name__}: {e}", flush=True)


def _run(jobs: list[tuple[int, str]]) -> None:
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for oid, url in jobs:
                pool.submit(_check_one, oid, url)
    except Exception as e:  # pragma: no cover
        print(f"[liveness] {type(e).__name__}: {e}", flush=True)


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())


def enqueue_check() -> dict:
    """Lance la vérification des offres `new`. Ne bloque pas.

    Si un lot tourne déjà, on n'en lance pas un second : on renvoie son état.
    `started_at` permet au navigateur de savoir quand toutes les cartes ont
    été revérifiées (url_checked_at >= started_at).
    """
    global _thread, _started_at, _queued
    with _lock:
        if is_running():
            return {"running": True, "queued": _queued, "started_at": _started_at}
        jobs = db.offers_to_check("new")
        _queued = len(jobs)
        _started_at = db.now()
        if not jobs:
            return {"running": False, "queued": 0, "started_at": _started_at}
        _thread = threading.Thread(target=_run, args=(jobs,), name="liveness", daemon=True)
        _thread.start()
        return {"running": True, "queued": _queued, "started_at": _started_at}
