"""Démarches France Travail (ODD) créées au passage d'une offre en « postulé ».

Un job = deux temps : (1) les champs partent sur Telegram sous forme de question
go / pas go, (2) après un « go », un sous-processus `hermes chat --oneshot` avec
le skill `france-travail-admin` se connecte et saisit les trois démarches. Paul
approuve la double authentification sur son téléphone : il garde la main.

Troisième thread de travail du hub, sérialisé comme les autres : l'agent pilote
un navigateur, un seul à la fois.

L'état vit sur l'offre (`offers.ft_*`). France Travail ne sait pas supprimer une
démarche : on n'écrit donc jamais deux fois pour la même candidature sans
`force`.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import config
import db

FT_DIR = config.FT_DIR
HERMES = config.HERMES_BIN
BUDGET = config.FT_BUDGET
GO_TIMEOUT = config.FT_GO_TIMEOUT
TG_TOKEN = config.TG_TOKEN
TG_CHAT_ID = config.TG_CHAT_ID
FT_IDENTIFIER = config.FT_IDENTIFIER

# Valeurs figées, telles que Paul les écrit. On ne les corrige pas : elles
# partent dans un enregistrement qu'on ne peut pas supprimer.
SITE_RECHERCHE = "job-hunter (mon site)"
SITE_CV = "cv-tailor (mon site)"

GO_WORDS = ("go", "oui", "ok", "yes", "y", "vas-y", "vas y", "envoie", "c'est bon",
            "cest bon", "d'accord", "daccord")
NO_WORDS = ("non", "no", "n", "stop", "annule", "annuler", "laisse", "pas go",
            "pas d'accord")

_stop = threading.Event()
_thread: threading.Thread | None = None

PROMPT_TEMPLATE = """Charge le skill france-travail-admin et applique-le.

