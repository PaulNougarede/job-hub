"""Ingestion des offres trouvées par le cron job-hunter, + amorçage de l'historique.

Le cron de veille écrit une ligne JSON par offre retenue dans le fichier pointé
par `HUB_INBOX` (par défaut `~/job-hunter/inbox.jsonl`, voir `config.py`).
Ce module les transforme en cartes du board (dédoublonnage par URL) et garde un
curseur pour ne pas relire ce qui est déjà importé.

Lancement manuel :  ./.venv/bin/python ingest.py [--seed]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import config
import db

INBOX = config.INBOX_PATH
SENT_LOG = config.SENT_LOG_PATH  # fallback si le cron écrit là
CV_TAILOR = config.CV_TAILOR_DIR


def _norm(d: dict) -> dict:
    """Accèpte les variantes de nommage d'un JSON produit par le LLM du cron."""
    def pick(*keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)):
                return v
        return None

    matched = d.get("matched") or d.get("skills") or d.get("matched_skills") or []
    gaps = d.get("gaps") or d.get("missing") or d.get("gaps_skills") or []
    if isinstance(matched, str):
        matched = [s.strip() for s in re.split(r"[,;·/]", matched) if s.strip()]
    if isinstance(gaps, str):
        gaps = [s.strip() for s in re.split(r"[,;·/]", gaps) if s.strip()]
    score = pick("score", "match", "match_score")
    try:
        score = int(str(score).split("/")[0]) if score is not None else None
    except ValueError:
        score = None
    return {
        "url": pick("url", "link", "href"),
        "title": pick("title", "job_title", "poste", "intitule"),
        "company": pick("company", "entreprise", "employer"),
        "location": pick("location", "lieu", "city", "ville"),
        "contract": pick("contract", "contrat", "type"),
        "score": score,
        "matched": [str(x) for x in matched][:8],
        "gaps": [str(x) for x in gaps][:6],
        "description": pick("description", "summary", "body", "snippet"),
        "source": pick("source") or "cron",
    }


def ingest_inbox(verbose: bool = False) -> dict:
    """Importe les nouvelles lignes de l'inbox. Idempotent (clé = url)."""
    stats = {"read": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}
    path = INBOX if INBOX.exists() else (SENT_LOG if SENT_LOG.exists() else None)
    if not path:
        return stats
    raw = path.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        stats["read"] += 1
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            data = _norm(obj)
            if not data["title"]:
                stats["errors"] += 1
                continue
            oid, created = db.upsert_offer(data)
            if oid is None:          # offre supprimée volontairement : on n'insiste pas
                stats["skipped"] += 1
                continue
            stats["created" if created else "updated"] += 1
            if verbose:
                print(f"{'NEW ' if created else 'seen'} #{oid} {data['title'][:70]}")
        except Exception as e:  # une ligne pourrie ne doit pas bloquer le reste
            stats["errors"] += 1
            if verbose:
                print(f"skip: {e}")
    return stats


def seed_from_cv_tailor(verbose: bool = False) -> int:
    """Crée une carte 'postulé' pour chaque candidature déjà produite par cv-tailor.

    Le nom du dossier est <date>_<entreprise>_<slug-poste> : c'est la seule
    mémoire des candidatures envoyées avant le hub. Idempotent : relancer la
    fonction rattache les dossiers manquants sans dupliquer les offres.
    """
    out = CV_TAILOR / "output"
    if not out.exists():
        return 0
    n = 0
    for d in sorted(out.iterdir()):
        if not d.is_dir() or len(list(d.glob("*.pdf"))) == 0:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_([a-z0-9]+)_(.+)$", d.name)
        if not m:
            continue
        day, company, slug = m.groups()
        files = sorted(p for p in d.iterdir() if p.suffix in (".pdf", ".docx"))
        fmap = {}
        for p in files:
            kind = ("cv" if p.name.startswith("cv") else "lm") + ("_pdf" if p.suffix == ".pdf" else "_docx")
            fmap[kind] = p.name
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM offers WHERE source='cv_tailor' AND notes LIKE ?",
                (f"%{d.name}%",),
            ).fetchone()
        if row:
            oid = row["id"]
        else:
            offer_txt = next(iter(sorted((CV_TAILOR / "offers").glob(f"{day}_{company}_*.txt"))), None)
            desc = offer_txt.read_text(encoding="utf-8", errors="replace")[:4000] if offer_txt else None
            title = slug.replace("-", " ").capitalize()
            if desc:
                first = next((ln.strip() for ln in desc.splitlines() if ln.strip()), "")
                first = re.sub(r"\s*\((H|F|H/F|F/H)\)\s*$", "", first)
                if 8 <= len(first) <= 90 and not first.lower().startswith(("source", "entreprise", "poste")):
                    title = first.title() if first.isupper() else first
            oid, created = db.upsert_offer({
                "url": None,
                "title": title,
                "company": company.capitalize(),
                "description": desc,
                "source": "cv_tailor",
                "found_at": f"{day}T12:00:00+02:00",
            })
            with db.conn() as c:
                c.execute("UPDATE offers SET notes = ?, applied_at = ? WHERE id = ?",
                          (f"Dossier de candidature : {d.name}\nFichiers : {', '.join(fmap.values())}",
                           f"{day}T12:00:00+02:00", oid))
                db.set_status(c, oid, "applied", "Historique importé depuis cv-tailor/output")
            n += 1
            if verbose:
                print(f"seed #{oid} {company} / {slug}")
        # Un dossier existant doit être consultable depuis le hub : on le rattache
        # comme un run terminé (aucun agent relancé, les fichiers sont déjà là).
        with db.conn() as c:
            if c.execute("SELECT 1 FROM runs WHERE offer_id = ?", (oid,)).fetchone():
                continue
            c.execute(
                "INSERT INTO runs (offer_id, state, started_at, finished_at, output_dir, files, manifest, created_at) "
                "VALUES (?, 'done', ?, ?, ?, ?, ?, ?)",
                (oid, f"{day}T12:00:00+02:00", f"{day}T12:00:00+02:00", str(d),
                 json.dumps(fmap, ensure_ascii=False),
                 json.dumps({"status": "ok", "files": fmap, "legacy": True}, ensure_ascii=False),
                 db.now()),
            )
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="importe l'historique cv-tailor/output")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    db.init_db()
    print("inbox:", ingest_inbox(a.verbose))
    if a.seed:
        print("seed:", seed_from_cv_tailor(a.verbose), "candidatures importées")
