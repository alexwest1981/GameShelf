#!/usr/bin/env bash
# Install GameShelf into the Omarchy shell plugin directory.
#
# The repo owns the sources; the plugin folder is a build artifact. That is
# deliberate: every file change inside a plugin folder makes the shell reload
# the plugin, so editing here would make the bar blink while you work.
#
#   bash install.sh            # copy files, validate, enable
#   bash install.sh --no-enable
set -euo pipefail

PLUGIN_ID=io.github.alexwest1981.gameshelf
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

FILES=(manifest.json BarWidget.qml Panel.qml scan-games.py)

echo "== installerar $PLUGIN_ID -> $DEST"
mkdir -p "$DEST"
for f in "${FILES[@]}"; do
  [ -f "$SRC/$f" ] || { echo "  saknas: $f"; exit 1; }
  install -m 0644 "$SRC/$f" "$DEST/$f"
  echo "  $f"
done
# The helper must be runnable by hand too (the README documents the manual scan).
chmod 0755 "$DEST/scan-games.py"

echo "== validerar"
if omarchy plugin validate "$DEST"; then
  echo "  manifest ok"
else
  echo "  VALIDERING MISSLYCKADES"; exit 1
fi

if [ "${1:-}" != "--no-enable" ]; then
  echo "== aktiverar i baren"
  omarchy plugin enable "$PLUGIN_ID" || true
  omarchy plugin list --json | python3 -c "
import json, sys
plugin = sys.argv[1]
for p in json.load(sys.stdin):
    if p.get('id') == plugin:
        print('  %s  enabled=%s' % (p.get('id'), p.get('enabled')))
        break
else:
    print('  %s hittades inte i listan' % plugin)
" "$PLUGIN_ID"
fi

echo "== klart. Ändra källor/mappar med:"
echo "   omarchy bar set $PLUGIN_ID folders \"~/Downloads,~/Games\""
echo "   omarchy bar set $PLUGIN_ID sources \"steam,heroic\""
echo "   (kör install.sh igen efter git pull)"
