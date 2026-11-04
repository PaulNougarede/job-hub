"""Tests du hub : ingestion, dédoublonnage, statuts, chemins de fichiers.

    ./.venv/bin/python tests/test_hub.py     # ou python3 -m pytest tests/
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Une base jetable AVANT d'importer db (DB_PATH lu à l'import).
tmp = tempfile.mkdtemp(prefix="hubtest_")
os.environ["HUB_DB"] = str(Path(tmp) / "test.db")

import config  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402

PASS = 0


def check(cond, label):
    global PASS
    if not cond:
        raise AssertionError(label)
    PASS += 1
    print(f"ok  {label}")


def test_schema():
    db.init_db()
    with db.conn() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check({"offers", "runs", "events"} <= tables, "schéma créé")


def test_norm_variants():
    a = ingest._norm({"link": "http://x", "poste": "Data Scientist", "entreprise": "ACME",
                      "match": "82/100", "skills": "Python, RAG", "missing": "Airflow"})
    check(a["url"] == "http://x" and a["title"] == "Data Scientist" and a["score"] == 82,
          "variantes de champs reconnues")
    check(a["matched"] == ["Python", "RAG"] and a["gaps"] == ["Airflow"],
          "listes séparées par des virgules")


def test_upsert_dedup():
    oid, created = db.upsert_offer({"url": "http://job/1", "title": "Data Scientist",
                                    "company": "ACME", "score": 70, "matched": ["Python"]})
    check(created, "première insertion")
    oid2, created2 = db.upsert_offer({"url": "http://job/1", "title": "Data Scientist",
                                      "company": "ACME", "description": "texte complet"})
    check(not created2 and oid2 == oid, "même URL = même carte")
    with db.conn() as c:
        row = c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()
    check(row["description"] == "texte complet", "les trous sont complétés")
    check(row["score"] == 70, "une valeur existante n'est pas écrasée")


def test_status_flow():
    with db.conn() as c:
        row = c.execute("SELECT id FROM offers LIMIT 1").fetchone()
        oid = row["id"]
        db.set_status(c, oid, "shortlist")
        db.set_status(c, oid, "applied")
        r = c.execute("SELECT status, applied_at FROM offers WHERE id=?", (oid,)).fetchone()
        ev = c.execute("SELECT COUNT(*) FROM events WHERE offer_id=?", (oid,)).fetchone()[0]
    check(r["status"] == "applied" and r["applied_at"], "statut + date de candidature")
    check(ev >= 3, "chaque transition laisse un événement")
    try:
        with db.conn() as c:
            db.set_status(c, oid, "nimportequoi")
        raise AssertionError("statut inconnu accepté")
    except ValueError:
        check(True, "statut inconnu refusé")


def test_inbox_ingest():
    inbox = Path(tmp) / "inbox.jsonl"
    rows = [
        {"url": "http://job/2", "title": "ML Engineer", "company": "Beta", "score": 75,
         "matched": ["PyTorch"], "gaps": [], "description": "d", "source": "adzuna"},
        {"title": "sans url", "company": "Gamma"},          # accepté (offre manuelle)
        {"url": "http://job/2", "title": "ML Engineer"},     # doublon
        {"ceci": "n'est pas une offre"},                     # ignoré (pas de titre)
        "ligne cassée",
    ]
    inbox.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    old = ingest.INBOX
    ingest.INBOX = inbox
    try:
        stats = ingest.ingest_inbox()
    finally:
        ingest.INBOX = old
    check(stats["created"] == 2 and stats["errors"] == 1, f"comptage inbox: {stats}")
    check(stats["read"] == 4, "les lignes non-JSON sont ignorées sans casser")


def test_tombstone():
    """Une offre supprimée ne doit pas revenir par l'import automatique."""
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO dismissed (url, at) VALUES ('http://job/3', '2026-01-01')")
    oid, created = db.upsert_offer({"url": "http://job/3", "title": "Offre supprimée"})
    check(oid is None and not created, "import auto : URL supprimée ignorée")
    oid2, created2 = db.upsert_offer({"url": "http://job/3", "title": "Offre rétablie à la main"},
                                     respect_dismissed=False)
    check(oid2 is not None and created2, "ajout manuel : la pierre tombale est levée")
    with db.conn() as c:
        left = c.execute("SELECT COUNT(*) FROM dismissed WHERE url='http://job/3'").fetchone()[0]
    check(left == 0, "pierre tombale supprimée après ajout manuel")


