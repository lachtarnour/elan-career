# Gérer sa liste d’entreprises

La page **Entreprises** conserve les cibles, leur priorité et leur état `checked`
dans la base locale d’Élan. Une nouvelle installation commence avec une liste
vide. Ajoutez une entreprise depuis l’interface ou importez votre CSV privé.

## Fichiers privés

Conservez le CSV dans `data/company-targeting/companies.csv` et les éventuelles
décisions de classement dans `data/company-targeting/ranking.json`.
Le dossier `data/` est exclu de Git. Les profils, listes de prospection, notes de
recherche, bases et sauvegardes personnels ne doivent pas être inclus dans le
code, les tests ou les ressources distribuées avec l’application.

Le CSV utilise ces neuf en-têtes, sans colonne `checked` :

```text
Ordre,Entreprise,Priorité,Niveau de priorité,Catégorie,Type,Anglais estimé,Sélectivité estimée,Raison du classement
```

Utilisez un ordre entier positif unique et une priorité de 0 (non définie) à 5.
Les noms doivent être uniques après normalisation Unicode, casse et espaces.
Les champs contenant des virgules doivent être entourés de guillemets CSV.
L’import accepte UTF-8 avec ou sans BOM et valide tout le fichier avant écriture.

## Synchronisation

Depuis le dépôt :

```bash
make check-companies-sync
make sync-companies
```

Pour un CSV situé ailleurs :

```bash
make sync-companies COMPANIES_CSV="/chemin/liste.csv"
.venv/bin/elan sync-companies --csv "/chemin/liste.csv" --dry-run
.venv/bin/elan sync-companies --csv "/chemin/liste.csv"
```

La commande utilise la base configurée pour Élan (`ELAN_HOME`, `ELAN_ENV_FILE`
et `DATABASE_URL`), indiquée dans le rapport. Elle ajoute les nouvelles cibles et
actualise les fiches non cochées en les identifiant par leur nom normalisé.
**Les fiches `checked` sont conservées intégralement**, ainsi que les entreprises
absentes du CSV. Aucun enregistrement n’est supprimé ou recréé.

Avant une modification SQLite, une sauvegarde est créée dans `backups/` à côté
de la base. `--dry-run` affiche les différences sans les appliquer. Après une
synchronisation, revenez sur la page Entreprises pour recharger la liste ; aucun
rebuild n’est nécessaire. Le CSV reste privé et n’est pas copié dans le paquet.

## Reproduire un classement documenté

L’outil optionnel de classement utilise des décisions préparées dans un audit
JSON privé. Il ne consulte ni le Web ni la base de données :

```bash
.venv/bin/python tools/rank_companies.py --check
.venv/bin/python tools/rank_companies.py
make check-companies-sync
make sync-companies
```

`--csv` et `--audit` permettent de choisir d’autres fichiers. L’audit contient
`as_of` (date ISO) et `companies`, avec exactement les mêmes entreprises que le
CSV. Chaque entrée documente `company`, `action`, `constraint`, `fit`, `fit_basis`,
`work`, `maturity`, `evidence`, `reason` et `sources`.

Les actions `apply`, `qualify_offer`, `targeted_watch`, `broaden` et
`historical_or_unverified` correspondent aux priorités 1 à 5. À action égale, le
tri compare contrainte, adéquation, travail, structure, niveau de preuve, puis
nom. Les valeurs catégorielles admises sont définies dans
[`tools/rank_companies.py`](../tools/rank_companies.py).

Une action `apply` exige `evidence: "offer_read"` et les cinq indicateurs
`eligibility` à `true` : `open`, `france`, `permanent`, `experience_compatible`,
`no_required_phd`. Ces critères correspondent à la politique de ce classement ;
ils ne conviennent pas nécessairement à toutes les recherches. Une information
inconnue ne valide pas un critère. `--check` vérifie la reproductibilité du CSV,
pas l’ouverture actuelle des offres : les sources doivent être revérifiées.

Les tests du classement et de la synchronisation utilisent uniquement des
entreprises et justifications fictives, indépendantes des fichiers privés.
