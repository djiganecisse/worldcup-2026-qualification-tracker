#!/usr/bin/env python3
"""Pont Telegram pour l'assistant hybride — service permanent sur le Mac Mini.

Long-polling de l'API Telegram Bot (stdlib uniquement, comme le tracker),
et une session ClaudeSDKClient persistante : le contexte de conversation
est conservé entre les messages, jusqu'à /new ou l'expiration d'inactivité.

Variables d'environnement :
    TELEGRAM_BOT_TOKEN          (requis)  token du bot
    TELEGRAM_CHAT_ID            (requis)  seul chat autorisé
    HYBRID_AUTO=1               (option)  autorise les outils d'écriture
    HYBRID_MAX_BUDGET_USD       (option)  plafond $/session (défaut 2.0)
    HYBRID_SESSION_TTL_HOURS    (option)  reset auto après inactivité (défaut 12)
    HYBRID_BRIEFING=07:30       (option)  heure d'un briefing quotidien proactif
    HYBRID_BRIEFING_PROMPT      (option)  prompt du briefing
    HYBRID_WHISPER_CMD          (option)  commande de transcription des vocaux,
                                          ex. "whisper-cli -f {file} -np -nt"
    HYBRID_SETTING_SOURCES=user (option)  hérite des MCP de Claude Code (Gmail...)

Commandes Telegram :
    /new     conversation vierge        /cost   coût de la session
    /stats   usage des 7 derniers jours /update git pull + redémarrage
    /help    aide
"""

import asyncio
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

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
MAX_BUDGET = float(os.environ.get("HYBRID_MAX_BUDGET_USD", "2.0"))
SESSION_TTL_S = float(os.environ.get("HYBRID_SESSION_TTL_HOURS", "12")) * 3600
BRIEFING_TIME = os.environ.get("HYBRID_BRIEFING", "").strip()  # "HH:MM" ou vide
BRIEFING_PROMPT = os.environ.get(
    "HYBRID_BRIEFING_PROMPT",
    "Briefing du matin : donne la date du jour, puis un point concis (5 à 8 "
    "lignes) sur l'actualité tech/IA marquante des dernières 24 h via la "
    "recherche web, et termine par les rappels utiles de ta mémoire "
    "persistante s'il y en a.",
)
WHISPER_CMD = os.environ.get("HYBRID_WHISPER_CMD", "").strip()

API = f"https://api.telegram.org/bot{TOKEN}"
TG_MAX = 4096  # limite Telegram par message
REPO_ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = Path.home() / ".hybrid-assistant-media"
TELEMETRY_FILE = Path.home() / ".hybrid-assistant-telemetry.jsonl"

DELEGATION_LABELS = {
    "advisor": "🧠 Je consulte l'advisor (modèle frontière) — ça peut prendre quelques minutes...",
    "researcher": "🔍 Recherche déléguée à un ou plusieurs workers...",
    "scout": "⚡ Vérification rapide par le scout...",
}

HELP_TEXT = (
    "Commandes :\n"
    "/new — conversation vierge (le contexte est sinon conservé)\n"
    "/cost — coût de la session en cours\n"
    "/stats — usage des 7 derniers jours\n"
    "/update — met à jour le code (git pull) et redémarre le service\n"
    "/help — cette aide\n\n"
    "Tu peux aussi envoyer des photos et des documents (je les lis), "
    "et des vocaux si la transcription est configurée."
)


# ── Telegram (stdlib) ─────────────────────────────────────────────────────

def _tg_call(method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as resp:
        return json.loads(resp.read().decode())


async def tg(method: str, **params) -> dict:
    return await asyncio.to_thread(_tg_call, method, **params)


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


async def send(text: str) -> None:
    for part in _chunks(text.strip() or "(réponse vide)"):
        await tg("sendMessage", chat_id=CHAT_ID, text=part)


def _download_tg_file(file_id: str, suffix: str) -> Path:
    """Télécharge un fichier Telegram dans MEDIA_DIR (bloquant)."""
    info = _tg_call("getFile", file_id=file_id)
    remote = info["result"]["file_path"]
    MEDIA_DIR.mkdir(mode=0o700, exist_ok=True)
    dest = MEDIA_DIR / f"{int(time.time())}-{file_id[-8:]}{suffix}"
    url = f"https://api.telegram.org/file/bot{TOKEN}/{remote}"
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:
        f.write(resp.read())
    return dest


# ── Télémétrie ────────────────────────────────────────────────────────────

def log_telemetry(kind: str, cost: float, duration_ms: int, delegations: list) -> None:
    try:
        entry = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "kind": kind,
            "cost": round(cost, 6),
            "duration_ms": duration_ms,
            "delegations": delegations,
        }
        with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # la télémétrie ne doit jamais casser un tour


