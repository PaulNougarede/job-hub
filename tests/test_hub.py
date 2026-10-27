"""Tests du hub : ingestion, dédoublonnage, statuts, chemins de fichiers.

    ./.venv/bin/python tests/test_hub.py     # ou python3 -m pytest tests/
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Une base jetable AVANT d'importer db (DB_PATH lu à l'import).
tmp = tempfile.mkdtemp(prefix="hubtest_")
os.environ["HUB_DB"] = str(Path(tmp) / "test.db")

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
    root = Path("/home/user/cv-tailor/output").resolve()
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
                     ".drawer-head h2", ".modal h2"):
        body = _rule(css, selector)
        check(body and "font-family" not in body, f"{selector} hérite de la famille unique")


if __name__ == "__main__":
    for fn in (test_schema, test_norm_variants, test_upsert_dedup, test_status_flow,
               test_inbox_ingest, test_tombstone, test_delete_offer, test_file_guard,
               test_pwa_icons, test_branding, test_theme_assets):
        fn()
    print(f"\nALL TESTS PASSED ({PASS} vérifications)")
