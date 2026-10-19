"""Worker d'adaptation CV + LM.

Un seul run à la fois (les scripts de cv-tailor écrivent dans build/ : deux runs
parallèles se marcheraient dessus). Le worker lance un vrai agent Hermes avec le
skill cv-application-tailor, suit le run en base et récupère le manifest.json
écrit par l'agent pour exposer les fichiers produits.

Prompt volontairement court : toute la procédure (recherche entreprise, plans,
budget de mots, QA, mail) est dans le skill, pas ici. Ce module ne fait que
transporter l'offre et vérifier le résultat.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import db

CV_TAILOR = Path("/home/paul/cv-tailor")
PROMPT_DIR = Path("/home/paul/job-hub/data/prompts")
LOG_DIR = Path("/home/paul/job-hub/logs")
HERMES = shutil.which("hermes") or "/home/paul/.local/bin/hermes"
RUN_BUDGET = int(os.environ.get("HUB_RUN_BUDGET", "2700"))  # secondes

_stop = threading.Event()
_thread: threading.Thread | None = None


PROMPT_TEMPLATE = """Tu prépares une candidature pour Paul Nougarède. Charge le skill
cv-application-tailor et suis-le intégralement : recherche de l'entreprise,
plans JSON, budget de mots, génération docx + PDF, QA, puis mail d'archive à
paul.nougarede78@gmail.com (avec les PDF ET les DOCX).

Offre à traiter (données fournies par le hub de candidatures) :

Titre : {title}
Entreprise : {company}
Lieu : {location}
Contrat : {contract}
URL : {url}
Score de matching /100 : {score}
Compétences matchées : {matched}
Compétences manquantes (NE RIEN INVENTER pour les couvrir) : {gaps}

Texte de l'offre :
---
{description}
---

Contraintes supplémentaires de ce run :
- Dossier de sortie obligatoire : /home/paul/cv-tailor/output/{date}_{company_slug}_{title_slug}/
- À la fin, écris TOI-MÊME /home/paul/cv-tailor/output/{date}_{company_slug}_{title_slug}/manifest.json
  au format exact :
  {{"status": "ok", "offre_url": "{url}", "company": "{company}", "title": "{title}",
    "language": "fr|en", "files": {{"cv_pdf": "...", "lm_pdf": "...", "cv_docx": "...", "lm_docx": "..."}},
    "email_sent": true|false, "company_summary": "3-4 phrases sur l'entreprise",
    "adapted": ["..."], "gaps": ["..."]}}
  Les chemins de "files" sont relatifs au dossier de sortie.
- Si une étape échoue (LibreOffice absent, rendu 2 pages, PDF manquant), NE
  prétends pas que c'est fini : mets "status": "failed" et "error" dans le manifest.
- Dernière ligne de ta réponse : MANIFEST_WRITTEN <chemin absolu du manifest>
"""


def _slug(s: str, maxlen: int = 40) -> str:
    s = (s or "").lower()
    s = re.sub(r"[àâä]", "a", s)
    s = re.sub(r"[éèêë]", "e", s)
    s = re.sub(r"[îï]", "i", s)
    s = re.sub(r"[ôö]", "o", s)
    s = re.sub(r"[ùûü]", "u", s)
    s = re.sub(r"[ç]", "c", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:maxlen].rstrip("-")) or "poste"


def _offer_text(offer: dict) -> str:
    desc = (offer.get("description") or "").strip()
    if len(desc) >= 200:
        return desc[:9000]
    # Description trop courte : l'agent ira chercher la page lui-même.
    return desc + (
        "\n\n[La description est incomplète ou absente : ouvre l'URL de l'offre "
        "et récupère le texte réel avant de rédiger quoi que ce soit. Si la page "
        "est inaccessible, dis-le dans le manifest et n'invente aucun contenu.]"
    )


def build_prompt(offer: dict) -> tuple[str, str]:
    """Retourne (prompt, nom du dossier de sortie)."""
    day = datetime.now().strftime("%Y-%m-%d")
    company_slug = _slug(offer.get("company") or "entreprise", 24)
    title_slug = _slug(offer.get("title") or "poste", 40)
    dirname = f"{day}_{company_slug}_{title_slug}"
    prompt = PROMPT_TEMPLATE.format(
        title=offer.get("title") or "",
        company=offer.get("company") or "",
        location=offer.get("location") or "",
        contract=offer.get("contract") or "",
        url=offer.get("url") or "(aucune — offre ajoutée à la main)",
        score=offer.get("score") if offer.get("score") is not None else "(non scoré)",
        matched=", ".join(offer.get("matched") or []) or "(aucune)",
        gaps=", ".join(offer.get("gaps") or []) or "(aucune)",
        description=_offer_text(offer),
        date=day,
        company_slug=company_slug,
        title_slug=title_slug,
    )
    return prompt, dirname


def enqueue(offer_id: int) -> int:
    """Crée un run 'queued'. Le worker le prendra au prochain tour."""
    with db.conn() as c:
        offer = c.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not offer:
            raise KeyError(offer_id)
        pending = c.execute(
            "SELECT id FROM runs WHERE offer_id = ? AND state IN ('queued','running')",
            (offer_id,),
        ).fetchone()
        if pending:
            return int(pending["id"])
        cur = c.execute(
            "INSERT INTO runs (offer_id, state, created_at) VALUES (?, 'queued', ?)",
            (offer_id, db.now()),
        )
        rid = int(cur.lastrowid)
        db.log_event(c, offer_id, "tailor_queued", f"run #{rid}")
        return rid


def _find_manifest(started: float, expected_dir: Path, log_text: str) -> Path | None:
    """Manifest écrit par l'agent : chemin annoncé, puis dossier attendu, puis scan."""
    m = re.findall(r"MANIFEST_WRITTEN\s+(\S+)", log_text)
    for cand in reversed(m):
        p = Path(cand.strip().strip('"'))
        if p.exists():
            return p
    p = expected_dir / "manifest.json"
    if p.exists():
        return p
    if (CV_TAILOR / "output").exists():
        cands = [
            d / "manifest.json"
            for d in (CV_TAILOR / "output").iterdir()
            if d.is_dir() and (d / "manifest.json").exists()
            and d.stat().st_mtime >= started - 5
        ]
        if cands:
            return max(cands, key=lambda x: x.stat().st_mtime)
    return None


