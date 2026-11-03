"""Fichiers produits par un run de cv-tailor : nommage lisible et garde-fous.

Aucun accès SQLite ici : `app.py` résout le run, ce module décide quel fichier
est servable, sous quel nom, et lit la recherche entreprise. Tout passe par un
chemin résolu et vérifié — un path venu de la base ne doit jamais sortir de
`output/`.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# kind -> (libellé affiché, extension, préfixe du nom de fichier)
DOC_KINDS = {
    "cv_pdf": ("CV", "pdf", "CV"),
    "lm_pdf": ("Lettre de motivation", "pdf", "Lettre"),
    "cv_docx": ("CV (modifiable)", "docx", "CV"),
    "lm_docx": ("Lettre (modifiable)", "docx", "Lettre"),
}

RESEARCH_NAME = "company_research.md"
RESEARCH_MAX = 200_000  # au-delà, on tronque : le hub ne rend pas un roman


def slug(value: str | None, maxlen: int = 40) -> str:
    """ASCII, sans accent ni espace : un nom de fichier qui survit à un Mac."""
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return (s[:maxlen].rstrip("-")) or "offre"


def doc_filename(kind: str, company: str | None, day: str | None) -> str:
    """Nom téléchargé, parlant dans ~/Downloads : `CV_Groupe-VYV_2026-10-30.pdf`."""
    if kind not in DOC_KINDS:
        raise ValueError(f"type de document inconnu: {kind}")
    _, ext, short = DOC_KINDS[kind]
    parts = [short, slug(company, 30), (day or "").strip()[:10]]
    return "_".join(p for p in parts if p) + "." + ext


def resolve_doc(out_dir: str | Path | None, files: dict | None,
                kind: str, root: str | Path) -> Path | None:
    """Chemin absolu du document demandé, ou None s'il n'existe pas / sort de root."""
    name = (files or {}).get(kind)
    if not out_dir or not name or kind not in DOC_KINDS:
        return None
    root_p = Path(root).resolve()
    target = (Path(out_dir) / str(name)).resolve()
    if not target.is_relative_to(root_p) or not target.is_file():
        return None
    return target


def documents(out_dir: str | Path | None, files: dict | None,
              company: str | None, day: str | None,
              root: str | Path) -> list[dict]:
    """Liste prête à afficher : un document par fichier réellement présent."""
    out = []
    for kind in DOC_KINDS:
        path = resolve_doc(out_dir, files, kind, root)
        if not path:
            continue
        out.append({
            "kind": kind,
            "label": DOC_KINDS[kind][0],
            "ext": DOC_KINDS[kind][1],
            "filename": doc_filename(kind, company, day),
            "size": path.stat().st_size,
        })
    return out


def read_research(out_dir: str | Path | None, manifest: dict | None,
                  root: str | Path, limit: int = RESEARCH_MAX) -> dict | None:
    """Recherche entreprise du run.

    Le fichier est la source ; le résumé du manifest n'est qu'un repli pour les
    candidatures produites avant `company_research.md`. `kind` le dit à l'UI,
    qui peut alors afficher « résumé » plutôt que de faire croire à un dossier
    complet.
    """
    manifest = manifest or {}
    rel = manifest.get("company_research") or RESEARCH_NAME
    if out_dir:
        root_p = Path(root).resolve()
        target = (Path(out_dir) / str(rel)).resolve()
        if target.is_relative_to(root_p) and target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            return {
                "kind": "file",
                "name": target.name,
                "markdown": text[:limit],
                "truncated": len(text) > limit,
            }
    summary = (manifest.get("company_summary") or "").strip()
    if summary:
        return {"kind": "summary", "name": None, "markdown": summary, "truncated": False}
    return None