def test_delete_offer():
    """Supprimer une offre pose une pierre tombale : elle ne revient pas."""
    oid, created = db.upsert_offer({"url": "http://job/supprimee", "title": "À supprimer"})
    assert oid is not None and created
    check(db.delete_offer(oid) is True, "suppression : URL retenue (tombstone)")
    with db.conn() as c:
        gone = c.execute("SELECT COUNT(*) FROM offers WHERE id=?", (oid,)).fetchone()[0]
        tomb = c.execute("SELECT COUNT(*) FROM dismissed WHERE url='http://job/supprimee'").fetchone()[0]
    check(gone == 0, "offre absente de la base")
    check(tomb == 1, "URL dans dismissed")
    oid2, created = db.upsert_offer({"url": "http://job/supprimee", "title": "Réimport"})
    check(oid2 is None and not created, "import auto : offre supprimée non récréée")
    try:
        db.delete_offer(999999)
        check(False, "suppression d'une offre inconnue doit lever KeyError")
    except KeyError:
        check(True, "offre inconnue → KeyError")


def test_file_guard():
    """Le service de fichiers ne doit jamais sortir de cv-tailor/output."""
    root = config.CV_OUTPUT_DIR
    for probe in ("../data/hub.db", "../../job-hub/data/hub.db", "/etc/passwd"):
        target = (root / probe).resolve()
        check(not target.is_relative_to(root), f"chemin bloqué: {probe}")


def test_pwa_icons():
    """L'icône du raccourci iPhone existe et est un PNG 180x180 opaque."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check('rel="apple-touch-icon"' in html, "index.html déclare apple-touch-icon")
    href = html.split('rel="apple-touch-icon"')[1].split('href="')[1].split('"')[0]
    icon = ROOT / href.lstrip("/")
    check(icon.is_file(), f"icône présente sur le disque ({href})")
    data = icon.read_bytes()
    check(data[:8] == b"\x89PNG\r\n\x1a\n", "icône au format PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    check((width, height) == (180, 180), f"icône en 180x180 (={width}x{height})")
    check(data[25] in (0, 2), "PNG sans canal alpha (iOS rend la transparence en noir)")
    manifest = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["icons"]:
        path = ROOT / entry["src"].lstrip("/")
        check(path.is_file(), f"manifeste : {entry['src']} présent")


def test_branding():
    """Nom de l'app et logo fourni : en-tête, favicon et manifeste."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))
    check("<title>jobhunter</title>" in html, "titre de l'onglet = jobhunter")
    check(manifest["name"] == "jobhunter" and manifest["short_name"] == "jobhunter",
          "manifeste nommé jobhunter")
    check('apple-mobile-web-app-title" content="jobhunter"' in html,
          "titre du raccourci iOS = jobhunter")
    check("<h1>jobhunter</h1>" in html, "marque de l'en-tête = jobhunter")
    check("Candidatures — Paul" not in html, "l'ancien nom a disparu du HTML")
    check("data:image/svg+xml" not in html, "plus de favicon SVG en data-URI")

    # le logo fourni remplace la pastille dégradée
    check(".brand::before" not in css, "la pastille dégradée a disparu du CSS")
    check('class="brand-mark"' in html, "l'en-tête porte le logo en image")
    src = html.split('class="brand-mark"')[1].split('src="')[1].split('"')[0]
    check((ROOT / src.lstrip("/")).is_file(), f"logo de l'en-tête présent ({src})")

    # chaque icône d'onglet existe sur le disque
    icons = [chunk.split('href="')[1].split('"')[0]
             for chunk in html.split('rel="icon"')[1:]]
    check(icons, "index.html déclare au moins une icône")
    for href in icons:
        path = ROOT / href.lstrip("/")
        check(path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
              f"icône PNG présente ({href})")


def _rule(css: str, selector: str) -> str:
    """Corps de la première règle dont le sélecteur exact commence ainsi."""
    idx = css.find(selector)
    if idx < 0:
        return ""
    return css[idx:css.find("}", idx)]


