"""Configuration du hub : aucun chemin absolu en dur dans le code.

Chaque valeur se surcharge par variable d'environnement (voir `.env.example`).
Les valeurs par défaut sont calculées au chargement à partir de l'emplacement du
dépôt et du foyer de l'utilisateur : après un `git clone`, le projet tourne sans
qu'on ait à éditer un seul chemin.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# Racine du dépôt (le dossier de ce fichier).
BASE_DIR = Path(__file__).resolve().parent
HOME = Path.home()


def _path(var: str, default: Path) -> Path:
    """Chemin absolu résolu, surchargé par la variable d'environnement `var`."""
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


# --- Données locales -------------------------------------------------------
# La base est la seule source de vérité du board ; `logs/` et `data/prompts/`
# gardent la trace de chaque adaptation (offre + prompt envoyé à l'agent).
DB_PATH = _path("HUB_DB", BASE_DIR / "data" / "hub.db")
LOG_DIR = _path("HUB_LOG_DIR", BASE_DIR / "logs")
PROMPT_DIR = _path("HUB_PROMPT_DIR", BASE_DIR / "data" / "prompts")

# --- Projet cv-tailor (dépôt séparé, hors de celui-ci) ---------------------
CV_TAILOR_DIR = _path("CV_TAILOR_DIR", HOME / "cv-tailor")
CV_OUTPUT_DIR = _path("CV_OUTPUT_DIR", CV_TAILOR_DIR / "output")

# --- Entrées produites par le cron de veille ------------------------------
INBOX_PATH = _path("HUB_INBOX", HOME / "job-hunter" / "inbox.jsonl")
SENT_LOG_PATH = _path("HUB_SENT_LOG", INBOX_PATH.with_name("sent.jsonl"))

# --- Agent d'adaptation ----------------------------------------------------
HERMES_BIN = os.environ.get("HUB_HERMES_BIN", "").strip() or (
    shutil.which("hermes") or str(HOME / ".local" / "bin" / "hermes")
)

# Adresse qui reçoit l'archive des candidatures produites. Vide = pas de mail
# (l'agent saute l'étape au lieu d'écrire à une adresse en dur).
ARCHIVE_EMAIL = os.environ.get("HUB_ARCHIVE_EMAIL", "").strip()

# Propriétaire du hub, tel qu'annoncé dans le prompt de l'agent.
OWNER_NAME = os.environ.get("HUB_OWNER_NAME", "Paul Nougarède").strip()

# Budget d'un run d'adaptation, en secondes.
RUN_BUDGET = int(os.environ.get("HUB_RUN_BUDGET", "2700"))
