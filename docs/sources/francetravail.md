# France Travail

Source id : `francetravail`

France Travail interroge l'API officielle Offres d'emploi v2. C'est la source
la plus structurée pour les contrats, l'expérience, le temps de travail et le
contexte ROME.

## Stratégie rapide

| Phase | Stratégie |
| --- | --- |
| Collecte | Recherche API OAuth avec filtres structurables. |
| Filtre local | Priorité aux champs API fiables avant le texte libre. |
| Analyse LLM | Envoie contacts, URLs et faits FT compacts en metadata. |
| Limite | Certaines entreprises masquent leur nom ou mettent l'URL dans des champs texte. |

## Configuration

| Variable | Rôle |
| --- | --- |
| `FRANCETRAVAIL_CLIENT_ID` | Client OAuth partenaire. |
| `FRANCETRAVAIL_CLIENT_SECRET` | Secret OAuth partenaire. |
| `FRANCETRAVAIL_SCOPE` | Scope API, par défaut `api_offresdemploiv2 o2dsoffre`. |

## Appels

| Appel | Auth | Usage |
| --- | --- | --- |
| `POST entreprise.francetravail.fr/.../access_token` | Client credentials | Récupère un token cache en mémoire. |
| `GET api.francetravail.io/.../offres/search` | Bearer token | Recherche paginée par `range=start-end`. |

Paramètres importants :

| Paramètre | Origine Élan |
| --- | --- |
| `motsCles` | `query`, avec fallback location en texte si non résolue. |
| `commune`, `departement`, `region` | Résolution de `location` via référentiel geo France. |
| `typeContrat` | Option explicite si fournie. |
| `minCreationDate/maxCreationDate` | Conversion de `date_posted` (`today`, `3days`, `week`, `month`). |

## Limite par requête

Le champ « Résultats par source » s'applique **à chaque recherche par intitulé**
pour France Travail, comme pour LinkedIn/Apify. Toutes les recherches prévues
sont parcourues, y compris les variantes d'intitulés et les recherches doctorales,
sans plafond global sur leur cumul. Avec une limite de 100 et quatre recherches
complètes, jusqu'à 400 offres sont collectées avant déduplication et filtrage local.

Chaque recherche est paginée par lots de 50 maximum : une limite de 100 demande
normalement deux pages de 50, une limite de 75 demande une page de 50 puis 25.
Les réponses déjà reçues sont traitées intégralement, même si l'API dépasse la
taille demandée. Une erreur sur une page conserve les pages précédentes et les
autres recherches continuent, avec un avertissement. Les erreurs de configuration
ou d'autorisation arrêtent les requêtes restantes sans supprimer les offres reçues.

## Mapping

| Sortie | Champs France Travail |
| --- | --- |
| `RawJob.title` | `intitule`. |
| `RawJob.company` | `entreprise.nom`, sinon valeur neutre. |
| `RawJob.location` | `lieuTravail.libelle`. |
| `RawJob.contract_type` | `typeContratLibelle` ou `natureContrat`. |
| `RawJob.description` | Présentation entreprise + expérience + description + compétences + qualités. |
| `RawJob.experience` | `_smartapply_experience` extrait de `experienceExige/Libelle/Commentaire`. |
| `RawJob.application_url` | `origineOffre.urlOrigine`. |
| `RawJob.published_date` | `dateCreation`. |
| `source_data` | Payload offre brut, enrichi avec `_smartapply_experience`. |

### Identité durable d'une offre

`source_data.id` est l'identité canonique France Travail. La collecte et la
persistance la comparent en plus de `external_id`, afin qu'un ancien hash ou une
URL de candidature modifiée ne crée ni seconde offre ni second dossier. Cette
règle reste limitée à la source `francetravail` : le même texte provenant d'une
autre source suit le parcours normal de rapprochement.

Pour une base historique, `tools/repair_francetravail_identity.py` affiche un
plan en lecture seule par défaut. Exécuter d'abord ce plan sur une sauvegarde ;
`--apply` effectue ensuite une transaction, conserve les alias et dossiers, puis
vérifie les clés étrangères et l'intégrité SQLite.

## Stratégie filtre

Adapter : `build_francetravail_filter_facts`.

| Fact normalisé | Source | Effet |
| --- | --- | --- |
| `experience_requirement/min_years` | `_smartapply_experience`, fallback libellés FT | Rejet si expérience obligatoire trop haute ; ignore les années si débutant accepté/souhaité. |
| `structured_contract_type` | `typeContrat`, `typeContratLibelle`, `natureContrat` | Rejet stage, alternance, intérim, freelance, non salarié. |
| `structured_location` | `lieuTravail.libelle` | Rejet des lieux hors zone acceptée. |
| `structured_alternance` | `alternance` | Signal direct pour exclure l'alternance. |
| `structured_work_time` | `dureeTravailLibelleConverti`, fallback `dureeTravailLibelle` | Exclut le temps partiel quand détecté. |
| `structured_rome_*` | `romeCode`, `romeLibelle`, `appellationlibelle` | Contexte de diagnostic et audit. |

Garde-fou : les durées d'expérience supérieures à 11 ans sont marquées non
fiables et ne deviennent pas un hard reject automatique.

## Stratégie analyse

Builder : `build_francetravail_source_metadata`.

| Section | Contenu |
| --- | --- |
| `CONTACT_AND_APPLICATION_METADATA` | ID brut, entreprise, origine offre, contact, emails, URLs classées. |
| `STRUCTURED_JOB_FACTS` | Expérience, contrat, durée, salaire, secteur, effectif, déplacement, formations, langues, compétences, qualités, contexte. |

Objectif : aider le LLM à séparer l'offre, l'entreprise, les contacts et les
contraintes structurées sans lui envoyer tout le JSON France Travail.