def test_theme_assets():
    """Charte « papier » sobre : une seule police, contours nets, pas de crayonné."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for face in ("inter-latin.woff2", "inter-latin-ext.woff2"):
        check(face in css, f"@font-face déclare {face}")
        check((ROOT / "static" / "fonts" / face).is_file(), f"police présente : {face}")
    check("kalam" not in css.lower(), "l'ancienne police manuscrite a disparu du CSS")
    check("kalam" not in html.lower(), "l'ancienne police manuscrite a disparu du HTML")
    check('id="pencil"' not in html, "le filtre crayon n'est plus défini")
    check("url(#pencil)" not in css, "aucun contour filtré « crayon » restant")
    check("--font-hand" not in css, "plus de famille manuscrite dans les tokens")

    # la typographie unique s'applique à tous les champs textuels
    check("--font-ui" in css, "token --font-ui défini")
    check("var(--font-ui)" in _rule(css, "input, select, textarea, button"),
          "champs de formulaire et boutons : var(--font-ui)")
    fields = _rule(css, "select, textarea, input[type=text]")
    check("font-family: var(--font-ui)" in fields, "select/textarea/input : var(--font-ui)")
    check("var(--font-ui)" in _rule(css, ".search input"), "champ de recherche : var(--font-ui)")
    for selector in (".brand h1", ".toolbar-title h2", ".col-head h2", ".card h3",
                     ".drawer-head h2", ".modal h2", ".detail-title h1", ".md h2"):
        body = _rule(css, selector)
        check(body and "font-family" not in body, f"{selector} hérite de la famille unique")


def test_detail_view_assets():
    """La fiche d'une offre est une vraie vue, bâtie sur la charte existante."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    check('id="viewOffer"' in html, "index.html porte la vue fiche")
    check('id="tabbar"' in html, "index.html porte la barre d'onglets internes")
    check('id="dExpand"' in html, "le tiroir a un bouton « ouvrir en grand »")
    expand = html.split('id="dExpand"')[1].split(">")[0]
    check("target=" not in expand,
          "« ouvrir en grand » n'ouvre plus de fenêtre du navigateur")
    for needle in ("localStorage", "TABS_KEY", "function renderTabs", "function closeTab"):
        check(needle in js, f"onglets internes : {needle}")
    check("ensureTab(state.offerId)" in js, "la route #/offre/<id> crée son onglet")
    check("'#/offre/' + o.id" in js, "le lien du tiroir pointe sur la fiche")
    check("/^offre\\/(\\d+)/" in js, "le routeur reconnaît #/offre/<id>")
    check('id="oStatus"' in js and 'id="oNotes"' in js,
          "statut et notes modifiables depuis la fiche")
    check("research.markdown" in js and "o.documents" in js,
          "la fiche affiche la recherche entreprise et les documents")

    body = _rule(css, ".detail-body")
    check("grid-template-columns" in body, "fiche en deux colonnes")
    check("var(--paper-card)" in _rule(css, ".panel"), "cartes de la fiche sur --paper-card")
    check("var(--line)" in _rule(css, ".panel"), "filet 1 px existant, pas de nouvelle ombre dure")
    for token in ("--good", "--warn", "--bad"):
        check(token in css, f"jeton d'état {token} défini")


def test_doc_helpers():
    """Nommage des fichiers et garde-fous de /api/offers/<id>/doc/<kind>."""
    import files as docfiles

    check(docfiles.doc_filename("cv_pdf", "Groupe VYV", "2026-10-30")
          == "CV_Groupe-VYV_2026-10-30.pdf", "nom de CV lisible et sans accent")
    check(docfiles.doc_filename("lm_docx", "Brief IA", "2026-10-30")
          == "Lettre_Brief-IA_2026-10-30.docx", "nom de lettre lisible")
    try:
        docfiles.doc_filename("autre", "X", "2026-01-01")
        check(False, "type de document inconnu doit lever")
    except ValueError:
        check(True, "type de document inconnu refusé")

    root = Path(tmp) / "output"
    out = root / "2026-10-30_acme_ds"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cv_fr.pdf").write_bytes(b"%PDF-1.4")
    files_map = {"cv_pdf": "cv_fr.pdf", "lm_pdf": "absent.pdf"}
    check(docfiles.resolve_doc(out, files_map, "cv_pdf", root) == (out / "cv_fr.pdf").resolve(),
          "document existant résolu")
    check(docfiles.resolve_doc(out, files_map, "lm_pdf", root) is None,
          "fichier annoncé mais absent → None")
    check(docfiles.resolve_doc(out, {"cv_pdf": "../../data/hub.db"}, "cv_pdf", root) is None,
          "traversée de chemin refusée")
    docs = docfiles.documents(out, files_map, "Acme", "2026-10-30", root)
    check(len(docs) == 1 and docs[0]["filename"] == "CV_Acme_2026-10-30.pdf",
          f"un seul document servi : {docs}")