def summarize_telemetry(days: int = 7) -> str:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    turns, cost, total_ms = 0, 0.0, 0
    per_agent: dict = {}
    try:
        with open(TELEMETRY_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if dt.datetime.fromisoformat(e["ts"]) < cutoff:
                        continue
                    turns += 1
                    cost += e.get("cost", 0.0)
                    total_ms += e.get("duration_ms", 0)
                    for d in e.get("delegations", []):
                        per_agent[d] = per_agent.get(d, 0) + 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except OSError:
        return "Pas encore de données d'usage."
    if not turns:
        return f"Aucun tour sur les {days} derniers jours."
    lines = [
        f"📊 {days} derniers jours :",
        f"• {turns} tours — ${cost:.2f} au total (moy. ${cost / turns:.4f}/tour)",
        f"• durée moyenne : {total_ms / turns / 1000:.1f}s/tour",
    ]
    if per_agent:
        detail = ", ".join(f"{k} ×{v}" for k, v in sorted(per_agent.items()))
        lines.append(f"• délégations : {detail}")
    else:
        lines.append("• aucune délégation (tout traité par l'exécuteur)")
    return "\n".join(lines)


# ── Le pont ───────────────────────────────────────────────────────────────

class Bridge:
    def __init__(self):
        self.stack: AsyncExitStack | None = None
        self.client: ClaudeSDKClient | None = None
        self.totals = {"cost": 0.0}
        self.budget_warned = False
        self.last_turn_at = time.monotonic()
        self.turn_lock = asyncio.Lock()

    async def start_session(self) -> None:
        if self.stack is not None:
            await self.stack.aclose()
        self.stack = AsyncExitStack()
        self.client = await self.stack.enter_async_context(
            ClaudeSDKClient(options=build_options(AUTO))
        )
        self.totals["cost"] = 0.0
        self.budget_warned = False
        self.last_turn_at = time.monotonic()

    async def close(self) -> None:
        if self.stack is not None:
            await self.stack.aclose()

    async def _typing_keepalive(self, stop: asyncio.Event) -> None:
        """Maintient l'indicateur « écrit... » (il expire toutes les ~5 s)."""
        while not stop.is_set():
            try:
                await tg("sendChatAction", chat_id=CHAT_ID, action="typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    async def run_turn(self, prompt: str, kind: str = "text") -> str:
        """Un tour complet : envoi, notifications de délégation, coût, télémétrie."""
        parts: list[str] = []
        delegations: list[str] = []
        announced: set = set()
        duration_ms = 0
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_keepalive(stop_typing))
        try:
            await self.client.query(prompt)
            async for message in self.client.receive_response():
                if isinstance(message, AssistantMessage):
                    if getattr(message, "parent_tool_use_id", None):
                        continue
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                        elif isinstance(block, ToolUseBlock) and block.name in ("Agent", "Task"):
                            agent = block.input.get("subagent_type", "?")
                            delegations.append(agent)
                            if agent not in announced:
                                announced.add(agent)
                                note = DELEGATION_LABELS.get(agent, f"↳ délégation → {agent}...")
                                await send(note)
                elif isinstance(message, ResultMessage):
                    cost = message.total_cost_usd or 0.0
                    duration_ms = message.duration_ms
                    self.totals["cost"] += cost
                    footer = f"\n\n— ${cost:.4f} (session ${self.totals['cost']:.4f})"
                    if delegations:
                        footer += " · agents: " + ", ".join(dict.fromkeys(delegations))
                    if message.subtype != "success":
                        footer += f" · fin: {message.subtype}"
                    parts.append(footer)
        finally:
            stop_typing.set()
            await typing_task
        self.last_turn_at = time.monotonic()
        log_telemetry(kind, self.totals["cost"], duration_ms, delegations)
        reply = "".join(parts)
        if not self.budget_warned and self.totals["cost"] >= 0.8 * MAX_BUDGET:
            self.budget_warned = True
            reply += (
                f"\n\n⚠️ Budget : ${self.totals['cost']:.2f} sur un plafond de "
                f"${MAX_BUDGET:.2f} pour cette session. /new remet le compteur à zéro."
            )
        return reply

    async def maybe_expire_session(self) -> bool:
        """Réinitialise la session après une longue inactivité (contexte périmé et coûteux)."""
        if time.monotonic() - self.last_turn_at > SESSION_TTL_S:
            await self.start_session()
            return True
        return False

    # ── Entrées non textuelles ────────────────────────────────────────────

    async def prompt_from_message(self, msg: dict) -> tuple[str | None, str]:
        """Transforme un message Telegram en prompt. Retourne (prompt, kind)."""
        text = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()

        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]  # la plus grande résolution
            path = await asyncio.to_thread(_download_tg_file, file_id, ".jpg")
            return (
                f"[L'utilisateur t'a envoyé une image, enregistrée ici : {path} — "
                f"lis-la avec l'outil Read avant de répondre.]\n{caption or 'Décris et analyse cette image.'}",
                "photo",
            )

        if msg.get("document"):
            doc = msg["document"]
            name = doc.get("file_name", "document")
            suffix = Path(name).suffix or ".bin"
            path = await asyncio.to_thread(_download_tg_file, doc["file_id"], suffix)
            return (
                f"[L'utilisateur t'a envoyé le fichier « {name} », enregistré ici : {path} — "
                f"lis-le avec l'outil Read avant de répondre.]\n{caption or 'Analyse ce document.'}",
                "document",
            )

        if msg.get("voice") or msg.get("audio"):
            media = msg.get("voice") or msg.get("audio")
            if not WHISPER_CMD:
                await send(
                    "🎙️ Vocal reçu, mais la transcription n'est pas configurée. "
                    "Installe un transcripteur (ex. `brew install whisper-cpp`) puis ajoute "
                    "HYBRID_WHISPER_CMD dans ~/.hybrid-assistant.env — voir le README."
                )
                return None, "voice"
            path = await asyncio.to_thread(_download_tg_file, media["file_id"], ".oga")
            cmd = WHISPER_CMD.format(file=str(path)) if "{file}" in WHISPER_CMD else f"{WHISPER_CMD} {path}"
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await proc.communicate()
            transcript = out.decode(errors="replace").strip()
            if proc.returncode != 0 or not transcript:
                await send(f"🎙️ Transcription échouée : {err.decode(errors='replace')[:300]}")
                return None, "voice"
            await send(f"🎙️ J'ai compris : « {transcript} »")
            return transcript, "voice"

        return (text or None), "text"

    # ── Commandes ─────────────────────────────────────────────────────────

    async def cmd_update(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(REPO_ROOT), "pull", "--ff-only",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = out.decode(errors="replace").strip()
        if proc.returncode != 0:
            await send(f"❌ git pull a échoué :\n{output[-1000:]}")
            return
        if "Already up to date" in output or "Déjà à jour" in output:
            await send("✅ Déjà à jour — pas de redémarrage nécessaire.")
            return
        await send(f"⬇️ Mise à jour récupérée :\n{output[-800:]}\n♻️ Je redémarre (launchd me relance)...")
        # Met à jour le SDK au passage (best effort, silencieux).
        pip = REPO_ROOT / "hybrid-assistant" / ".venv" / "bin" / "pip"
        if pip.exists():
            p = await asyncio.create_subprocess_exec(
                str(pip), "install", "--quiet", "--upgrade", "claude-agent-sdk",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.communicate()
        await self.close()
        sys.exit(0)  # KeepAlive relance le service avec le nouveau code

    # ── Briefing quotidien ────────────────────────────────────────────────

    async def briefing_loop(self) -> None:
        if not BRIEFING_TIME:
            return
        try:
            hh, mm = (int(x) for x in BRIEFING_TIME.split(":"))
        except ValueError:
            await send(f"⚠️ HYBRID_BRIEFING invalide ({BRIEFING_TIME!r}) — attendu HH:MM.")
            return
        while True:
            now = dt.datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            async with self.turn_lock:
                await self.maybe_expire_session()
                try:
                    reply = await self.run_turn(BRIEFING_PROMPT, kind="briefing")
                    await send("🌅 Briefing du jour\n\n" + reply)
                except Exception as exc:
                    await send(f"⚠️ Briefing impossible : {exc}")
                    await self.start_session()

    # ── Boucle principale ─────────────────────────────────────────────────

    async def handle_message(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()

        if text == "/help" or text == "/start":
            await send(HELP_TEXT)
            return
        if text == "/new":
            await self.start_session()
            await send("🆕 Nouvelle conversation.")
            return
        if text == "/cost":
            await send(
                f"Coût de la session : ${self.totals['cost']:.4f} "
                f"(plafond ${MAX_BUDGET:.2f})"
            )
            return
        if text == "/stats":
            await send(summarize_telemetry(days=7))
            return
        if text == "/update":
            await self.cmd_update()
            return

        async with self.turn_lock:
            if await self.maybe_expire_session():
                await send("♻️ Session réinitialisée après une longue inactivité (contexte remis à zéro).")
            prompt, kind = await self.prompt_from_message(msg)
            if prompt is None:
                return
            try:
                reply = await self.run_turn(prompt, kind=kind)
            except Exception as exc:  # session cassée : on la recrée
                await send(f"⚠️ Erreur : {exc}\nJe repars sur une session neuve.")
                await self.start_session()
                return
            await send(reply)

    async def run(self) -> None:
        await self.start_session()
        extras = []
        if BRIEFING_TIME:
            extras.append(f"briefing à {BRIEFING_TIME}")
        if WHISPER_CMD:
            extras.append("vocaux activés")
        suffix = f" ({', '.join(extras)})" if extras else ""
        await send(
            "🤖 Assistant hybride en ligne (executor Sonnet 5 + advisor Fable 5)"
            + suffix + "\n/help pour les commandes."
        )
        briefing = asyncio.create_task(self.briefing_loop())
        offset: int | None = None
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
                    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                        continue  # ignore tout chat non autorisé
                    if not (msg.get("text") or msg.get("photo") or msg.get("document")
                            or msg.get("voice") or msg.get("audio") or msg.get("caption")):
                        continue
                    await self.handle_message(msg)
        finally:
            briefing.cancel()
            await self.close()


def main() -> None:
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis.")
    asyncio.run(Bridge().run())


if __name__ == "__main__":
    main()
