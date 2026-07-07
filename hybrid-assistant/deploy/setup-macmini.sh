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

# 4. Service permanent (optionnel) : REPL toujours disponible dans tmux,
#    relancé au démarrage de la machine par launchd.
if command -v tmux >/dev/null 2>&1; then
  cat <<EOF

Pour un service permanent :
  sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/com.hybrid-assistant.plist" \\
    > ~/Library/LaunchAgents/com.hybrid-assistant.plist
  launchctl load ~/Library/LaunchAgents/com.hybrid-assistant.plist

Puis, à tout moment (localement ou en SSH) :
  tmux attach -t hybrid-assistant
EOF
else
  echo "ℹ️  Installe tmux (brew install tmux) pour le mode service permanent."
fi