def test_offer_payload():
    """GET /api/offers/<id> joint documents nommés + recherche entreprise."""
    import app as app_module

    oid, _ = db.upsert_offer({"url": "http://job/detail", "title": "Data Scientist",
                              "company": "Acme", "score": 80})
    assert oid is not None
    root = Path(tmp) / "payload"
    out = root / "2026-10-30_acme_ds"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cv_fr.pdf").write_bytes(b"%PDF-1.4")
    (out / "cv_fr.docx").write_bytes(b"PK")
    (out / "company_research.md").write_text("# Acme\n\nFondée en 1999.", encoding="utf-8")
    manifest = {"status": "ok", "company_summary": "Acme vend des logiciels.",
                "company_research": "company_research.md"}
    with db.conn() as c:
        c.execute(
            "INSERT INTO runs (offer_id, state, output_dir, files, manifest, created_at)"
            " VALUES (?, 'done', ?, ?, ?, ?)",
            (oid, str(out), json.dumps({"cv_pdf": "cv_fr.pdf", "cv_docx": "cv_fr.docx"}),
             json.dumps(manifest), db.now()),
        )
    old = app_module.CV_OUTPUT
    app_module.CV_OUTPUT = root
    try:
        payload = app_module.get_offer(oid)
        kinds = [d["kind"] for d in payload["documents"]]
        check(kinds == ["cv_pdf", "cv_docx"], f"documents présents seulement : {kinds}")
        check(payload["documents"][0]["filename"].startswith("CV_Acme_"),
              "nom de fichier téléchargé parlant")
        check(payload["research"]["kind"] == "file"
              and "Fondée en 1999" in payload["research"]["markdown"],
              "recherche entreprise lue depuis le dossier de candidature")

        # Sans fichier de recherche : repli sur le résumé du manifest, annoncé.
        (out / "company_research.md").unlink()
        payload = app_module.get_offer(oid)
        check(payload["research"]["kind"] == "summary"
              and payload["research"]["markdown"] == "Acme vend des logiciels.",
              "repli sur company_summary, signalé comme résumé")
        check(payload["documents"], "les documents restent servis")
    finally:
        app_module.CV_OUTPUT = old


def test_apply_schema_and_enqueue():
    """Migration additive + file d'attente du lien de candidature."""
    with db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(offers)")}
    check({"apply_url", "apply_state", "apply_at", "apply_alts"} <= cols,
          "colonnes de candidature présentes après init_db")

    import linkhunt

    oid, _ = db.upsert_offer({"url": "http://job/lien", "title": "Data Scientist",
                              "company": "Acme"})
    assert oid is not None
    check(linkhunt.enqueue(oid) == "queued", "enqueue passe l'offre en file")
    check(linkhunt.enqueue(oid) == "queued", "pas de doublon quand la recherche est en file")
    with db.conn() as c:
        check(db.apply_state(c, oid) == "queued", "état persisté en base")
        db.set_apply(c, oid, "found", url="https://acme.example/jobs/1", kind="ats",
                     confidence="high", note="page ATS de l'entreprise")
        check(db.apply_state(c, oid) == "found", "résultat enregistré")
        # un état transitoire ne doit pas effacer le lien déjà trouvé
        db.set_apply(c, oid, "running")
        row = c.execute("SELECT apply_url FROM offers WHERE id=?", (oid,)).fetchone()
        check(row["apply_url"] == "https://acme.example/jobs/1",
              "un run qui démarre ne perd pas le lien existant")
        db.set_apply(c, oid, "found", url="https://acme.example/jobs/1", kind="ats",
                     confidence="high", note="page ATS de l'entreprise")
    check(linkhunt.enqueue(oid) == "found", "un lien trouvé n'est pas recherché à nouveau")
    check(linkhunt.enqueue(oid, force=True) == "queued", "relance possible avec force")
    with db.conn() as c:
        db.set_apply(c, oid, "manual", url="https://acme.example/postuler", kind="manual",
                     confidence="high")
        check(db.apply_state(c, oid) == "manual", "saisie manuelle tracée")
        r = db.row_to_offer(c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone())
    check(r["apply"]["url"] == "https://acme.example/postuler"
          and r["apply"]["kind"] == "manual" and "apply_url" not in r,
          "row_to_offer expose un objet apply et masque les colonnes brutes")


