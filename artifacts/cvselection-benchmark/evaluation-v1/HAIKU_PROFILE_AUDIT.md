# Crédibilité des sélections CVSelection de Claude Haiku 5.5

## Périmètre

Contrôle en lecture seule des 40 `structured_response` de `claude-haiku-5-5` dans `runs/dual-batch-v1/results.jsonl`, comparés au profil candidat transmis au Batch et au profil local actuel. Les dix fichiers JSON du profil ont les mêmes SHA-256 dans `dataset-v1/manifest.json` et dans les profils local et Élan actuels. Aucun CV final, résumé professionnel, lettre ou reviewer n'a été généré par ce benchmark : ce contrôle ne certifie donc pas un document final.

## Vérification des champs

| Champ | Résultat |
|---|---|
| Titre | 40/40 restent dans la famille « Data Scientist », identité professionnelle documentée ; les variantes Applied AI, NLP, Computer Vision et Applied Analytics sont cohérentes avec les expériences/projets, sans titre Lead, Architect, GNC ou HPC inventé. |
| Projets | 120/120 références pointent vers un projet du profil ; exactement trois distincts par offre ; aucun projet `disabled` sélectionné ; au plus un `specialized` par offre, avec `complementary_value` renseigné. |
| Compétences | 478 occurrences, 52 compétences distinctes ; toutes figurent dans `skills.json` et dans la bonne catégorie. L'inscription au catalogue atteste une déclaration du candidat, pas une réalisation documentée pour chacune. |
| Certifications | 30 sélections sur 18 offres ; chaque ID correspond à l'un des deux certificats du profil. |
| Raisons de projet | Lecture des 120 raisons : aucune attribution factuelle précise étrangère au projet source repérée. Les liens indirects sont généralement qualifiés de transférables. Cette vérification humaine ne prouve pas l'exhaustivité des faits. |

Contrôle complémentaire de nomenclature : les 120 IDs de projet, 30 IDs de certificat, 478 libellés de compétence, 147 IDs de blocs de compétences et 40 `skills_profile_id` correspondent exactement aux identifiants/libellés du profil. Tous les IDs de `skills_order` existent aussi dans le profil. Sur trois offres (11845, 10592, 11846), `skills_order` mentionne des catégories valides sans bloc de compétences sélectionné ; le moteur de rendu ignore ces catégories vides. Les noms de projets et certificats affichés sont résolus depuis le profil à partir des IDs, et les compétences affichées sont canonisées avec le catalogue du profil.

## Réserves concrètes

1. **Compétences RL déclarées mais projet source désactivé.** Les offres 10803, 10773 et 7578 affichent `Q-learning` et/ou `SARSA` ; 10773 affiche aussi `Policy gradients`. `Q-learning` et `SARSA` sont bien dans `skills.json`, mais leur réalisation précise apparaît dans `proj_rl_gym`, marqué `disabled` et jamais sélectionné. `Policy gradients` figure au catalogue sans réalisation identifiée dans les expériences ou projets. Cela respecte actuellement la règle applicative qui interdit le *projet* `disabled` tout en autorisant les compétences déclarées ; cela peut néanmoins donner un signal RL plus fort que les projets affichés dans le CV.
2. **Entraînement distribué déclaré sans réalisation de projet.** Les offres 8110, 9017 et 9567 affichent `Distributed training (PyTorch DDP)`. Cette compétence est dans `skills.json`, mais aucune expérience ou projet du profil ne documente l'usage de DDP. Haiku le signale explicitement en 9567 ; les autres sélections ne doivent pas être lues comme une preuve de systèmes multi-GPU livrés.
3. **Exactitude interne d'un avertissement.** Pour 3940, Haiku écrit que l'expérience de service OpenAI n'est pas documentée. La rubrique de mots-clés du projet Élan contient pourtant `OpenAI embeddings`. Cela documente cet usage précis, sans établir une expertise générale des services OpenAI. Cet avertissement n'est pas une phrase destinée au CV, mais sa formulation est trop absolue.
4. **Pertinence métier plutôt que crédibilité factuelle.** L'évaluation principale classe trois sorties Haiku avec défaut majeur de sélection : 8110 (projets moins proches de l'inférence), 10803 (compétences et certificat périphériques) et 7578 (compétences RL/séries temporelles qui diluent le GNC). Aucun de ces défauts n'est une invention pure de projet ou de certificat.

## Conclusion

Les choix Haiku sont **présents dans le profil** et les réalisations attribuées aux projets sont globalement bornées par ce profil. Dire que « toutes les informations d'un CV final sont prouvées » serait excessif : certaines compétences sont seulement déclarées, et les futurs textes libres du CV n'ont pas été produits ni contrôlés dans ce benchmark. Si `disabled` doit aussi masquer les compétences issues uniquement d'un projet désactivé, il faut en faire une règle distincte ; le comportement actuel ne l'implique pas.
