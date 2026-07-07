#!/usr/bin/env python3
"""Assistant généraliste hybride construit sur le Claude Agent SDK.

Architecture (les deux patterns du billet Anthropic, combinés) :

    ┌─────────────────────┐   escalade (rare)   ┌──────────────────┐
    │  EXECUTOR           │ ──────────────────▶ │  ADVISOR         │
    │  Sonnet 5           │                     │  Fable 5         │
    │  boucle principale  │ ◀────────────────── │  plan / déblocage│
    └────────┬────────────┘      conseil        └──────────────────┘
             │ fan-out (parallèle)
             ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ researcher   │  │ researcher   │  │ scout        │
    │ Sonnet 5     │  │ Sonnet 5     │  │ Haiku 4.5    │
    └──────────────┘  └──────────────┘  └──────────────┘

L'exécuteur Sonnet 5 traite chaque tour (la majorité des tokens sont
facturés au tarif Sonnet). Fable 5 n'est consulté qu'aux moments à fort
levier : plan initial d'une tâche complexe, déblocage, revue finale.
Le travail token-intensif (recherche web, lecture de gros volumes) part
en parallèle vers des workers Sonnet 5 / Haiku 4.5 dont seule la synthèse
revient dans le contexte principal.

Usage :
    python assistant.py                  # REPL interactif
    python assistant.py --once "..."     # un seul prompt puis sortie
    python assistant.py --auto           # auto-approuve les éditions de fichiers
"""

import argparse
import asyncio
import os
import sys
import warnings
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

# Modèles surchargables par variable d'environnement.
EXECUTOR_MODEL = os.environ.get("HYBRID_EXECUTOR_MODEL", "claude-sonnet-5")
ADVISOR_MODEL = os.environ.get("HYBRID_ADVISOR_MODEL", "claude-fable-5")
SCOUT_MODEL = os.environ.get("HYBRID_SCOUT_MODEL", "claude-haiku-4-5")

# Plafond de dépense par session (le SDK arrête l'agent au-delà).
MAX_BUDGET_USD = float(os.environ.get("HYBRID_MAX_BUDGET_USD", "2.0"))

# Sources de réglages Claude Code à hériter (ex. "user" pour récupérer les
# serveurs MCP configurés sur la machine). Vide = comportement autonome.
SETTING_SOURCES = [s for s in os.environ.get("HYBRID_SETTING_SOURCES", "").split(",") if s]

# Mémoire persistante inter-sessions : un simple fichier markdown que
# l'assistant lit au démarrage et met à jour quand il apprend quelque chose.
MEMORY_FILE = Path(os.environ.get("HYBRID_MEMORY_FILE", str(Path.home() / ".hybrid-assistant-memory.md")))
MEMORY_MAX_CHARS = 8000


def _memory_section() -> str:
    try:
        content = MEMORY_FILE.read_text(encoding="utf-8").strip()[:MEMORY_MAX_CHARS]
    except OSError:
        content = ""
    return f"""

## Mémoire persistante
Ton fichier de mémoire est `{MEMORY_FILE}`. Son contenu au démarrage de cette
session est reproduit ci-dessous. Quand tu apprends quelque chose de durable sur
l'utilisateur (préférence, contexte de projet, correction importante), mets ce
fichier à jour avec l'outil Edit ou Write — une information par ligne, concis,
en supprimant ce qui est devenu faux. N'y stocke JAMAIS de secrets, tokens ou
mots de passe.

<memoire>
{content or "(vide pour l'instant)"}
</memoire>"""

