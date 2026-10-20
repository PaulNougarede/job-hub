# Job Hub — tableau de bord des candidatures

Hub personnel : les offres trouvées par le cron `job-hunter` arrivent ici, chaque
offre est une carte avec un statut, et un bouton lance l'adaptation du CV + de la
lettre par le skill `cv-application-tailor`.

Accès : **http://hp-server-1.taile60ff2.ts.net:8765** (réseau Tailscale
uniquement — le service écoute sur l'IP Tailscale, jamais sur le LAN ni sur
Internet, et n'ouvre aucun port public).

## Architecture

```
cron job-hunter (09:00)
  └─ adzuna_fetch.py + scoring LLM
       └─ append  /home/paul/job-hunter/inbox.jsonl   (1 ligne JSON par offre)
              └─ hub : ingest.py (surveille le fichier toutes les 20 s)
                    └─ SQLite  data/hub.db  →  board web

bouton « Adapter CV + LM »
  └─ POST /api/offers/<id>/tailor
       └─ runs (table)  état queued → running → done/failed
            └─ hermes chat --query-file … -s cv-application-tailor
                 └─ /home/paul/cv-tailor/output/<dossier>/manifest.json
                      └─ fichiers liés dans la carte + mail d'archive
```

Un seul run d'adaptation à la fois (les scripts de cv-tailor partagent `build/`).

## Fichiers

| fichier | rôle |
|---|---|
| `app.py` | API FastAPI + service des fichiers produits |
| `db.py` | schéma SQLite (`offers`, `runs`, `events`) |
| `ingest.py` | lecture de `inbox.jsonl`, import de l'historique `cv-tailor/output` |
| `worker.py` | file d'attente + lancement de l'agent d'adaptation |
| `static/` | board (HTML/CSS/JS sans build, pas de Node) |
| `data/hub.db` | la base — à sauvegarder |
| `logs/run_<id>.log` | sortie complète de chaque run d'adaptation |

## Exploitation

    systemctl --user status job-hub      # état
    systemctl --user restart job-hub     # après modif du code
    journalctl --user -u job-hub -f      # logs serveur
    ./.venv/bin/python tests/test_hub.py # tests (base jetable, ne touche pas hub.db)

Service utilisateur (`~/.config/systemd/user/job-hub.service`), linger activé :
il redémarre au boot sans session ouverte. `./start.sh` en direct reste possible.

**Ne pas redémarrer pendant un run d'adaptation** : le run tourne dans le même
cgroup, un restart tue l'agent au milieu du travail (le run est alors marqué
`failed`, aucun fichier n'est produit). Vérifier avec `GET /api/board` qu'aucun
run n'est `running` avant un restart.

## Les deux vues

- **#/nouvelles** — les offres fraîches, en grille, triées par score. Chaque carte
  porte trois actions : `⚡ Adapter CV + LM`, `✓` (la faire passer dans le suivi),
  `✕` (la supprimer — elle ne revient pas par le cron). Les job boards (HelloWork,
  Welcome to the Jungle, LinkedIn) sont en haut à droite : `data/sites.json`
  contient leurs URLs de recherche, éditable à la main.
- **#/candidatures** — le pipeline en 5 colonnes (À postuler → Postulé →
  Entretien → Refusé → Offre), glisser-déposer pour changer d'étape. La colonne
  Refusé ne contient que les candidatures qui n'ont pas abouti : une offre qu'on
  ne veut pas se supprime depuis #/nouvelles, elle ne passe jamais en Refusé
  toute seule.

Une offre « gardée » quitte la page Nouvelles : c'est le geste qui dit « je la
traite », pas une alerte à ne pas perdre.

## Statuts

`new` → `shortlist` (à postuler) → `applied` → `interview` → `rejected` / `offer`.
Glisser une carte d'une colonne à l'autre change le statut. Un run réussi passe
automatiquement la carte en `shortlist` : c'est Paul qui confirme l'envoi.

## Ajouter une offre à la main

Bouton « + Offre » : intitulé obligatoire, le texte de l'offre est ce qui sert à
la recherche entreprise et à l'adaptation — colle-le, l'URL seule ne suffit pas.

Une offre supprimée laisse une pierre tombale (`dismissed`) : le cron ne la
réimporte plus, sinon elle réapparaîtrait à chaque redémarrage. La rajouter à la
main lève la pierre.

## Développer

    git clone https://github.com/PaulNougarede/job-hub.git && cd job-hub
    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
    HUB_DB=data/hub.db ./.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
    ./.venv/bin/python tests/test_hub.py     # tests (base jetable, ne touche pas hub.db)

Le code suit un flux **issue → branche → PR** : voir `CONTRIBUTING.md`.
`main` est protégée par la CI (`.github/workflows/ci.yml`) ; chaque modification
part d'une issue et arrive par une pull request qui porte `Closes #<numéro>`.

    gh issue list                    # la file de travail
    scripts/issue-branch.sh 12       # branche fix/12-… ou feat/12-… depuis main

## Sauvegarde

    sqlite3 data/hub.db ".backup 'data/hub-$(date +%F).db'"

`data/` et `logs/` sont les seules choses à conserver.
