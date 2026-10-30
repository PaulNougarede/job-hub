# Job Hub — tableau de bord de recherche d'emploi

![CI](https://github.com/PaulNougarede/job-hub/actions/workflows/ci.yml/badge.svg)

Un hub personnel pour piloter une recherche d'emploi de bout en bout : une veille
quotidienne dépose les offres qui correspondent à mon profil, chaque offre devient
une carte sur un board, et un bouton lance l'adaptation du CV et de la lettre de
motivation par un agent — avec vérification du résultat produit.

FastAPI + SQLite + HTML/CSS/JS sans étape de build : un seul processus, une seule
base, aucune dépendance front.

## Les deux vues

- **Nouvelles** — les offres fraîches, en grille, triées par score de matching.
  Trois actions par carte : `⚡ Adapter CV + LM`, `✓` (faire entrer l'offre dans
  le suivi), `✕` (la supprimer). Les job boards (HelloWork, Welcome to the
  Jungle, LinkedIn) ont leurs URLs de recherche dans `data/sites.json`, éditable
  à la main.
- **Candidatures** — le pipeline en cinq colonnes : À postuler → Postulé →
  Entretien → Refusé → Offre, en glisser-déposer.

Une offre supprimée laisse une **pierre tombale** (table `dismissed`) : sans ça,
la veille la réimporterait à chaque passage. Supprimer n'est pas refuser — la
colonne Refusé ne contient que les candidatures qui n'ont pas abouti.

Ajouter une offre à la main (bouton « + Offre ») demande son texte, pas seulement
son URL : c'est ce texte qui sert à la fois à la recherche sur l'entreprise et à
l'adaptation.

## Architecture

```
veille quotidienne (cron, dépôt séparé)
  └─ écrit une ligne JSON par offre retenue
       └─ $HOME/job-hunter/inbox.jsonl
            └─ ingest.py (surveille le fichier toutes les 20 s)
                 └─ SQLite data/hub.db  →  board web

bouton « Adapter CV + LM »
  └─ POST /api/offers/<id>/tailor
       └─ table runs : queued → running → done | failed
            └─ worker.py : agent (Hermes) + skill d'adaptation
                 └─ $HOME/cv-tailor/output/<date>_<entreprise>_<poste>/manifest.json
                      └─ fichiers liés dans la carte
```

L'adaptation du CV vit dans un autre dépôt (`cv-tailor`) : ce hub ne fait que
transporter l'offre et vérifier ce que l'agent a réellement produit. Un seul run
à la fois, parce que les scripts d'adaptation partagent un dossier de travail.

| fichier | rôle |
|---|---|
| `app.py` | API FastAPI + service des fichiers produits |
| `config.py` | chemins et réglages, tous surchargeables par l'environnement |
| `db.py` | schéma SQLite (`offers`, `runs`, `events`, `dismissed`) |
| `ingest.py` | lecture de l'inbox, import de l'historique, dédoublonnage |
| `worker.py` | file d'attente et lancement de l'agent |
| `static/` | le board (HTML/CSS/JS, sans build) |
| `data/hub.db` | la base — à sauvegarder |
| `logs/run_<id>.log` | sortie complète de chaque run |

## Démarrage

```bash
git clone https://github.com/PaulNougarede/job-hub.git && cd job-hub
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

cp .env.example .env          # optionnel : sans .env, tout a une valeur par défaut
./start.sh                    # écoute sur l'IP Tailscale, sinon 127.0.0.1:8765

./.venv/bin/python tests/test_hub.py     # tests (base jetable, ne touche pas hub.db)
```

## Configuration

Toutes les valeurs sont optionnelles et calculées au chargement par `config.py`
(racine du dépôt, `$HOME`) ; `.env.example` les documente toutes.

| variable | défaut | rôle |
|---|---|---|
| `HUB_DB` | `data/hub.db` | base SQLite |
| `HUB_LOG_DIR` | `logs/` | sortie de chaque run |
| `HUB_PROMPT_DIR` | `data/prompts/` | prompt exact envoyé à l'agent |
| `CV_TAILOR_DIR` | `$HOME/cv-tailor` | dépôt d'adaptation CV |
| `CV_OUTPUT_DIR` | `$CV_TAILOR_DIR/output` | fichiers servis par `/files/` |
| `HUB_INBOX` | `$HOME/job-hunter/inbox.jsonl` | entrée de la veille |
| `HUB_SENT_LOG` | `sent.jsonl` à côté de l'inbox | variante d'entrée |
| `HUB_HERMES_BIN` | `hermes` dans le `PATH` | agent lancé par le worker |
| `HUB_OWNER_NAME` | `Paul Nougarède` | propriétaire annoncé dans le prompt |
| `HUB_ARCHIVE_EMAIL` | vide | adresse d'archive des candidatures ; vide = pas de mail |
| `HUB_RUN_BUDGET` | `2700` | budget d'un run, en secondes |
| `HUB_HOST` / `HUB_PORT` | IP Tailscale / `8765` | écoute du service |

## Modèle de sécurité

Ce hub est **mono-utilisateur et sans authentification applicative** : c'est un
choix, pas un oubli. La frontière de sécurité est le réseau privé, pas
l'application.

- Le service n'écoute que sur l'interface Tailscale (ou `127.0.0.1`), jamais sur
  le LAN ni sur un port public : `0.0.0.0` n'est pas une option, et l'appartenance
  au tailnet fait office d'authentification des appareils. Exposer le service
  au-delà suppose d'ajouter d'abord un jeton partagé.
- **Aucune donnée personnelle dans Git** : `data/hub.db` (candidatures, notes),
  `data/prompts/` (offre + prompt), `logs/`, les CV générés et `.env` sont
  gitignorés. Ce dépôt ne contient que du code.
- **Aucun secret dans le dépôt** : tout passe par l'environnement, y compris
  l'adresse d'archive des candidatures, vide par défaut.
- **Le texte d'une offre est une entrée non fiable.** Il vient d'un site d'emploi
  et finit dans le prompt d'un agent qui a accès au disque, au web et au
  terminal : c'est le vrai risque du projet (injection de prompt). Mitigations :
  un run à la fois, budget de temps, sortie confinée au dossier d'adaptation,
  manifest vérifié après coup, et rien n'est envoyé automatiquement — c'est moi
  qui postule.
- **Service de fichiers borné** : `GET /files/{path}` résout le chemin puis
  refuse tout ce qui sort de `CV_OUTPUT_DIR` (test de régression dédié).

Un problème de sécurité se signale par une issue, en privé si la divulgation
publique pose problème.

## Cycle de vie d'une offre

`new` → `shortlist` (à postuler) → `applied` → `interview` → `rejected` / `offer`.

Un run d'adaptation réussi fait passer la carte en `shortlist` : l'agent produit
des fichiers, il ne postule pas. Le score, les compétences matchées et les
compétences manquantes viennent de la veille et restent affichés sur la carte.

## Développement

Le dépôt suit un flux **issue → branche → PR** : rien n'arrive sur `main` sans
une issue et une pull request qui la ferme (`Closes #<numéro>`).

```bash
gh issue create --title "..." --label type:feature --label area:back --label prio:moyenne
scripts/issue-branch.sh 12        # branche feat/12-… (fix/ pour type:bug, chore/ sinon)
./.venv/bin/python tests/test_hub.py
git commit -m "feat: ..."         # Conventional Commits, en anglais
gh pr create --fill --body "Closes #12"
gh pr merge --squash --delete-branch
```

Labels : `type:bug|feature|chore|docs`, `area:back|front|worker|ingest|ci`,
`prio:haute|moyenne|basse`.

La CI (`.github/workflows/ci.yml`) lance les tests à chaque push sur `main` et à
chaque pull request, avec un jeton en lecture seule et aucun secret : une PR
externe ne peut donc rien exécuter de sensible.

Les tests (`tests/test_hub.py`) sont un lanceur maison sans dépendance : base
jetable, ils ne touchent jamais `data/hub.db`.

## Exploitation

```bash
systemctl --user status job-hub       # état
systemctl --user restart job-hub      # après modif de app.py / db.py / worker.py
journalctl --user -u job-hub -f       # logs serveur
```

Unité systemd utilisateur minimale (linger activé, pour redémarrer au boot sans
session ouverte) :

```ini
[Service]
WorkingDirectory=%h/job-hub
ExecStart=%h/job-hub/start.sh
Restart=always
```

`static/` est servi depuis le disque : une modification de `index.html`,
`style.css` ou `app.js` ne demande aucun redémarrage.

**Ne pas redémarrer pendant un run d'adaptation** : le run tourne dans le même
cgroup, un restart tue l'agent au milieu du travail (run marqué `failed`, aucun
fichier produit). Vérifier via `GET /api/board` qu'aucun run n'est `running`.

## Sauvegarde

```bash
sqlite3 data/hub.db ".backup 'data/hub-$(date +%F).db'"

# sans le CLI sqlite3 :
./.venv/bin/python -c "import sqlite3, datetime as d; \
  sqlite3.connect('data/hub.db').backup(sqlite3.connect(f'data/hub-{d.date.today()}.db'))"
```

`data/` et `logs/` sont les seules choses à conserver.

## Limites connues

- Pas d'authentification applicative (assumée : le service vit sur un tailnet).
- La sauvegarde de la base est manuelle.
- Un redémarrage pendant un run tue le run.
- Les tests utilisent un lanceur maison plutôt que pytest.

## Licence

MIT — voir [LICENSE](LICENSE).

## Ce que ce projet m'a servi à travailler

Une petite API FastAPI autour d'une base SQLite en WAL, une file de travail à un
seul consommateur, l'intégration d'un agent LLM avec vérification de ses sorties,
et la partie ingrate : borner les chemins servis, séparer les données personnelles
du code, et rendre la configuration portable.