def _collect_files(out_dir: Path) -> dict:
    out: dict[str, str] = {}
    if not out_dir or not out_dir.exists():
        return out
    for key, pattern in (
        ("cv_pdf", "cv_*.pdf"), ("lm_pdf", "lm_*.pdf"),
        ("cv_docx", "cv_*.docx"), ("lm_docx", "lm_*.docx"),
    ):
        hits = sorted(out_dir.glob(pattern))
        if hits:
            out[key] = hits[0].name
    return out


def _run_one(run_row: dict) -> None:
    rid, offer_id = run_row["id"], run_row["offer_id"]
    with db.conn() as c:
        offer = dict(c.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone())
        c.execute("UPDATE runs SET state='running', started_at=? WHERE id=?", (db.now(), rid))
    prompt, dirname = build_prompt(offer)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPT_DIR / f"run_{rid}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    log_path = LOG_DIR / f"run_{rid}.log"
    expected = CV_TAILOR / "output" / dirname
    started = time.time()

    cmd = [
        HERMES, "chat", "--query-file", str(prompt_path), "--oneshot", "-v",
        "-s", "cv-application-tailor", "-t", "web,file,terminal",
        "--in", str(CV_TAILOR), "--run-budget", str(RUN_BUDGET),
        "--source", "job-hub",
    ]
    ok, error = False, None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd)}\n\n")
            log.flush()
            proc = subprocess.run(
                cmd, cwd=str(CV_TAILOR), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=RUN_BUDGET + 300,
            )
        ok = proc.returncode == 0
        if not ok:
            error = f"hermes a quitté avec le code {proc.returncode}"
    except subprocess.TimeoutExpired:
        error = f"timeout après {RUN_BUDGET + 300}s"
    except Exception as e:  # pragma: no cover
        error = f"{type(e).__name__}: {e}"

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    manifest_path = _find_manifest(started, expected, log_text)
    manifest, files, out_dir = None, {}, None
    if manifest_path:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as e:
            error = f"manifest illisible: {e}"
        out_dir = manifest_path.parent
        files = _collect_files(out_dir)
        if manifest and manifest.get("files"):
            for k, v in manifest["files"].items():
                if v and (out_dir / v).exists():
                    files[k] = v
        if manifest and manifest.get("status") not in (None, "ok"):
            ok = False
            error = error or manifest.get("error") or f"status {manifest.get('status')}"
    elif ok:
        error = "run terminé sans manifest.json (fichiers non vérifiables)"

    with db.conn() as c:
        c.execute(
            "UPDATE runs SET state=?, finished_at=?, output_dir=?, files=?, manifest=?, error=?, log_path=? WHERE id=?",
            (
                "done" if ok else "failed", db.now(),
                str(out_dir) if out_dir else None,
                json.dumps(files, ensure_ascii=False),
                json.dumps(manifest, ensure_ascii=False) if manifest else None,
                error, str(log_path), rid,
            ),
        )
        if ok:
            db.log_event(c, offer_id, "documents_ready",
                         f"CV + LM dans {out_dir.name if out_dir else '?'}")
            # Une candidature préparée = à postuler ; Paul confirme l'envoi lui-même.
            row = c.execute("SELECT status FROM offers WHERE id=?", (offer_id,)).fetchone()
            if row and row["status"] in ("new", "shortlist"):
                db.set_status(c, offer_id, "shortlist", "Documents prêts (CV + LM)")
        else:
            db.log_event(c, offer_id, "tailor_failed", error or "échec inconnu")


def _loop() -> None:
    while not _stop.is_set():
        try:
            with db.conn() as c:
                row = c.execute(
                    "SELECT * FROM runs WHERE state='queued' ORDER BY id ASC LIMIT 1"
                ).fetchone()
            if row:
                _run_one(dict(row))
                continue
        except Exception as e:  # ne jamais tuer la boucle
            print(f"[worker] {type(e).__name__}: {e}", flush=True)
        _stop.wait(3)


def recover() -> None:
    """Après un redémarrage, un run 'running' est mort : on le marque échoué."""
    with db.conn() as c:
        rows = c.execute("SELECT id, offer_id FROM runs WHERE state='running'").fetchall()
        for r in rows:
            c.execute(
                "UPDATE runs SET state='failed', finished_at=?, error=? WHERE id=?",
                (db.now(), "interrompu par un redémarrage du hub", r["id"]),
            )
            db.log_event(c, r["offer_id"], "tailor_failed", "interrompu par un redémarrage")


def start() -> None:
    global _thread
    recover()
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="tailor-worker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
