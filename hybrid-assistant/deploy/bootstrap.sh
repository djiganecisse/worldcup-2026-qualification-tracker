#!/bin/bash
# Installation « zéro saisie » de l'assistant hybride sur un Mac.
# Une seule commande à coller dans Terminal :
#
#   curl -fsSL https://raw.githubusercontent.com/djiganecisse/worldcup-2026-qualification-tracker/main/hybrid-assistant/deploy/bootstrap.sh | bash
#
# Le script clone/met à jour le repo, demande le token du bot (un collage),
# détecte automatiquement le chat_id, écrit ~/.hybrid-assistant.env et
# installe le service launchd via setup-macmini.sh.
set -euo pipefail

REPO_URL="https://github.com/djiganecisse/worldcup-2026-qualification-tracker.git"
APP_ROOT="$HOME/worldcup-2026-qualification-tracker"
ENV_FILE="$HOME/.hybrid-assistant.env"

say() { printf '\n%s\n' "$*"; }

say "== Assistant hybride — installation automatique =="

# ── 1. Code ──────────────────────────────────────────────────────────────
if [ -d "$APP_ROOT/.git" ]; then
  say "→ Mise à jour du repo existant ($APP_ROOT)"
  git -C "$APP_ROOT" fetch origin
  git -C "$APP_ROOT" checkout main
  git -C "$APP_ROOT" pull origin main
else
  say "→ Clonage du repo dans $APP_ROOT"
  git clone "$REPO_URL" "$APP_ROOT"
fi

# ── 2. Bot Telegram ──────────────────────────────────────────────────────
# stdin est le pipe du curl : les read passent par /dev/tty.
if [ -f "$ENV_FILE" ] && grep -q '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE"; then
  say "✅ $ENV_FILE existe déjà — je le réutilise."
else
  say "📱 Dans Telegram : ouvre @BotFather, envoie /newbot et suis les étapes."
  printf 'Colle ici le token du bot puis Entrée : '
  read -r TOKEN </dev/tty
  TOKEN="${TOKEN// /}"

  ME_JSON=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" || true)
  BOT_USER=$(printf '%s' "$ME_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d['result']['username'] if d.get('ok') else '')
except Exception:
    print('')
")
  if [ -z "$BOT_USER" ]; then
    say "❌ Token invalide ou réseau indisponible. Relance le script."
    exit 1
  fi
  say "✅ Bot @${BOT_USER} reconnu."

  say "📱 Maintenant : envoie n'importe quel message à @${BOT_USER} dans Telegram."
  say "   Je détecte ton chat_id (2 minutes max)..."
  CHAT_ID=""
  for _ in $(seq 1 60); do
    CHAT_ID=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ids = [u['message']['chat']['id'] for u in d.get('result', []) if 'message' in u]
    print(ids[-1] if ids else '')
except Exception:
    print('')
")
    [ -n "$CHAT_ID" ] && break
    sleep 2
  done
  if [ -z "$CHAT_ID" ]; then
    say "❌ Aucun message reçu. Envoie un message au bot puis relance le script."
    exit 1
  fi
  say "✅ chat_id détecté : $CHAT_ID"

  umask 177
  {
    echo "TELEGRAM_BOT_TOKEN=$TOKEN"
    echo "TELEGRAM_CHAT_ID=$CHAT_ID"
  } > "$ENV_FILE"
  umask 022
fi

# ── 3. Identifiants Anthropic ────────────────────────────────────────────
if ! grep -q '^ANTHROPIC_API_KEY=' "$ENV_FILE" 2>/dev/null && ! command -v claude >/dev/null 2>&1; then
  say "🔑 Ni CLI 'claude' connecté ni clé API détectés."
  printf 'Colle une clé API Anthropic (sk-ant-..., créée sur platform.claude.com) : '
  read -r KEY </dev/tty
  echo "ANTHROPIC_API_KEY=${KEY// /}" >> "$ENV_FILE"
fi

# ── 4. Venv, test de fumée et service launchd ────────────────────────────
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
bash "$APP_ROOT/hybrid-assistant/deploy/setup-macmini.sh"

say "🎉 Terminé. Le bot doit t'avoir envoyé « 🤖 Assistant hybride en ligne » sur Telegram."
say "   Logs si besoin : /tmp/hybrid-assistant-telegram.log"
