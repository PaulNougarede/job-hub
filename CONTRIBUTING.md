# Contribuer à Job Hub

Le dépôt suit un flux **issue → branche → pull request**. `main` reste toujours
déployable : on ne commite jamais directement dessus.

## 1. Ouvrir une issue d'abord

Tout changement commence par une issue sur GitHub (templates « Bug » ou
« Fonctionnalité ») : c'est la fiche de suivi, elle porte le quoi et le pourquoi.

Une issue = un sujet. Elle reçoit au minimum :

- un label de **type** : `type:bug`, `type:feature`, `type:chore`, `type:docs` ;
- un label de **zone** : `area:back`, `area:front`, `area:worker`, `area:ingest`, `area:ci` ;
- une **priorité** : `prio:haute`, `prio:moyenne`, `prio:basse`.

## 2. Créer la branche depuis `main`

Convention de nommage : `<type>/<numéro>-<slug>`, par exemple
`fix/12-paths-configurables` ou `feat/15-sauvegarde-automatique`.

    scripts/issue-branch.sh 12        # lit le titre/labels de l'issue #12 et crée la branche

ou à la main :

    git switch main && git pull --ff-only
    git switch -c fix/12-paths-configurables

## 3. Coder et commiter

Commits courts, en anglais, au format Conventional Commits :

    feat: sauvegarde quotidienne de hub.db
    fix: chemins cv-tailor lus depuis l'environnement
    chore: ajoute requirements.txt
    test: couvre l'import d'une offre supprimée
    docs: documente le workflow d'issues

Un commit doit passer les tests :

    ./.venv/bin/python tests/test_hub.py

## 4. Ouvrir la PR

    gh pr create --fill --body "Closes #12"

- Le corps de la PR contient `Closes #<numéro>` : la fusion ferme l'issue.
- La CI (`.github/workflows/ci.yml`) doit être verte avant fusion.
- Fusion en **squash**, puis suppression de la branche.

    gh pr merge --squash --delete-branch

## 5. Règles non négociables

- Jamais de `data/hub.db`, `logs/`, `.venv/`, ni de secret dans un commit
  (voir `.gitignore`).
- Une PR ne traite qu'une issue ; un sujet hors périmètre devient une nouvelle
  issue.
- Si un changement révèle un bug, on ouvre l'issue correspondante au lieu de
  l'entasser dans la PR en cours.

## Ce que fait l'agent (Hermes)

Pour chaque demande de modification du hub, l'agent :

1. cherche une issue existante, sinon en crée une (titre clair + labels) ;
2. crée la branche `type/<num>-<slug>` depuis `main` à jour ;
3. code + lance les tests, commite sur la branche ;
4. ouvre la PR avec `Closes #<num>`, attend la CI, fusionne en squash ;
5. laisse la trace complète dans l'issue (PR liée, commit de fusion) pour que
   l'avancement soit visible dans l'onglet Issues.
