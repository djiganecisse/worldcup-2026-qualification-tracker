#!/usr/bin/env python3
"""Pont Telegram pour l'assistant hybride — service permanent sur le Mac Mini.

Long-polling de l'API Telegram Bot (stdlib uniquement, comme le tracker),
et une session ClaudeSDKClient persistante : le contexte de conversation
est conservé entre les messages, jusqu'à /new.

Variables d'environnement :
    TELEGRAM_BOT_TOKEN   (requis)  token du bot
    TELEGRAM_CHAT_ID     (requis)  seul chat autorisé à parler à l'assistant
    HYBRID_AUTO=1        (option)  autorise les outils d'écriture (Bash, Edit…)

Commandes :
    /new    repart d'une conversation vierge
    /cost   coût cumulé de la session en cours

Usage :
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python telegram_bridge.py
"""

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack

from assistant import build_options
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
AUTO = os.environ.get("HYBRID_AUTO", "") == "1"
API = f"https://api.telegram.org/bot{TOKEN}"

TG_MAX = 4096  # limite Telegram par message


def _tg_call(method: str, **params) -> dict:
    """Appel bloquant à l'API Telegram (à exécuter via asyncio.to_thread)."""
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as resp:
        return json.loads(resp.read().decode())


def _chunks(text: str, size: int = TG_MAX):
    """Découpe un texte en morceaux <= size, de préférence sur une fin de ligne."""
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 1, size)
        if cut == -1:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


async def tg(method: str, **params) -> dict:
    return await asyncio.to_thread(_tg_call, method, **params)


async def send(text: str) -> None:
    for part in _chunks(text.strip() or "(réponse vide)"):
        await tg("sendMessage", chat_id=CHAT_ID, text=part)


async def run_turn(client: ClaudeSDKClient, prompt: str, totals: dict) -> str:
    """Envoie un tour à l'assistant et retourne la réponse formatée."""
    parts: list[str] = []
    delegations: list[str] = []
    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            if getattr(message, "parent_tool_use_id", None):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                elif isinstance(block, ToolUseBlock) and block.name in ("Agent", "Task"):
                    delegations.append(block.input.get("subagent_type", "?"))
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            totals["cost"] += cost
            footer = f"\n\n— ${cost:.4f} (session ${totals['cost']:.4f})"
            if delegations:
                footer += " · agents: " + ", ".join(delegations)
            parts.append(footer)
    return "".join(parts)


async def main() -> None:
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis.")

    totals = {"cost": 0.0}
    offset: int | None = None

    stack = AsyncExitStack()
    client = await stack.enter_async_context(ClaudeSDKClient(options=build_options(AUTO)))
    await send("🤖 Assistant hybride en ligne (executor Sonnet 5 + advisor Fable 5).")

    try:
        while True:
            try:
                updates = await tg(
                    "getUpdates",
                    timeout=50,
                    **({"offset": offset} if offset is not None else {}),
                )
            except Exception:
                await asyncio.sleep(5)  # réseau ou timeout : on réessaie
                continue

            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                text = (msg.get("text") or "").strip()
                if not text or str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue  # ignore tout chat non autorisé

                if text == "/new":
                    await stack.aclose()
                    stack = AsyncExitStack()
                    client = await stack.enter_async_context(
                        ClaudeSDKClient(options=build_options(AUTO))
                    )
                    totals["cost"] = 0.0
                    await send("🆕 Nouvelle conversation.")
                    continue
                if text == "/cost":
                    await send(f"Coût de la session : ${totals['cost']:.4f}")
                    continue

                await tg("sendChatAction", chat_id=CHAT_ID, action="typing")
                try:
                    reply = await run_turn(client, text, totals)
                except Exception as exc:  # session cassée : on la recrée
                    await send(f"⚠️ Erreur : {exc}\nJe repars sur une session neuve.")
                    await stack.aclose()
                    stack = AsyncExitStack()
                    client = await stack.enter_async_context(
                        ClaudeSDKClient(options=build_options(AUTO))
                    )
                    continue
                await send(reply)
    finally:
        await stack.aclose()


if __name__ == "__main__":
    asyncio.run(main())