def test_linkhunt_result():
    """Le résultat de l'agent est normalisé ; aucune URL non http n'est retenue."""
    import linkhunt

    ok = linkhunt.normalise({"status": "ok", "apply_url": " https://x.example/j/1 ",
                             "kind": "site", "confidence": "high", "reason": "page carrière",
                             "alternatives": [{"url": "https://x.example/j/1", "kind": "board"},
                                              {"url": "javascript:alert(1)"},
                                              {"url": "https://board.example/x", "kind": "board",
                                               "note": "même annonce"}]})
    check(ok["url"] == "https://x.example/j/1" and ok["kind"] == "site", "URL nettoyée")
    check(len(ok["alternatives"]) == 1 and ok["alternatives"][0]["url"] == "https://board.example/x",
          "alternatives filtrées (doublon et javascript: écartés)")

    bad = linkhunt.normalise({"status": "ok", "apply_url": "pas une url"})
    check(bad["status"] == "failed" and bad["url"] is None,
          "status ok sans URL valide → échec explicite")

    nf = linkhunt.normalise({"status": "not_found", "reason": "offre retirée"})
    check(nf["status"] == "not_found" and nf["reason"] == "offre retirée", "not_found conservé")

    tmp_json = Path(tmp) / "link.json"
    tmp_json.write_text(json.dumps({"status": "ok", "apply_url": "https://acme.example/a",
                                    "kind": "ats", "confidence": "medium", "reason": "ATS"}),
                        encoding="utf-8")
    parsed = linkhunt.parse_result(tmp_json, "")
    check(parsed["status"] == "ok" and parsed["confidence"] == "medium", "résultat lu depuis le fichier")
    tmp_json.unlink()
    log = 'bavardage\n```json\n{"status": "ok", "apply_url": "https://acme.example/b", "kind": "site"}\n```\nLINKFILE /x'
    parsed = linkhunt.parse_result(tmp_json, log)
    check(parsed["url"] == "https://acme.example/b", "repli : JSON lu dans le log si le fichier manque")
    parsed = linkhunt.parse_result(tmp_json, "rien du tout")
    check(parsed["status"] == "failed", "aucun JSON → échec, pas d'invention")


def test_linkhunt_prompt_and_command():
    import linkhunt

    prompt = linkhunt.build_prompt(
        {"title": "Data Scientist", "company": "Acme", "url": "https://board.example/1",
         "location": "Paris", "contract": "CDI", "source": "adzuna"},
        Path(tmp) / "offer_1.json",
    )
    for needle in ("Acme", "Data Scientist", "https://board.example/1", "offer_1.json",
                   "LINKFILE"):
        check(needle in prompt, f"prompt contient {needle}")
    cmd = linkhunt.build_command(Path("/tmp/p.txt"))
    check("--source" not in cmd, "aucun --source dans la commande de recherche")
    check(cmd[cmd.index("-s") + 1] == "application-link-hunt", "skill de recherche chargé")
    check(cmd[cmd.index("-t") + 1] == "web,file", "toolsets web+file seulement")
    check(str(config.BASE_DIR) in cmd, "--in pointe sur le dépôt du hub")


def test_apply_trigger_and_manual_override():
    """Passer en À postuler lance la recherche ; l'URL manuelle gagne."""
    import app as app_module

    oid, _ = db.upsert_offer({"url": "http://job/trigger", "title": "ML Engineer",
                              "company": "Beta"})
    assert oid is not None
    app_module.patch_offer(oid, app_module.OfferPatch(status="shortlist"))
    with db.conn() as c:
        check(db.apply_state(c, oid) == "queued", "passage en À postuler → recherche en file")

    app_module.patch_offer(oid, app_module.OfferPatch(apply_url="https://beta.example/postuler"))
    with db.conn() as c:
        row = c.execute("SELECT apply_url, apply_state FROM offers WHERE id=?", (oid,)).fetchone()
    check(row["apply_url"] == "https://beta.example/postuler" and row["apply_state"] == "manual",
          "URL collée à la main enregistrée")

    app_module.patch_offer(oid, app_module.OfferPatch(apply_url="javascript:alert(1)"))
    with db.conn() as c:
        row = c.execute("SELECT apply_url, apply_state FROM offers WHERE id=?", (oid,)).fetchone()
    check(row["apply_url"] is None and row["apply_state"] == "idle",
          "URL non http refusée et lien effacé")


