"""Recherche du lien de candidature d'une offre, en tâche de fond.

Un job = un sous-processus `hermes chat --oneshot` avec le skill
`application-link-hunt`. Rien à voir avec un run d'adaptation : pas de build/,
pas de docx, donc un second thread de travail est possible sans marcher sur
cv-tailor. Les jobs sont sérialisés entre eux (un seul agent à la fois).

L'état vit sur l'offre (`offers.apply_*`), pas dans une table de runs : une
offre n'a qu'un lien de candidature, le dernier gagne.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import config
import db

LOG_DIR = config.LOG_DIR
LINK_DIR = config.LINK_DIR
HERMES = config.HERMES_BIN
BUDGET = config.LINK_BUDGET

_stop = threading.Event()
_thread: threading.Thread | None = None

PROMPT_TEMPLATE = """Charge le skill application-link-hunt et applique-le pour
cette offre : trouve l'URL de candidature la plus directe (site de l'entreprise,
ATS, puis board où l'on postule réellement).

Entreprise : {company}
Poste : {title}
Lieu : {location}
Contrat : {contract}
Lien d'origine : {url}
Source : {source}

Écris le résultat dans :
{out_path}

Format exact (JSON strict, aucune phrase autour dans le fichier) :
{{"status": "ok|not_found|failed", "apply_url": "https://…",
  "kind": "site|ats|board", "confidence": "high|medium|low",
  "reason": "pourquoi ce lien (une phrase)",
  "alternatives": [{{"url": "https://…", "kind": "board", "note": "…"}}],
  "error": "seulement si status=failed"}}

Rappels : n'écris QUE des URL que tu as réellement vues et ouvertes ; si la page
ne mentionne pas l'entreprise et le poste, elle ne compte pas. Rien de fiable
trouvé → "status": "not_found" avec la raison. Termine ta réponse par la ligne
LINKFILE {out_path}
"""


def clean_url(value) -> str | None:
    """URL http(s) exploitable, ou None. Aucune autre forme n'est acceptée."""
    if not isinstance(value, str):
        return None
    url = value.strip().strip("<>\"' ")
    if not url.lower().startswith(("http://", "https://")):
        return None
    if len(url) > 800 or any(c in url for c in " \n\t"):
        return None
    return url


def build_prompt(offer: dict, out_path: Path) -> str:
    return PROMPT_TEMPLATE.format(
        company=offer.get("company") or "(inconnue)",
        title=offer.get("title") or "(inconnu)",
        location=offer.get("location") or "(inconnu)",
        contract=offer.get("contract") or "(inconnu)",
        url=offer.get("url") or "(aucun lien d'origine)",
        source=offer.get("source") or "inconnue",
        out_path=out_path,
    )


def normalise(raw: dict) -> dict:
    """Résultat de l'agent → structure d'affichage, sans URL non vérifiable."""
    status = str(raw.get("status") or "").strip().lower()
    if status not in ("ok", "not_found", "failed"):
        status = "failed"
    url = clean_url(raw.get("apply_url"))
    if status == "ok" and not url:
        return {"status": "failed", "url": None, "kind": None, "confidence": None,
                "reason": None, "alternatives": [],
                "error": "résultat ok sans URL exploitable"}
    kind = str(raw.get("kind") or "").strip().lower() or None
    confidence = str(raw.get("confidence") or "").strip().lower() or None
    alts = []
    for a in raw.get("alternatives") or []:
        if not isinstance(a, dict):
            continue
        alt = clean_url(a.get("url"))
        if not alt or alt == url:
            continue
        alts.append({"url": alt, "kind": (a.get("kind") or "")[:20], "note": (a.get("note") or "")[:200]})
    return {
        "status": status,
        "url": url,
        "kind": kind if kind in ("site", "ats", "board") else None,
        "confidence": confidence if confidence in ("high", "medium", "low") else None,
        "reason": (raw.get("reason") or "").strip()[:400] or None,
        "alternatives": alts[:5],
        "error": (raw.get("error") or "").strip()[:400] or None,
    }


