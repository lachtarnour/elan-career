# Sélection des projets

Le profil déclare une politique stable par projet dans `projects.json` :

```json
"selection_policy": {
  "tier": "specialized",
  "rationale": "Projet pédagogique utile pour démontrer une tâche précise.",
  "activation_conditions": ["Extraction d'entités nommées explicitement demandée"]
}
```

- `primary` : projet prioritaire par pertinence directe ou compétences transférables.
- `specialized` : complément activable pour un besoin explicite de l'offre ; au plus un.
- `disabled` : conservé dans le profil et l'historique, exclu des nouvelles sélections.

Les anciens profils sans politique restent compatibles et utilisent `primary`.
La politique ne modifie ni les descriptions ni les preuves du projet. Elle est
personnelle et reste dans le profil privé, sans identifiants codés dans le moteur.

## Parcours de génération

L'appel de personnalisation existant choisit exactement trois projets distincts :
le minimum est de trois et le maximum reste de trois. Il retient d'abord les
meilleures correspondances directes, puis les projets prioritaires démontrant les
compétences transférables les plus utiles pour compléter la sélection. Il écrit
ensuite un résumé fondé sur cette sélection. Chaque choix comporte une
justification et un extrait exact des tâches, compétences demandées ou du texte de
l'offre, y compris lorsque le lien repose sur des compétences transférables. La
justification doit décrire ce lien honnêtement, sans inventer une expérience du
domaine demandé.

Un projet spécialisé doit apporter une contribution distincte et répondre à un
besoin explicite ; la simple présence de Python ou PyTorch ne suffit pas à
l'activer. Le minimum de trois n'autorise ni l'assouplissement de cette règle ni
la sélection de projets désactivés. Si le catalogue ne permet pas trois choix
admissibles, la génération s'arrête en signalant la limite.

Le modèle juge la pertinence et la complémentarité. Le code contrôle les
identifiants, doublons, catégories, minimum et maximum de trois, présence des justifications et
existence des extraits cités. Ce dernier contrôle ne prouve pas la pertinence
sémantique d'une justification.

La recherche de l'extrait applique la même normalisation typographique à la
citation et aux sources : Unicode, casse, espaces et retours à la ligne,
apostrophes droites/courbes et traits d'union typographiques. Une coupure telle
que `d’` suivie d'un retour à la ligne puis `APIs` correspond donc à `d'APIs`.
Les textes d'origine restent inchangés. Les mots, leur ordre, les accents,
les chiffres et la ponctuation significative restent nécessaires ; aucune
recherche approximative ni validation supplémentaire par LLM n'est ajoutée.
Cette normalisation ne sert pas à localiser les passages à remplacer lors de
la relecture, qui conserve ses contrôles exacts.

Une sélection invalide déclenche une seule nouvelle tentative de personnalisation
complète. Si elle échoue aussi, la génération s'arrête avant la lettre. Aucun
correctif automatique ne retire ou n'ajoute un projet sous un résumé existant.
Le parcours normal n'ajoute aucun appel LLM.

La lettre reçoit le CV final et les preuves des seuls projets retenus. HTML,
PDF et DOCX suivent les identifiants dans le même ordre et n'ajoutent jamais de
projet lors du rendu. Les scores d'embeddings restent des diagnostics, sans imposer la
sélection.

Les raisons sont conservées dans `project_selection_reasons` du snapshot CV pour
audit, mais exclues des documents et des sources de relecture. Les anciens
snapshots sans raisons, y compris avec moins de trois ou plus de trois projets,
restent lisibles. Le minimum s'applique aux nouvelles générations ; les dossiers
existants nécessitent une régénération pour le respecter.