SYSTEM_PROMPT = """Tu es un assistant généraliste efficace. Tu es l'EXÉCUTEUR \
d'une architecture hybride : tu fais toi-même le travail courant, et tu disposes \
de trois sous-agents à utiliser selon cette politique de routage.

## Politique de routage

1. ESCALADE VERS `advisor` (modèle frontière, coûteux — au plus 1 à 2 appels par tâche) :
   - au démarrage d'une tâche complexe ou ambiguë, pour obtenir un plan ;
   - quand tu es bloqué après deux tentatives infructueuses ;
   - pour une revue finale quand l'enjeu du résultat est élevé.
   N'appelle JAMAIS l'advisor pour une question simple, une reformulation ou une
   tâche que tu sais faire. Donne-lui tout le contexte nécessaire dans le prompt
   (il ne voit pas la conversation) et pose-lui une question précise.

2. DÉLÉGATION VERS `researcher` (fan-out parallèle) :
   - recherche web, veille, comparaison de sources ;
   - lecture ou analyse de gros volumes (nombreux fichiers, longues pages).
   Lance plusieurs researchers EN PARALLÈLE quand les sous-questions sont
   indépendantes. Chaque worker doit recevoir une sous-question autonome et
   retourner une synthèse courte avec ses sources.

3. DÉLÉGATION VERS `scout` (très bon marché) :
   - vérification ponctuelle d'un fait, localisation d'un fichier, lookup simple.

4. TOUT LE RESTE : fais-le toi-même, directement.

## Règle d'attente
Quand tu invoques un sous-agent dont tu as besoin du résultat pour répondre,
passe `run_in_background: false` à l'outil Agent. Ne termine JAMAIS ton tour
en « attendant » un sous-agent : collecte ses résultats, puis réponds. Le
lancement en arrière-plan n'est permis que pour du travail dont le résultat
sera consommé à un tour ultérieur.

## Style
Réponds dans la langue de l'utilisateur. Va au résultat d'abord, les détails
ensuite. Quand des sous-agents ont travaillé, synthétise — ne recopie pas leurs
rapports bruts."""

AGENTS = {
    "advisor": AgentDefinition(
        description=(
            "Conseiller stratégique sur modèle frontière. À consulter uniquement "
            "aux moments à fort levier : plan initial d'une tâche complexe, "
            "déblocage après échecs répétés, revue finale d'un résultat critique. "
            "Coûteux — au plus 1 à 2 appels par tâche."
        ),
        prompt=(
            "Tu es un conseiller senior consulté ponctuellement par un agent "
            "exécuteur. On t'appelle parce que la décision est difficile : plan "
            "d'attaque, diagnostic d'un blocage, ou revue critique. Tu ne fais "
            "pas le travail toi-même : tu rends un avis actionnable, structuré "
            "et court — le plan ou le diagnostic, les risques principaux, et la "
            "prochaine étape concrète. Si le contexte fourni est insuffisant "
            "pour trancher, dis exactement quelle information manque."
        ),
        tools=["Read", "Grep", "Glob"],
        model=ADVISOR_MODEL,
        effort="xhigh",
        maxTurns=15,
    ),
    "researcher": AgentDefinition(
        description=(
            "Worker de recherche pour le travail token-intensif : recherche web, "
            "lecture de documentation, exploration de nombreux fichiers. Lancer "
            "plusieurs researchers en parallèle sur des sous-questions "
            "indépendantes."
        ),
        prompt=(
            "Tu es un worker de recherche. Traite la sous-question qu'on te "
            "confie de façon autonome : cherche, lis, recoupe. Retourne "
            "UNIQUEMENT une synthèse compacte : les faits trouvés, leur source, "
            "et ton niveau de confiance. Pas de narration de ta démarche."
        ),
        tools=["WebSearch", "WebFetch", "Read", "Grep", "Glob"],
        model="inherit",
        effort="medium",
        maxTurns=25,
    ),
    "scout": AgentDefinition(
        description=(
            "Éclaireur ultra-économique pour les vérifications ponctuelles : "
            "localiser un fichier, vérifier un fait dans le code, lookup simple."
        ),
        prompt=(
            "Tu es un éclaireur rapide. Réponds à la question posée en un "
            "minimum d'étapes et retourne une réponse d'une à trois phrases."
        ),
        tools=["Read", "Grep", "Glob"],
        model=SCOUT_MODEL,
        effort="low",
        maxTurns=10,
    ),
}