Contexte : Paul vient de postuler à cette offre. Il faut créer TROIS démarches
dans l'outil « Organisation des démarches » de son espace France Travail
(https://candidat.francetravail.fr/demarcheretouremploi/journal), et rien
d'autre. Il a déjà validé ces champs sur Telegram.

Offre :
- Entreprise : {entreprise}
- Poste : {metier}
- Date de réalisation : {date} (mode « Date », jamais « Échéance » ni « Période »)
- Commentaire : toujours vide
- Site pour la recherche d'offres : {site_recherche}
- Site pour le CV : {site_cv}

Démarches à créer, dans cet ordre :
1. « Répondre à des offres d'emploi » — champs : entreprise = {entreprise},
   métier = {metier}
2. « Rechercher des offres d'emploi ou d'entreprises », modalité « Sur internet »
   — champs : site = {site_recherche}, métier = {metier}, entreprise = {entreprise}
3. « Réaliser ou mettre à jour son CV sa lettre de motivation ou son portfolio »,
   modalité « En créant ou en mettant à jour mon CV et ou ma lettre de
   motivation » (c'est la seule qui propose un champ métier) — champs :
   site = {site_cv}, métier = {metier}

Interdits absolus :
- ne JAMAIS répondre à « L'avez-vous fait ? Oui/Non » : c'est Paul qui le fait ;
- ne rien mettre dans Commentaire ;
- ne pas modifier une démarche existante, ne pas en supprimer ;
- si une démarche identique existe déjà pour cette entreprise, ne pas la dupliquer :
  le signaler dans le compte-rendu et continuer avec les autres.

Rappels techniques vérifiés : le journal redirige vers la mire de connexion à
chaque visite (identifiant : {identifiant} ; mot de passe à prendre dans le
coffre Hermes, puis approbation de la 2FA par Paul sur son téléphone, puis
« Toujours confirmer »). Les champs sont `#dateFixe`, `#attribut-texte-0`,
`#attribut-texte-1`, `#attribut-texte-2` selon la démarche et
`#att-texte-commentaire` ; ce sont des inputs contrôlés, les remplir avec le
setter natif + événements input/change. Fermer la bannière cookies avant.

Écris le résultat dans :
{out_path}

Format exact (JSON strict, aucune phrase autour dans le fichier) :
{{"status": "ok|partial|failed",
  "demarches": ["libellé exact créé", "..."],
  "skipped": ["démarches déjà présentes, non recréées"],
  "note": "une phrase : ce qui bloque s'il y a un blocage"}}

Termine ta réponse par la ligne FTFILE {out_path}
"""


class MissingData(ValueError):
    """L'offre ne porte pas de quoi remplir un formulaire France Travail."""


def _fr_date(iso: str) -> str:
    """'2026-11-12T17:00:21+01:00' → '12/11/2026'. Vide si illisible."""
    raw = (iso or "")[:10]
    parts = raw.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    y, m, d = parts
    return f"{d}/{m}/{y}"


def payload(offer: dict) -> dict:
    """Champs des trois démarches, dérivés de l'offre. Rien d'inventé."""
    entreprise = (offer.get("company") or "").strip()
    metier = (offer.get("title") or "").strip()
    iso = offer.get("applied_at") or offer.get("updated_at") or ""
    date = _fr_date(iso)
    missing = [name for name, val in
               (("entreprise", entreprise), ("métier", metier), ("date", date)) if not val]
    if missing:
        raise MissingData("champ manquant : " + ", ".join(missing))
    return {"entreprise": entreprise, "metier": metier, "date": date,
            "site_recherche": SITE_RECHERCHE, "site_cv": SITE_CV,
            "identifiant": FT_IDENTIFIER or "(non configuré : HUB_FT_IDENTIFIER)"}


def message(p: dict) -> str:
    """Question envoyée sur Telegram : les champs exacts, puis go / pas go."""
    return (
        "France Travail — 3 démarches à créer ?\n\n"
        f"Offre : {p['entreprise']} — {p['metier']}\n"
        f"Date : {p['date']} (mode Date)\n"
        f"Commentaire : vide\n\n"
        f"1. Répondre à des offres d'emploi\n"
        f"   entreprise : {p['entreprise']}\n"
        f"   métier : {p['metier']}\n"
        f"2. Rechercher des offres d'emploi ou d'entreprises (Sur internet)\n"
        f"   site : {p['site_recherche']}\n"
        f"   métier : {p['metier']}\n"
        f"   entreprise : {p['entreprise']}\n"
        f"3. Réaliser ou mettre à jour son CV (création / mise à jour)\n"
        f"   site : {p['site_cv']}\n"
        f"   métier : {p['metier']}\n\n"
        "Je ne touche pas à « L'avez-vous fait ? ».\n"
        "Réponds « go » pour remplir, autre chose pour annuler.\n"
        "Tu devras approuver la connexion sur ton téléphone."
    )


def parse_reply(text: str | None) -> str | None:
    """'go' | 'no' | None (message qui ne tranche pas)."""
    if not text:
        return None
    clean = " ".join(text.strip().lower().split())
    if clean in GO_WORDS:
        return "go"
    if clean in NO_WORDS:
        return "no"
    return None


def build_prompt(p: dict, out_path: Path) -> str:
    return PROMPT_TEMPLATE.format(out_path=out_path, **p)


def configured() -> bool:
    """Sans bot Telegram, rien n'est rempli : l'accord doit être explicite."""
    return bool(TG_TOKEN and TG_CHAT_ID)


def redact(text: str) -> str:
    """Le jeton ne doit jamais atterrir dans un log ou une réponse HTTP."""
    return text.replace(TG_TOKEN, "***") if TG_TOKEN else text


# --- Telegram --------------------------------------------------------------

def _api(method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=40) as r:
        return json.loads(r.read().decode())


def send(text: str) -> bool:
    if not configured():
        return False
    try:
        _api("sendMessage", {"chat_id": TG_CHAT_ID, "text": text[:3900],
                             "disable_web_page_preview": "true"})
        return True
    except Exception as e:  # pragma: no cover - réseau
        print(f"[ft] send: {type(e).__name__}: {redact(str(e))}", flush=True)
        return False


def _updates(offset: int | None, wait: int = 0) -> list[dict]:
    params = {"timeout": wait}
    if offset is not None:
        params["offset"] = offset
    try:
        r = _api("getUpdates", params)
    except Exception as e:  # pragma: no cover - réseau
        print(f"[ft] getUpdates: {type(e).__name__}: {redact(str(e))}", flush=True)
        return []
    return r.get("result") or [] if r.get("ok") else []


def _next_offset(updates: list[dict]) -> int | None:
    ids: list[int] = []
    for u in updates:
        uid = u.get("update_id")
        if isinstance(uid, int):
            ids.append(uid)
    return max(ids) + 1 if ids else None


def wait_for_go(timeout: int | None = None) -> str:
    """Attend la réponse de Paul. 'go', 'no' ou 'expired'. Vide la file d'abord."""
    deadline = time.time() + (timeout if timeout is not None else GO_TIMEOUT)
    offset = _next_offset(_updates(None))  # les messages d'avant la question
    while time.time() < deadline:
        for u in _updates(offset, wait=20):
            offset = _next_offset([u]) or offset
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id") or "") != TG_CHAT_ID:
                continue
            verdict = parse_reply(msg.get("text"))
            if verdict:
                return verdict
        if _stop.is_set():
            break
    return "expired"


# --- File d'attente --------------------------------------------------------

def enqueue(offer_id: int, force: bool = False) -> str:
    """Demande la création des démarches. Retourne l'état applicatif résultant."""
    with db.conn() as c:
        row = c.execute("SELECT id FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            raise KeyError(offer_id)
        state = db.ft_state(c, offer_id)
        if state in ("queued", "awaiting_go", "filling"):
            return state
        if state == "done" and not force:
            return state
        if not configured():
            db.set_ft(c, offer_id, "blocked",
                      note="bot Telegram non configuré : accord impossible, rien n'est rempli")
            return "blocked"
        db.set_ft(c, offer_id, "queued")
        return "queued"


def parse_result(out_path: Path, log_text: str) -> dict:
    """JSON de l'agent ; repli sur le dernier bloc JSON du log."""
    raw = ""
    if out_path.exists():
        raw = out_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw and log_text:
        start = log_text.rfind("{")
        end = log_text.rfind("}")
        if start != -1 and end > start:
            raw = log_text[start:end + 1]
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("objet JSON attendu")
    except Exception:
        return {"status": "failed", "demarches": [], "skipped": [],
                "note": "aucun résultat JSON exploitable"}
    status = data.get("status") if data.get("status") in ("ok", "partial", "failed") else "failed"
    return {"status": status,
            "demarches": [str(d) for d in (data.get("demarches") or [])],
            "skipped": [str(d) for d in (data.get("skipped") or [])],
            "note": str(data.get("note") or "")}


def build_command(prompt_path: Path) -> list[str]:
    """Commande d'agent. Aucun `--source` : le gateway le refuserait."""
    return [HERMES, "chat", "--oneshot", "-s", "france-travail-admin",
            "--in", str(prompt_path)]


def _run_one(offer: dict) -> None:
    offer_id = offer["id"]
    FT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        p = payload(offer)
    except MissingData as e:
        with db.conn() as c:
            db.set_ft(c, offer_id, "failed", note=str(e))
        send(f"France Travail — rien rempli pour l'offre #{offer_id} : {e}")
        return

    with db.conn() as c:
        db.set_ft(c, offer_id, "awaiting_go", note=message(p))
    if not send(message(p)):
        with db.conn() as c:
            db.set_ft(c, offer_id, "failed", note="envoi Telegram impossible")
        return

    verdict = wait_for_go()
    if verdict != "go":
        with db.conn() as c:
            db.set_ft(c, offer_id, "declined" if verdict == "no" else "expired",
                      note="annulé" if verdict == "no" else "aucune réponse dans le délai")
        return

    prompt_path = FT_DIR / f"offer_{offer_id}.prompt.txt"
    out_path = FT_DIR / f"offer_{offer_id}.json"
    log_path = FT_DIR / f"offer_{offer_id}.log"
    prompt_path.write_text(build_prompt(p, out_path), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()  # un ancien résultat ne doit pas passer pour le nouveau

    with db.conn() as c:
        db.set_ft(c, offer_id, "filling", note="agent au travail, 2FA à approuver")
    send(f"France Travail — go reçu pour {p['entreprise']}. "
         "Je me connecte : approuve la notification sur ton téléphone.")

    cmd = build_command(prompt_path)
    error = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd)}\n\n")
            log.flush()
            subprocess.run(cmd, cwd=str(config.BASE_DIR), stdout=log,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=BUDGET + 120)
    except subprocess.TimeoutExpired:
        error = f"timeout après {BUDGET + 120}s"
    except Exception as e:  # pragma: no cover
        error = f"{type(e).__name__}: {e}"

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    result = parse_result(out_path, log_text)
    if error:
        result = {"status": "failed", "demarches": [], "skipped": [], "note": error}

    with db.conn() as c:
        if db.ft_state(c, offer_id) != "filling":
            return  # Paul a relancé à la main : sa relance gagne
        state = "done" if result["status"] == "ok" else (
            "partial" if result["status"] == "partial" else "failed")
        db.set_ft(c, offer_id, state, note=result["note"] or
                  ", ".join(result["demarches"]) or None)

    lines = [f"France Travail — {p['entreprise']} : {state}"]
    if result["demarches"]:
        lines += [f"• {d}" for d in result["demarches"]]
    if result["skipped"]:
        lines += [f"déjà présent, non recréé : {d}" for d in result["skipped"]]
    if result["note"]:
        lines += ["", result["note"]]
    lines += ["", "« L'avez-vous fait ? » reste à cocher par toi."]
    send("\n".join(lines))


def _loop() -> None:
    while not _stop.is_set():
        job = None
        try:
            with db.conn() as c:
                job = db.next_ft_job(c)
                if job:
                    db.set_ft(c, job["id"], "awaiting_go", note="question envoyée")
            if job:
                _run_one(job)
                continue
        except Exception as e:  # ne jamais tuer la boucle
            print(f"[ft] {type(e).__name__}: {redact(str(e))}", flush=True)
        _stop.wait(3)


def recover() -> None:
    """Après un restart, un job en cours est mort : on le marque échoué."""
    with db.conn() as c:
        for r in c.execute(
            "SELECT id FROM offers WHERE ft_state IN ('filling', 'awaiting_go')"
        ).fetchall():
            db.set_ft(c, r["id"], "failed",
                      note="interrompu par un redémarrage du hub")


def start() -> None:
    global _thread
    recover()
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="ft-demarches", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
