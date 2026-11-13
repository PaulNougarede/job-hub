"""Worker d'adaptation CV + LM.

Un seul run à la fois (les scripts de cv-tailor écrivent dans build/ : deux runs
parallèles se marcheraient dessus). Le worker lance un vrai agent Hermes avec le
skill cv-application-tailor, suit le run en base et récupère le manifest.json
écrit par l'agent pour exposer les fichiers produits.

Prompt volontairement court : toute la procédure (recherche entreprise, plans,
budget de mots, QA) est dans le skill, pas ici. Ce module ne fait que
transporter l'offre et vérifier le résultat.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import config
import db

CV_TAILOR = config.CV_TAILOR_DIR
CV_OUTPUT = config.CV_OUTPUT_DIR
PROMPT_DIR = config.PROMPT_DIR
LOG_DIR = config.LOG_DIR
HERMES = config.HERMES_BIN
RUN_BUDGET = config.RUN_BUDGET

_stop = threading.Event()
_thread: threading.Thread | None = None


PROMPT_TEMPLATE = """Tu prépares une candidature pour {owner}. Charge le skill
cv-application-tailor et suis-le intégralement : recherche de l'entreprise,
plans JSON, budget de mots, génération docx + PDF, QA. Aucun mail n'est
envoyé : les fichiers restent dans le dossier de sortie, Paul lit la
candidature dans le hub.

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
- Dossier de sortie obligatoire : {out_dir}
- À la fin, écris TOI-MÊME {manifest_path}
  au format exact :
  {{"status": "ok", "offre_url": "{url}", "company": "{company}", "title": "{title}",
    "language": "fr|en", "files": {{"cv_pdf": "...", "lm_pdf": "...", "cv_docx": "...", "lm_docx": "..."}},
    "company_summary": "3-4 phrases sur l'entreprise",
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
    out_dir = CV_OUTPUT / dirname
    prompt = PROMPT_TEMPLATE.format(
        owner=config.OWNER_NAME,
        out_dir=out_dir,
        manifest_path=out_dir / "manifest.json",
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
    if CV_OUTPUT.exists():
        cands = [
            d / "manifest.json"
            for d in CV_OUTPUT.iterdir()
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


def build_command(prompt_path: Path) -> list[str]:
    """Commande de l'agent pour un run d'adaptation.

    Pas de `--source` : Hermes n'accepte que les sources annoncées par son
    gateway (`cli`, `oneshot`…) et refuse les autres en échouant au démarrage.
    Un run fini est déjà stocké `oneshot` par défaut, et le hub trace ses runs
    dans sa propre table — un tag maison n'apporte rien et casse le run.
    """
    return [
        HERMES, "chat", "--query-file", str(prompt_path), "--oneshot", "-v",
        "-s", "cv-application-tailor", "-t", "web,file,terminal",
        "--in", str(CV_TAILOR), "--run-budget", str(RUN_BUDGET),
    ]


def _spawn(cmd: list[str], log_path: Path, rid: int) -> subprocess.Popen:
    """Lance l'agent dans sa **propre session**, pid tracé en base.

    `start_new_session=True` détache l'agent de la session du hub : les signaux
    envoyés à uvicorn (arrêt, Ctrl-C) ne l'atteignent pas. Combiné à
    `KillMode=process` dans l'unité systemd (deploy/job-hub.service), un
    `systemctl --user restart job-hub` ne tue plus le run en cours : le pid
    survit, `recover()` le récupère au démarrage suivant.
    """
    log = log_path.open("w", encoding="utf-8")
    try:
        log.write(f"$ {' '.join(cmd)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, cwd=str(CV_TAILOR), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    finally:
        log.close()  # l'enfant garde son propre descripteur
    with db.conn() as c:
        c.execute("UPDATE runs SET pid=? WHERE id=?", (proc.pid, rid))
    return proc


def _kill_group(pid: int, grace: float = 10.0) -> None:
    """SIGTERM au groupe de l'agent, SIGKILL s'il traîne (il a sa propre session)."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.5)


def _wait(proc: subprocess.Popen, budget: float) -> tuple[int | None, bool]:
    """Attend la fin de l'agent ; retourne (code, timeout_atteint)."""
    deadline = time.time() + budget
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc, False
        if time.time() > deadline:
            _kill_group(proc.pid)
            try:
                return proc.wait(timeout=30), True
            except subprocess.TimeoutExpired:  # pragma: no cover
                return None, True
        time.sleep(2)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def _is_run_process(pid: int | None, rid: int) -> bool:
    """Ce pid est-il encore l'agent du run #rid ? (relecture de /proc)

    Un pid peut avoir été recyclé par un autre process après le redémarrage du
    hub : on ne se fie pas au numéro seul, on cherche `run_<rid>.txt` dans la
    ligne de commande, que `build_command` passe en `--query-file`.
    """
    if not _pid_alive(pid) or not isinstance(pid, int):
        return False
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return True  # pas de /proc (portabilité) : le pid vivant suffit
    return any(f"run_{rid}.txt".encode() in p for p in parts)


def _log_text(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _expected_dir(rid: int) -> Path | None:
    """Dossier de sortie attendu, relu dans le prompt du run (daté du lancement)."""
    m = re.search(r"Dossier de sortie obligatoire\s*:\s*(\S+)", _log_text(PROMPT_DIR / f"run_{rid}.txt"))
    return Path(m.group(1)) if m else None


def _started_epoch(started_at: str | None) -> float:
    if not started_at:
        return time.time()
    try:
        return datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return time.time()


def _finalize(rid: int, offer_id: int, *, ok: bool, error: str | None,
              started: float, expected: Path | None, log_path: Path) -> None:
    """Clôt un run : manifest, fichiers, état, événement. Idempotent.

    Appelé par le thread qui a lancé l'agent **et** par celui qui adopte un run
    survivant à un redémarrage — d'où la signature explicite.
    """
    manifest_path = _find_manifest(started, expected or CV_OUTPUT, _log_text(log_path))
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
        elif manifest:
            ok = True  # un manifest valide vaut mieux qu'un code de sortie inconnu
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


def _adopt(rid: int, offer_id: int, pid: int, started: float,
           expected: Path | None, log_path: Path) -> None:
    """Suit un agent survivant au redémarrage du hub (il n'est pas notre enfant :
    pas de `wait()`, on surveille /proc jusqu'à sa disparition)."""
    while _pid_alive(pid):
        time.sleep(3)
    _finalize(rid, offer_id, ok=True, error=None, started=started,
              expected=expected, log_path=log_path)


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
    expected = CV_OUTPUT / dirname
    started = time.time()

    cmd = build_command(prompt_path)
    rc, error = None, None
    try:
        proc = _spawn(cmd, log_path, rid)
        rc, timed_out = _wait(proc, RUN_BUDGET + 300)
        if timed_out:
            error = f"timeout après {RUN_BUDGET + 300}s (agent tué)"
        elif rc != 0:
            error = f"hermes a quitté avec le code {rc}"
    except Exception as e:  # pragma: no cover
        error = f"{type(e).__name__}: {e}"

    _finalize(rid, offer_id, ok=(rc == 0 and error is None), error=error,
              started=started, expected=expected, log_path=log_path)


def _loop() -> None:
    while not _stop.is_set():
        try:
            with db.conn() as c:
                row = db.next_queued_run(c)
            if row:
                _run_one(row)
                continue
        except Exception as e:  # ne jamais tuer la boucle
            print(f"[worker] {type(e).__name__}: {e}", flush=True)
        _stop.wait(3)


def recover() -> None:
    """Au démarrage : adopter les runs vivants, marquer `interrupted` les morts.

    Depuis que l'unité systemd tourne en `KillMode=process` et que l'agent a sa
    propre session (`_spawn`), un `systemctl --user restart job-hub` ne tue plus
    l'agent : il finit son travail pendant que le hub redémarre. Au retour, trois
    cas, dans cet ordre :

    - le manifest est déjà là → on clôt le run normalement (rien à relancer) ;
    - le pid vit encore et c'est bien cet agent → on l'adopte (`_adopt` suit
      /proc et clôt à sa sortie) ;
    - sinon l'agent est mort → `interrupted`, relançable d'un clic.
    """
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM runs WHERE state='running'")]
    for r in rows:
        rid, offer_id = r["id"], r["offer_id"]
        expected = _expected_dir(rid)
        started = _started_epoch(r.get("started_at"))
        log_path = LOG_DIR / f"run_{rid}.log"
        if _find_manifest(started, expected or CV_OUTPUT, _log_text(log_path)):
            _finalize(rid, offer_id, ok=True, error=None, started=started,
                      expected=expected, log_path=log_path)
            print(f"[worker] run {rid} : manifest déjà écrit, clôturé", flush=True)
            continue
        if _is_run_process(r.get("pid"), rid):
            print(f"[worker] run {rid} encore vivant (pid {r['pid']}) : adopté", flush=True)
            threading.Thread(target=_adopt, name=f"adopt-{rid}", daemon=True,
                             args=(rid, offer_id, int(r["pid"]), started,
                                   expected, log_path)).start()
            continue
        with db.conn() as c:
            c.execute(
                "UPDATE runs SET state='interrupted', finished_at=?, error=?, log_path=? WHERE id=?",
                (db.now(), "interrompu par un redémarrage du hub : l'agent n'a pas "
                           "survécu (relançable)", str(log_path), rid),
            )
            db.log_event(c, offer_id, "tailor_interrupted", "interrompu par un redémarrage")


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