def parse_result(path: Path | None, log_text: str = "") -> dict:
    """Lit le JSON écrit par l'agent, sinon le dernier bloc JSON de son log."""
    raw = None
    if path and path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        # Repli : l'agent a répondu dans le chat au lieu d'écrire le fichier.
        for chunk in reversed(log_text.split("```")):
            chunk = chunk.strip()
            start = chunk.find("{")
            if start < 0:
                continue
            try:
                cand = json.loads(chunk[start:chunk.rfind("}") + 1])
            except ValueError:
                continue
            if isinstance(cand, dict) and ("apply_url" in cand or "status" in cand):
                raw = cand
                break
    if not isinstance(raw, dict):
        return {"status": "failed", "url": None, "kind": None, "confidence": None,
                "reason": None, "alternatives": [],
                "error": "aucun résultat JSON produit par l'agent"}
    return normalise(raw)


def build_command(prompt_path: Path) -> list[str]:
    """Commande de l'agent de recherche (mêmes règles que le worker : pas --source)."""
    return [
        HERMES, "chat", "--query-file", str(prompt_path), "--oneshot", "-v",
        "-s", "application-link-hunt", "-t", "web,file",
        "--in", str(config.BASE_DIR), "--run-budget", str(BUDGET),
    ]


def enqueue(offer_id: int, force: bool = False) -> str:
    """Demande une recherche. Retourne l'état applicatif résultant.

    - déjà en file ou en cours → on ne double pas ;
    - un lien existe déjà et `force` est faux → on n'y touche pas ;
    - sinon l'offre passe en `queued`, le thread fera le reste.
    """
    with db.conn() as c:
        row = c.execute("SELECT id FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            raise KeyError(offer_id)
        state = db.apply_state(c, offer_id)
        if state in ("queued", "running"):
            return state
        if state in ("found", "manual") and not force:
            return state
        db.set_apply(c, offer_id, "queued")
        return "queued"


def _run_one(offer: dict) -> None:
    offer_id = offer["id"]
    LINK_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(offer, LINK_DIR / f"offer_{offer_id}.json")
    prompt_path = LINK_DIR / f"offer_{offer_id}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    log_path = LINK_DIR / f"offer_{offer_id}.log"
    out_path = LINK_DIR / f"offer_{offer_id}.json"
    if out_path.exists():
        out_path.unlink()  # un ancien résultat ne doit pas passer pour le nouveau

    cmd = build_command(prompt_path)
    error = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd)}\n\n")
            log.flush()
            subprocess.run(
                cmd, cwd=str(config.BASE_DIR), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=BUDGET + 120,
            )
    except subprocess.TimeoutExpired:
        error = f"timeout après {BUDGET + 120}s"
    except Exception as e:  # pragma: no cover
        error = f"{type(e).__name__}: {e}"

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    result = parse_result(out_path, log_text)
    if error:
        result = {"status": "failed", "url": None, "kind": None, "confidence": None,
                  "reason": None, "alternatives": [], "error": error}

    with db.conn() as c:
        # Paul a pu saisir un lien à la main pendant le run : sa saisie gagne.
        if db.apply_state(c, offer_id) != "running":
            return
        if result["status"] == "ok":
            db.set_apply(c, offer_id, "found", url=result["url"], kind=result["kind"],
                         confidence=result["confidence"], alternatives=result["alternatives"],
                         note=result["reason"])
        elif result["status"] == "not_found":
            db.set_apply(c, offer_id, "not_found", alternatives=result["alternatives"],
                         note=result["reason"] or "aucun lien de candidature vérifié")
        else:
            db.set_apply(c, offer_id, "failed",
                         note=result["error"] or "recherche du lien impossible")


def _loop() -> None:
    while not _stop.is_set():
        job = None
        try:
            with db.conn() as c:
                job = db.next_apply_job(c)
                if job:
                    db.set_apply(c, job["id"], "running")
            if job:
                _run_one(job)
                continue
        except Exception as e:  # ne jamais tuer la boucle
            print(f"[linkhunt] {type(e).__name__}: {e}", flush=True)
        _stop.wait(3)


def recover() -> None:
    """Après un restart, un job `running` est mort : on le marque échoué."""
    with db.conn() as c:
        for r in c.execute("SELECT id FROM offers WHERE apply_state = 'running'").fetchall():
            db.set_apply(c, r["id"], "failed",
                         note="recherche interrompue par un redémarrage du hub")


def start() -> None:
    global _thread
    recover()
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="link-hunt", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