def test_config_paths():
    """Aucun chemin personnel en dur : tout passe par config.py + l'environnement."""
    sources = ["app.py", "config.py", "db.py", "files.py", "ingest.py", "linkhunt.py",
               "worker.py",
               "start.sh", "scripts/issue-branch.sh"]
    offenders = []
    for name in sources:
        p = ROOT / name
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "/home/" in line or "/Users/" in line:
                offenders.append(f"{name}:{i}")
    check(not offenders, f"aucun chemin absolu de foyer en dur ({', '.join(offenders)})")

    # Les valeurs par défaut sont calculées, jamais écrites : on les lit dans un
    # sous-processus dont l'environnement ne porte aucun HUB_* (le bac à sable du
    # test, lui, force HUB_DB).
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HUB_", "CV_TAILOR", "CV_OUTPUT"))}
    out = subprocess.run(
        [sys.executable, "-c", "import json, config; print(json.dumps({"
         "'db': str(config.DB_PATH), 'cv': str(config.CV_TAILOR_DIR), "
         "'out': str(config.CV_OUTPUT_DIR), 'inbox': str(config.INBOX_PATH), "
         "'sent': str(config.SENT_LOG_PATH)}))"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    check(out.returncode == 0, f"config s'importe sans variable d'environnement ({out.stderr[-200:]})")
    defaults = json.loads(out.stdout)
    check(defaults["db"] == str((ROOT / "data" / "hub.db").resolve()), f"DB par défaut : {defaults['db']}")
    check(defaults["cv"] == str((Path.home() / "cv-tailor").resolve()), f"cv-tailor par défaut : {defaults['cv']}")
    check(defaults["out"] == str((Path(defaults["cv"]) / "output")), f"sortie sous cv-tailor : {defaults['out']}")
    check(defaults["inbox"] == str((Path.home() / "job-hunter" / "inbox.jsonl").resolve()),
          f"inbox par défaut : {defaults['inbox']}")
    check(Path(defaults["sent"]).parent == Path(defaults["inbox"]).parent, "sent.jsonl à côté de l'inbox")

    # Une valeur fournie par l'environnement gagne (HUB_DB du bac à sable).
    check(config.DB_PATH == Path(os.environ["HUB_DB"]).resolve(), "HUB_DB surcharge la valeur par défaut")


def test_prompt_paths():
    """Le prompt de l'agent ne contient plus ni chemin figé ni adresse en dur."""
    import worker

    prompt, dirname = worker.build_prompt({
        "title": "Data Scientist", "company": "Acme", "url": "https://example.invalid/1",
        "description": "d" * 300, "matched": ["Python"], "gaps": [],
    })
    check(str(config.CV_OUTPUT_DIR / dirname) in prompt, "dossier de sortie dérivé de la config")
    check(prompt.count(str(config.CV_OUTPUT_DIR)) == 2,
          "prompt : dossier + manifest, tous deux calculés (aucun chemin figé)")
    check(config.OWNER_NAME in prompt, "propriétaire injecté depuis la config")
    # Invariant : plus aucune étape de mail dans le prompt (Paul lit tout dans
    # le hub). Ni adresse, ni demande d'envoi, ni champ email_sent attendu.
    check("@" not in prompt, "aucune adresse de mail dans le prompt")
    check("archive" not in prompt.lower(), "aucune étape d'archive dans le prompt")
    check("email_sent" not in prompt, "le manifeste demandé ne porte plus email_sent")
    check("mail" not in prompt.lower() or "aucun mail" in prompt.lower(),
          "le prompt ne demande pas d'envoi")


def test_health_redaction():
    """/api/health et les erreurs 500 ne doivent pas divulguer de chemin absolu."""
    import app as app_module

    payload = app_module.health()
    check(payload["ok"] is True, "health répond ok")
    check(all("/" not in str(v) for v in payload.values()), f"health sans chemin : {payload}")

    home = str(Path.home())
    check(app_module._redact(f"OSError: {home}/cv-tailor/x").endswith("~/cv-tailor/x"),
          "les chemins du foyer sont expurgés dans les erreurs")
    check(app_module._redact("ValueError: rien à voir") == "ValueError: rien à voir",
          "un message sans chemin reste intact")


def test_worker_command():
    """La commande de l'agent ne doit porter aucun `--source` (Hermes refuse les
    sources inconnues de son gateway et le run échoue au démarrage)."""
    import worker

    cmd = worker.build_command(Path("/tmp/run_1.txt"))
    check("--source" not in cmd, "aucun --source dans la commande de l'agent")
    check(cmd[1:3] == ["chat", "--query-file"], f"commande inattendue : {cmd[:3]}")
    for flag in ("--oneshot", "-s", "-t", "--in", "--run-budget"):
        check(flag in cmd, f"{flag} présent")
    check(cmd[cmd.index("-s") + 1] == "cv-application-tailor", "skill d'adaptation chargé")
    check(cmd[cmd.index("-t") + 1] == "web,file,terminal", "toolsets attendus")
    check(cmd[cmd.index("--in") + 1] == str(config.CV_TAILOR_DIR), "--in pointe sur cv-tailor")
    check(worker.HERMES == config.HERMES_BIN, "binaire hermes issu de la config")


def test_liveness_classify():
    import liveness

    for code in (200, 301):
        check(liveness.classify(code) == "ok", f"{code} → ok")
    for code in (404, 410):
        check(liveness.classify(code) == "gone", f"{code} → gone")
    for code in (403, 429, 503, 999, None):
        check(liveness.classify(code) == "unknown", f"{code} → unknown")
    check(liveness.probe("ftp://example.org/x") == ("unknown", None),
          "schéma non http : non vérifiable, sans réseau")


def test_liveness_closed_page():
    """Un 200 ne suffit pas : page de listing expirée, ou candidatures fermées."""
    import liveness

    LI = "https://fr.linkedin.com/jobs/view/data-scientist-at-x-123"
    LISTING = "https://fr.linkedin.com/jobs/x-emplois?trk=expired_jd_redirect"

    check(not liveness.is_expired_redirect(LI, LI), "toujours la fiche : pas expiré")
    check(liveness.is_expired_redirect(LI, LISTING),
          "redirection trk=expired_jd_redirect → expiré")
    check(liveness.is_expired_redirect(LI, "https://fr.linkedin.com/jobs/x-emplois"),
          "LinkedIn hors /jobs/view/ → expiré")
    check(not liveness.is_expired_redirect("https://www.adzuna.fr/details/1",
                                            "https://www.adzuna.fr/details/2"),
          "hors LinkedIn : la règle ne s'applique pas")

    check(liveness.classify_page(200, LI, LISTING, "") == "gone",
          "200 final sur une page de listing → gone")
    check(liveness.classify_page(200, LI, LI,
                                 '<figure class="closed-job closed-job__flavor">…') == "closed",
          "classe closed-job → candidatures closes")
    check(liveness.classify_page(
        200, LI, LI, "<figcaption>Les candidatures ne sont plus acceptées</figcaption>") == "closed",
          "« Les candidatures ne sont plus acceptées » → closed")
    check(liveness.classify_page(200, LI, LI, "No longer accepting applications") == "closed",
          "« no longer accepting applications » → closed")
    check(liveness.classify_page(200, LI, LI,
                                 "<div>Offres similaires : Data Analyst</div>") == "ok",
          "page vivante sans marqueur → ok")
    check(liveness.classify_page(429, LI, LI, "") == "unknown",
          "429 (LinkedIn limite) → non vérifiable, jamais gone")
    check(liveness.classify_page(410, LI, LI, "") == "gone", "410 → gone")


def test_url_state_migration():
    db.init_db()
    with db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(offers)")}
    check({"url_state", "url_checked_at"} <= cols, "colonnes url_state / url_checked_at créées")
    oid, _ = db.upsert_offer({"url": "https://job/alive-mig", "title": "ML Engineer"})
    with db.conn() as c:
        before = db.row_to_offer(c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone())
    check("url_state" in before and before["url_state"] is None
          and before["url_checked_at"] is None, "offre jamais vérifiée : états à None")
    db.set_url_state(oid, "gone")
    with db.conn() as c:
        after = db.row_to_offer(c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone())
    check(after["url_state"] == "gone" and after["url_checked_at"],
          "set_url_state écrit l'état et la date")
    try:
        db.set_url_state(oid, "peut-être")
        raise AssertionError("état d'URL inconnu accepté")
    except ValueError:
        check(True, "état d'URL inconnu refusé")


def test_check_alive_endpoint_offline():
    """Le bouton « Rafraîchir + vérifier », sans réseau : probe et ingest simulés."""
    import app as app_module
    import liveness

    dead, _ = db.upsert_offer({"url": "https://job/alive-dead", "title": "Data Analyst"})
    live, _ = db.upsert_offer({"url": "https://job/alive-live", "title": "Data Engineer"})
    kept, _ = db.upsert_offer({"url": "https://job/alive-kept", "title": "MLOps"})
    with db.conn() as c:
        db.set_status(c, kept, "shortlist")
    ids = [oid for oid, _ in db.offers_to_check()]
    check(dead in ids and live in ids and kept not in ids,
          "seules les offres nouvelles sont à vérifier")

    import threading

    seen = []
    gate = threading.Event()   # retient le lot tant que le second clic n'a pas eu lieu

    def fake_probe(url, timeout=8.0):
        gate.wait(5)
        seen.append(url)
        if url.endswith("alive-dead"):
            return "gone", 404
        if url.endswith("alive-live"):
            return "ok", 200
        raise RuntimeError("hôte injoignable")   # doit être avalé, pas arrêter le lot

    old_probe, old_ingest = liveness.probe, ingest.ingest_inbox
    liveness.probe = fake_probe
    ingest.ingest_inbox = lambda verbose=False: {"read": 2, "created": 2, "updated": 0,
                                                 "skipped": 0, "errors": 0}
    try:
        out = app_module.check_alive()
        check(out["ingested"] == 2 and out["running"] is True and out["queued"] >= 2,
              f"réponse de l'endpoint : {out}")
        first = liveness._thread
        again = liveness.enqueue_check()
        check(again["running"] is True and again["started_at"] == out["started_at"]
              and liveness._thread is first, "un second clic ne lance pas un second lot")
        gate.set()
        liveness._thread.join(timeout=10)
        check(not liveness._thread.is_alive(), "le lot se termine")
    finally:
        liveness.probe, ingest.ingest_inbox = old_probe, old_ingest

    with db.conn() as c:
        st = {r["id"]: r["url_state"] for r in c.execute(
            "SELECT id, url_state FROM offers WHERE id IN (?,?,?)", (dead, live, kept))}
    check(st[dead] == "gone", "404 → disparue")
    check(st[live] == "ok", "200 → répond")
    check(st[kept] is None, "offre non nouvelle : jamais vérifiée")
    check(all("alive-kept" not in u for u in seen), "aucune sonde sur une offre suivie")


def test_gone_offers_hidden():
    """Les annonces disparues ou closes sont masquées par défaut, jamais supprimées."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    new_view = js.split("function renderNew")[1].split("function renderPipeline")[0]

    check('id="toggleGone"' in html, "index.html porte le basculement « expirées »")
    check("hidden" in html.split('id="toggleGone"')[1].split(">")[0],
          "le basculement est masqué tant qu'il n'y a rien à révéler")
    check("showGone: false" in js, "état initial : les expirées sont masquées")
    check("function isExpired" in js and "o.url_state === 'gone'" in js
          and "o.url_state === 'closed'" in js,
          "isExpired couvre disparue ET candidatures closes")
    check("all.filter((o) => !isExpired(o))" in new_view,
          "la grille n'affiche pas les expirées par défaut")
    check("$('#toggleGone').onclick" in js, "le basculement est câblé")
    check("masquer les expirées" in new_view and "afficher les expirées (" in new_view,
          "libellés du basculement")
    check("expirée${plural(hidden)} masquée" in new_view,
          "le compteur annonce le nombre de masquées")
    check("Plus aucune annonce en ligne." in new_view,
          "tout expiré : la vue le dit au lieu de « rien de neuf »")
    check("DELETE" not in new_view and "/api/offers/" not in new_view,
          "masquer est un filtre d'affichage : aucun appel d'écriture")
    check("candidatures closes" in js and "non vérifiable" in js,
          "les puces distinguent disparue, close et non vérifiable")


if __name__ == "__main__":
    for fn in (test_schema, test_norm_variants, test_upsert_dedup, test_status_flow,
               test_inbox_ingest, test_tombstone, test_delete_offer, test_file_guard,
               test_pwa_icons, test_branding, test_theme_assets, test_detail_view_assets,
               test_doc_helpers, test_offer_payload, test_apply_schema_and_enqueue,
               test_linkhunt_result, test_linkhunt_prompt_and_command,
               test_apply_trigger_and_manual_override, test_config_paths,
               test_prompt_paths, test_health_redaction, test_worker_command,
               test_liveness_classify, test_liveness_closed_page, test_url_state_migration,
               test_check_alive_endpoint_offline, test_gone_offers_hidden):
        fn()
    print(f"\nALL TESTS PASSED ({PASS} vérifications)")