def build_options(auto: bool) -> ClaudeAgentOptions:
    async def gate_tool(tool_name: str, input_data: dict, _context):
        # Force chaque délégation en mode synchrone : l'exécuteur attend le
        # résultat du sous-agent au lieu de le lancer en arrière-plan et de
        # clore son tour. Le fan-out parallèle reste possible (plusieurs
        # appels Agent dans un même tour s'exécutent en concurrence).
        if tool_name in ("Agent", "Task"):
            return PermissionResultAllow(
                updated_input={**input_data, "run_in_background": False}
            )
        if auto:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(
            message=(
                "Outil refusé en mode par défaut (lecture/recherche seulement). "
                "Relance l'assistant avec --auto pour autoriser les écritures."
            ),
            interrupt=False,
        )

    return ClaudeAgentOptions(
        model=EXECUTOR_MODEL,
        system_prompt=SYSTEM_PROMPT + _memory_section(),
        agents=AGENTS,
        # Lecture et recherche auto-approuvées ; l'outil Agent passe
        # volontairement par gate_tool (voir ci-dessus). Les écritures et Bash
        # sont refusés sauf --auto.
        allowed_tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite"],
        can_use_tool=gate_tool,
        permission_mode="acceptEdits" if auto else "default",
        max_budget_usd=MAX_BUDGET_USD,
        # Par défaut ([]), n'hérite d'aucun réglage Claude Code de la machine
        # hôte. HYBRID_SETTING_SOURCES=user permet d'hériter des serveurs MCP
        # configurés sur la machine (Gmail, Calendar, ...).
        setting_sources=SETTING_SOURCES,
    )


async def handle_turn(client: ClaudeSDKClient, prompt: str, totals: dict) -> None:
    """Envoie un tour et affiche la réponse, les délégations et le coût."""
    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            # Les messages émis à l'intérieur d'un sous-agent portent
            # parent_tool_use_id : on ne les affiche pas (seule leur synthèse
            # revient à l'exécuteur).
            if getattr(message, "parent_tool_use_id", None):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
                elif isinstance(block, ToolUseBlock) and block.name in ("Agent", "Task"):
                    subagent = block.input.get("subagent_type", "?")
                    print(f"\n  ↳ délégation → {subagent}", flush=True)
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            totals["cost"] += cost
            totals["turns"] += 1
            print(
                f"\n[tour {totals['turns']} : ${cost:.4f} — "
                f"session : ${totals['cost']:.4f} — {message.duration_ms} ms]"
            )


async def run(auto: bool, once: str | None) -> None:
    totals = {"cost": 0.0, "turns": 0}
    async with ClaudeSDKClient(options=build_options(auto)) as client:
        if once is not None:
            await handle_turn(client, once, totals)
            return
        print("Assistant hybride (executor Sonnet 5 + advisor Fable 5).")
        print("Tape 'exit' pour quitter.\n")
        while True:
            try:
                user = (await asyncio.to_thread(input, "vous> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user.lower() in {"exit", "quit"}:
                break
            if not user:
                continue
            await handle_turn(client, user, totals)
            print()


def main() -> None:
    # Les outils en lecture seule sont volontairement auto-approuvés via
    # allowed_tools et ne passent pas par gate_tool : cet avertissement du SDK
    # décrit un comportement voulu.
    warnings.filterwarnings("ignore", message=".*can_use_tool will not be invoked.*")
    parser = argparse.ArgumentParser(description="Assistant généraliste hybride")
    parser.add_argument("--once", metavar="PROMPT", help="exécute un seul prompt puis quitte")
    parser.add_argument("--auto", action="store_true", help="auto-approuve les éditions de fichiers")
    args = parser.parse_args()
    try:
        asyncio.run(run(auto=args.auto, once=args.once))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
