# Assistant généraliste hybride — executor Sonnet 5 + advisor Fable 5

Un assistant multi-agents construit sur le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk),
qui combine les deux patterns de routage de modèles publiés par Anthropic :

- **Advisor (« escalade vers le haut »)** : un exécuteur Sonnet 5 fait tourner la
  boucle principale et consulte Fable 5 seulement aux moments à fort levier
  (plan initial, déblocage, revue finale). Sur SWE-bench Pro, ce setup atteint
  ~92 % du score de Fable 5 pour ~63 % du prix.
- **Orchestrator/workers (« délégation vers le bas »)** : le travail
  token-intensif (recherche web, lecture de gros volumes) part en parallèle vers
  des workers Sonnet 5 et Haiku 4.5 ; seule leur synthèse revient dans le
  contexte principal. Sur BrowseComp, le pattern équivalent atteint ~96 % de la
  précision de Fable 5 pour ~46 % du prix.

```
                     escalade (≤ 1-2 appels/tâche)
  EXECUTOR  Sonnet 5 ────────────────────────────▶  ADVISOR  Fable 5 (xhigh)
  boucle principale ◀────────────────────────────   plan / déblocage / revue
        │
        │ fan-out parallèle (token-intensif)
        ├──▶ researcher  Sonnet 5  (web + lecture, effort medium)
        ├──▶ researcher  Sonnet 5
        └──▶ scout       Haiku 4.5 (lookups, effort low)
```

## Pourquoi cette forme

Le coût d'un agent est dominé par la boucle principale (elle relit tout le
contexte à chaque tour) et par les tokens de recherche. Cette architecture met
donc :

1. **le modèle économique là où passent les tokens** — Sonnet 5 (3 $/15 $ par
   Mtok) sur la boucle et les workers ;
2. **le modèle frontière là où passe l'intelligence** — Fable 5 (10 $/50 $)
   sur les quelques décisions qui déterminent la qualité du résultat ;
3. **l'isolation de contexte comme levier** — chaque sous-agent a sa propre
   fenêtre : les tokens qu'un researcher brûle à lire des pages ne polluent ni
   ne facturent le contexte de l'exécuteur.

La politique de routage vit dans le system prompt de l'exécuteur
(`SYSTEM_PROMPT` dans `assistant.py`) : c'est elle qui fait l'« hybride » —
escalader vers le haut ou déléguer vers le bas selon la tâche.

## Installation

```bash
pip install claude-agent-sdk
```

Le SDK s'appuie sur le CLI Claude Code et ses identifiants (`claude` connecté,
ou `ANTHROPIC_API_KEY` exportée).

## Usage

```bash
python assistant.py                          # REPL interactif
python assistant.py --once "Ta question"     # un prompt, puis sortie
python assistant.py --auto                   # auto-approuve les éditions
```

Chaque tour affiche les délégations (`↳ délégation → advisor`) et le coût
(`[tour 1 : $0.0312 — session : $0.0312 — 8451 ms]`) lu depuis
`ResultMessage.total_cost_usd`.

## Configuration

| Variable d'environnement | Défaut | Rôle |
|---|---|---|
| `HYBRID_EXECUTOR_MODEL` | `claude-sonnet-5` | Boucle principale |
| `HYBRID_ADVISOR_MODEL` | `claude-fable-5` | Advisor (escalade) |
| `HYBRID_SCOUT_MODEL` | `claude-haiku-4-5` | Scout (lookups) |

Pour un budget serré, `HYBRID_ADVISOR_MODEL=claude-opus-4-8` garde le pattern
en divisant le coût de l'advisor par deux.

## Ajuster le routage

Trois curseurs, par ordre d'impact :

1. **La description des sous-agents** (`AGENTS` dans `assistant.py`) — c'est
   elle que l'exécuteur lit pour décider de déléguer. « Coûteux — au plus 1 à 2
   appels par tâche » est ce qui empêche l'advisor d'être sur-appelé.
2. **`effort`** — `xhigh` pour l'advisor (on le paie pour réfléchir), `medium`
   pour les researchers, `low` pour le scout.
3. **`tools`** — l'advisor est en lecture seule : il conseille, il n'exécute
   pas. C'est à la fois une garantie de sûreté et ce qui maintient son coût bas.

## Déploiement permanent sur Mac Mini (Telegram)

L'interface permanente est un **pont Telegram** (`telegram_bridge.py`, stdlib
uniquement côté Telegram, comme le tracker) : tu parles à l'assistant depuis
ton téléphone, le contexte de conversation persiste entre les messages, et
aucun port n'est exposé sur le Mini (long-polling sortant uniquement).

```bash
# Sur le Mac Mini :
git pull
cat > ~/.hybrid-assistant.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=123456789
# HYBRID_AUTO=1              # optionnel : autorise Bash/Edit/Write
EOF
bash hybrid-assistant/deploy/setup-macmini.sh   # venv + SDK + service launchd
```

Le service launchd (`deploy/com.hybrid-assistant-telegram.plist`) relance le
pont au boot et en cas de crash. Logs : `/tmp/hybrid-assistant-telegram.log`.

Côté Telegram :
- messages ordinaires → tours de conversation (contexte conservé) ;
- `/new` → conversation vierge ; `/cost` → coût cumulé de la session ;
- seul le `TELEGRAM_CHAT_ID` déclaré est écouté — tout autre chat est ignoré ;
- chaque réponse se termine par le coût du tour et les agents sollicités.

Alternative sans Telegram : un REPL maintenu dans tmux
(`deploy/com.hybrid-assistant.plist`), à reprendre avec
`tmux attach -t hybrid-assistant`.

Prérequis sur le Mac Mini : Python 3.10+ et des identifiants Anthropic
(CLI Claude Code connecté, ou `ANTHROPIC_API_KEY` dans le fichier env).

## Limites connues

- Le routage est probabiliste : l'exécuteur peut décider de ne pas escalader
  sur une tâche qui l'aurait mérité. Pour forcer, nommer l'agent dans le
  prompt (« utilise l'agent advisor pour... »).
- Les ratios coût/qualité cités viennent de benchmarks Anthropic (SWE-bench Pro
  sur un sous-ensemble de 482 problèmes, BrowseComp full set) ; vos ratios
  dépendront de votre mix de tâches.
