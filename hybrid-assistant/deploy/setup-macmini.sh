#!/bin/bash
# Installation / mise à jour de l'assistant hybride sur le Mac Mini.
# Usage : bash hybrid-assistant/deploy/setup-macmini.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$APP_DIR/.venv"

echo "== Assistant hybride — installation dans $APP_DIR =="

# 1. Environnement Python isolé
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip claude-agent-sdk

# 2. Vérification des identifiants (CLI Claude Code connecté ou clé API)
if [ -z "${ANTHROPIC_API_KEY:-}" ] && ! command -v claude >/dev/null 2>&1; then
  echo "⚠️  Ni ANTHROPIC_API_KEY ni le CLI 'claude' détectés."
  echo "   Installe Claude Code (https://claude.com/claude-code) et connecte-toi,"
  echo "   ou exporte ANTHROPIC_API_KEY avant de lancer l'assistant."
fi

# 3. Test de fumée
echo "== Test de fumée =="
"$VENV/bin/python" "$APP_DIR/assistant.py" --once "Réponds exactement: OK"

# 4. Service permanent — pont Telegram (recommandé)
ENV_FILE="$HOME/.hybrid-assistant.env"
if [ -f "$ENV_FILE" ]; then
  echo "== Installation du service Telegram (secrets: $ENV_FILE) =="
  mkdir -p ~/Library/LaunchAgents
  sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/com.hybrid-assistant-telegram.plist" \
    > ~/Library/LaunchAgents/com.hybrid-assistant-telegram.plist
  launchctl unload ~/Library/LaunchAgents/com.hybrid-assistant-telegram.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.hybrid-assistant-telegram.plist
  echo "✅ Service Telegram chargé — logs : /tmp/hybrid-assistant-telegram.log"
else
  cat <<EOF

Pour le service Telegram permanent, crée $ENV_FILE :
  TELEGRAM_BOT_TOKEN=123456:ABC-...
  TELEGRAM_CHAT_ID=123456789
  # HYBRID_AUTO=1            # optionnel : autorise Bash/Edit/Write
puis relance ce script.
EOF
fi

# 5. Alternative : REPL toujours disponible dans une session tmux.
if command -v tmux >/dev/null 2>&1; then
  cat <<EOF

Alternative REPL (tmux) :
  sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/com.hybrid-assistant.plist" \\
    > ~/Library/LaunchAgents/com.hybrid-assistant.plist
  launchctl load ~/Library/LaunchAgents/com.hybrid-assistant.plist
  tmux attach -t hybrid-assistant
EOF
fi
