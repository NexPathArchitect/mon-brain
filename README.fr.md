# mon_brain — outil local Ollama d'organisation / résumé-traduction / révision

**Langues / Languages / 语言**：[Français](README.fr.md) · [English](README.en.md) · [中文](README.md) · [📖 在线文档 / Docs / Documentation](https://nexpatharchitect.github.io/mon-brain/)

**← [返回中文版本 / Retour au chinois](README.md)**

Outil en ligne de commande tournant sur votre Ollama local : lire la source → découper automatiquement les textes longs et fusionner les résumés → appliquer le prompt du mode → appeler le modèle local → exporter le Markdown dans `sortie/`.
Les données ne quittent pas la machine ; l'outil ne dépend pas du projet d'atelier du professeur, il réutilise seulement la même façon d'appeler Ollama.

## Structure du dossier

```
mon_brain\
├── mon_brain.py        # script CLI principal (commentaires en anglais pour les relecteurs)
├── app_web.py          # interface web locale (serveur stdlib, même noyau réutilisé)
├── corrections.txt     # table statique de corrections de transcription (faute -> mot correct, modifiable)
├── prompts\            # prompts par mode (changez les exigences ici, pas dans le code)
│   ├── organize.md
│   ├── translate_summary.md
│   └── recap.md
├── sources\            # dossier de transit ; _upload\ reçoit les envois web temporaires (généré auto)
├── sessions\           # historique de session de révision multi-tours (JSON, généré automatiquement)
├── sortie\             # sortie Markdown (nouveau fichier à chaque exécution ; les originaux ne sont jamais écrasés)
└── .venv\              # environnement virtuel isolé (requests, pypdf installés)
```

## Environnement (déjà prêt)

- Interpréteur : `.venv\Scripts\python.exe` (isolé ; ne touche pas au Python système ni au projet du professeur)
- Dépendances : `requests`, `pypdf` (installés)
- Prérequis : Ollama est démarré et les modèles nécessaires sont téléchargés

## Modes et modèles par défaut

| `--mode` | Rôle | Modèle par défaut | Chaîne de réflexion |
|---|---|---|---|
| `organize` | Extraction objective et organisation structurée | `qwen2.5:latest` | retirée |
| `translate_summary` | Résumé + organisation (principalement transcriptions FR/EN) | `gemma3:12b` | retirée |
| `recap` | Révision approfondie + évaluation de la question + auto-contrôle | `qwen3:14b` | **conservée** |

Le modèle peut être surchargé avec `--model`.

## Configuration GPU / calcul (mesurée sur ce portable, 2026-11-23)

Ollama est réglé pour une RTX 5070 Ti Laptop (12 Go de VRAM) via des variables d'environnement utilisateur ; relancez `configure_ollama.ps1` après une réinstallation.

| Variable | Valeur | Effet |
|---|---|---|
| `OLLAMA_FLASH_ATTENTION` | `1` | Flash Attention : plus rapide, KV cache plus léger |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | mémoire du KV cache divisée par deux, perte de qualité négligeable |
| `OLLAMA_NUM_PARALLEL` | `1` | exécution sérielle des longs documents |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | un seul modèle 12B en mémoire |
| `OLLAMA_KEEP_ALIVE` | `30m` | évite 15–20 s de rechargement à chaque exécution |

Vérifié : `ollama ps` affiche `100% GPU` ; le journal indique `offloaded 49/49 layers to GPU`, `flash_attn = enabled`, `K (q8_0) / V (q8_0)`.

Fenêtre de contexte : `8192` par défaut (surchargeable via `MB_NUM_CTX`). Mesures à chaud pour `gemma3:12b` : `8192` → 27,6 s / 8,7 Go ; `12288` → 34,3 s / 8,9 Go. **8192 est l'optimum.**

Pipeline hybride (`--map-model`) : résumés de fragments par un modèle rapide, note finale par le modèle fort. Sur une transcription française de 28 694 caractères : `gemma3:12b` seul 344 s vs hybride (`qwen2.5:latest` + `gemma3:12b`) **106 s** (~3,2x). Deux garde-fous : plafond de tokens à l'étape map (`MAP_PREDICT=420`) et recompression toujours confiée au modèle fort.

Profils de l'interface web : **Rapide**, **Équilibré**, **Qualité**. L'en-tête YAML des notes enregistre `map_model`.

## Principe : pourquoi une source longue devient *plus rapide*

Une transcription française de 28 694 caractères prend 344 s avec `gemma3:12b` seul, 106 s avec la chaîne hybride. Le gain ne vient pas d'agents exécutés en parallèle, mais du fait qu'on réduit le travail de chaque étape et qu'on ne paie le gros modèle qu'une fois.

```text
source (>5000 caractères)
   │  split_text : 3500 caractères/fragment, chevauchement de 200 (pas de phrase coupée)
   ▼
[map] un appel par fragment -> puces brutes, num_predict=420 (~800 caractères max)
      séquentiel volontaire : NUM_PARALLEL=1, des fragments en parallèle se disputeraient la VRAM
   ▼
résumé fusionné
   │  seulement si le budget est encore dépassé -> reduce_to_budget avec le modèle fort,
   │  3 tours maximum, arrêt dès que la compression stagne
   ▼
[final] consignes + prompt de tâche + règle de langue + votre intention + résumé -> la note
```

Budget : `NUM_CTX=8192` moins la réserve de sortie et ~700 jetons d'instructions, estimé à `CHAR_PER_TOKEN=2` → environ 8 700 caractères sûrs par exécution.

Trois raisons : (1) le 12B ne rédige que la note finale, environ 2,6 fois plus lent par jeton que le 7B ; (2) le temps de génération suit la longueur produite, et chaque fragment est plafonné à 420 jetons ; (3) on évite la voie coûteuse où une source de 28k dépasse le contexte et où `gemma3` doit recompresser tour après tour.

Différence avec « plusieurs sous-agents » : le découpage est un pipeline map-reduce figé — même prompt, sans état, sans dialogue, sans décision — il transforme juste une requête trop longue en plusieurs courtes. Les sous-agents portent chacun un objectif, des outils et une planification. Ici le gain vient de *moins de travail total*, pas de la concurrence.

## Politique de langue de sortie

**Le corps du texte est toujours rédigé en chinois.** `--lang` ne contrôle que la langue de parallèle pour les termes/paragraphes clés ; il ne produit jamais de résumé entier en langue étrangère. Le glossaire final est toujours trilingue : **中文 / English / Français**.

| `--lang` | Signification |
|---|---|
| `zh` | Corps chinois seul, sans parallèle de paragraphe (défaut) |
| `en` | Corps chinois + parallèle anglais pour les termes/paragraphes clés |
| `fr` | Corps chinois + parallèle français pour les termes/paragraphes clés |
| `bilingual` | Corps chinois + parallèle anglais et français |
| `fr-zh` | alias historique de `bilingual`, conservé pour compatibilité |

## Interface web

Outre la CLI, un tableau de bord web local est disponible (127.0.0.1 ; les données restent sur la machine) :

```powershell
cd C:\path\to\mon_brain
.\.venv\Scripts\python.exe app_web.py
# ouvre automatiquement http://127.0.0.1:5000
```

- L'interface est par défaut en **anglais** ; passage au **chinois / français** en haut à droite (pratique pour un relecteur anglophone).
- **Interface auto-explicative** : le bloc dépliable *Guide d'utilisation* couvre les trois modes, le coût mesuré de chaque profil (avec le nom exact des modèles), le rôle du modèle de résumé de fragments, le contenu du panneau de débogage, comment remplir l'intention et comment continuer à interroger la révision. Chaque réglage a un badge `?` avec une infobulle dans la langue courante ; les modes s'affichent `translate_summary · 翻译总结 · Résumé`.
- **Entièrement hors ligne** : Tailwind est localisé dans `static\tailwind.js` et servi depuis `/static/tailwind.js` (plus d'appel au CDN). Sur une nouvelle machine, lancer `tools\fetch_vendor.ps1` une fois.
- **Page `/docs`** : «说明 / Docs» en haut ouvre `http://127.0.0.1:5000/docs` dans un nouvel onglet et affiche ce README en page web (zh / en / fr). Principe, architecture, tableaux de paramètres, mesures. Un lien permet de revenir à l'atelier.
- Deux modes de sortie :
  - `Résumé chinois + termes parallèles` (défaut) : corps chinois + langue parallèle choisie + glossaire trilingue ;
  - `Suivre la langue de la source (réponse entière)` : réponse entière dans la langue de la source (source française → français, anglaise → anglais), sans passage au chinois.
- Prend en charge : envoi de plusieurs fichiers, choix du mode / langue parallèle / modèle, case de fusion, démarrage à neuf, suivi de révision en bulle, liste historique avec visualisation et téléchargement.
- La révision propose un champ facultatif « intention / votre question » (CLI `--intent`) ; laisser vide pour réviser le seul matériau.
- Si Ollama local est inaccessible, la zone de résultat affiche une alerte rouge pour le démarrer.
- Les fichiers envoyés sont temporaires dans `sources\_upload\` ; les dossiers de plus de 24 h sont nettoyés au prochain envoi. Les notes finales restent dans `sortie\`.

## Exemples

Ouvrez PowerShell dans ce dossier :

```powershell
# Utiliser directement le Python du venv (sans activation)
$py = ".\.venv\Scripts\python.exe"

# 1) Fichier seul : résumé + organisation (mode par défaut, corps chinois)
& $py mon_brain.py "C:\path\to\notes\lecture.txt" --lang bilingual

# 2) Traiter chaque fichier d'un dossier en note séparée
& $py mon_brain.py "C:\path\to\notes" --mode organize

# 3) Fusionner tous les fichiers du dossier en une seule note (contexte partagé)
& $py mon_brain.py "C:\path\to\notes" --mode organize --merge

# 4) Mode révision (chaîne conservée, évaluation de la question + auto-contrôle), puis boucle de suivi
& $py mon_brain.py sources\test_recap.txt --mode recap --interactive

# 5) Reprendre la session de révision précédente (suivi multi-tours)
& $py mon_brain.py sources\test_recap.txt --mode recap --continue

# 6) Surcharger le modèle ; --fresh ignore le brouillon précédent et repart de zéro
& $py mon_brain.py sources\xxx.docx --mode recap --model deepseek-r1:8b --fresh

# 7) Lister les fichiers de prompt disponibles
& $py mon_brain.py --list-prompts
```

## Paramètres CLI

| Paramètre | Effet |
|---|---|
| `--mode` | `organize` / `translate_summary` / `recap` ; défaut `translate_summary` |
| `--model` | surcharger le modèle par défaut du mode |
| `--lang` | `zh` / `en` / `fr` / `bilingual` (voir tableau ci-dessus) |
| `--merge` | pour un dossier, combiner tous les fichiers en une note au lieu d'une note par fichier |
| `--fresh` | désactiver la mise à jour incrémentale : ne pas lire le brouillon précédent |
| `--interactive` | révision : après le premier brouillon, entrer dans une boucle de questions/réponses |
| `--intent` | révision uniquement : votre question / ce que vous attendez ce tour, pour évaluer la qualité de la question ; vide = révision du seul matériau |
| `--continue` | charger la session de révision précédente depuis `sessions\` et continuer |
| `--list-prompts` | lister les prompts dans `prompts\` puis quitter |

## Contrôles de l'interface (après chaque modification du front)

L'interface tient dans un seul `<script>` inline : une seule parenthèse manquante empêche tout le script de s'exécuter (boutons de langue, profils de calcul et upload semblent alors « morts »). À lancer avant tout test :

```powershell
.\.venv\Scripts\python.exe tools\check_page.py
```

Il vérifie la syntaxe JS (`node --check`), la couverture des clés i18n en/zh/fr, et l'existence de chaque cible `getElementById` dans le HTML. Code de sortie 0 = tout est bon.

## Table statique de corrections (corrections.txt)

Les fautes courantes de reconnaissance vocale (ex. `Olama → Ollama`, `Lama → Llama`, `LocalRoss → localhost`) sont listées dans `corrections.txt`. Le script la charge au démarrage et l'injecte dans le prompt pour que le modèle restaure les bons mots.

- Format : `faute => correct  # note` ; les lignes commençant par `#` sont des commentaires.
- La section `[uncertain]` contient les éléments à confirmer ; ils sont marqués incertains dans la sortie et jamais présentés comme des faits.
- Ajoutez vos propres fautes fréquentes à tout moment.

## Mise à jour incrémentale

Lors du retraitement d'une même source (même mode, même nom de fichier), le script retrouve automatiquement le dernier brouillon dans `sortie\` et l'injecte comme « brouillon précédent » : il le révise et le complète au lieu de repartir de zéro. `--fresh` désactive ce comportement. Les anciens brouillons ne sont jamais écrasés.

## Fusion et mémoire de session

- **Mode fusion `--merge`** : chaque fichier est d'abord résumé ; les conclusions des fichiers précédents servent de contexte aux suivants, puis tout est fusionné en une note unique. L'en-tête `source` liste tous les fichiers d'entrée.
- **Sessions de révision** : le premier échange est stocké dans `sessions\<source>.json` ; `--interactive` permet de continuer à poser des questions après le premier brouillon, et `--continue` reprend l'historique suivant. Les tours de suivi sont ajoutés à la même note Markdown.

## Règles de sortie

- Nom de fichier : `date_mode_source.md` ; un nom existant reçoit un numéro incrémenté — **les originaux ne sont jamais écrasés**.
- En-tête YAML : source, mode, modèle, langue, date ; les notes de révision référencent aussi le fichier de session.
- **Texte long** : au-delà d'environ 5000 caractères, découpage en fragments, résumé de chaque fragment puis fusion en une note (pas de troncature brute).
- Si la source fusionnée reste trop longue, un avertissement suggère de la traiter en plusieurs passages.
- **Mode révision** : deux appels — révision de la source d'abord, puis évaluation de la question + auto-contrôle ; la chaîne de réflexion est conservée dans sa propre section ; les balises de bloc Markdown superflues sont retirées automatiquement.

## Limites actuelles (lots ultérieurs)

1. **Sections de révision répétées (corrigé)** : le second appel réémet souvent les trois sections. `strip_repeated_sections` coupe au premier titre répété (4 836 → 2 501 caractères sur un cas réel), et prompts/recap.md impose « chaque section une seule fois ».
2. **Parallèle de paragraphe peu fiable** : les modèles locaux 12B ne suivent pas constamment « ajouter un parallèle étranger aux paragraphes clés » ; le glossaire trilingue est souvent seul produit.
3. **Plafond de longueur** : environ 2048–2560 jetons par appel ; les très longues réponses peuvent rester incomplètes en fin.
4. **PDF scannés illisibles** : les PDF image nécessitent une OCR (llava / easyocr), pas encore intégrée.
5. **Plafond du modèle** : les modèles locaux 7B–12B sont moins bons que les modèles cloud sur l'idiomatique française, la logique longue et le rendu des termes anglais ; relisez la sortie et ne traitez pas les éléments incertains comme des faits.
6. À faire : analyse de projet de code source, OCR des PDF scannés, continuation segmentée des longues réponses ; la zone de résultat web affiche actuellement le Markdown en texte brut, le rendu riche est à venir.

## Dépannage

- **Impossible de joindre Ollama / Connection refused** : vérifiez qu'Ollama tourne (icône dans la barre des tâches, ou lancez `ollama serve` dans un autre terminal).
- **model not found** : le nom du modèle ne correspond pas à `ollama list` ; utilisez `--model` avec le nom exact de la liste.
- **PDF sans texte** : très probablement un PDF image scanné ; l'OCR n'est pas pris en charge dans cette version.
- **Caractères chinois/français illisibles** : le script force l'UTF-8 ; si la console reste garbled, vérifiez que vous utilisez le Python du `.venv`.


## Droits d'auteur et utilisation (pas une licence open source)

Copyright © 2026 NexPathArchitect. **Tous droits réservés.**

- Autorisé : lire le code, en comprendre l'approche, exécuter votre propre copie locale.
- Interdit : copier, modifier, redistribuer ou utiliser à des fins commerciales sans autorisation écrite, ou se l'approprier.
- Toute citation de cette documentation doit créditer l'auteur et ce dépôt.

Contactez le propriétaire du dépôt pour toute autorisation. Ce dépôt **n'est pas** open source et ne comporte aucune licence open source.
