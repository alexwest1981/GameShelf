#!/bin/sh
# Start a bare Windows game with Wine, in a Wine prefix of its own.
#
#   game-wine.sh /path/to/Game.exe [game args...]        start the game
#   game-wine.sh --prefix /path/to/Game.exe              print its prefix path
#   game-wine.sh --setup /path/to/Game.exe vcrun2022     winetricks into it
#
# One prefix per game folder, under ~/.local/share/gameshelf/wine/<folder>-<hash>,
# so a winetricks tweak (or a 32-bit game) in one prefix cannot break the next.
# Nothing is written into the game folder, and no launcher owns the game: the
# prefix is deletable on its own.
#
# The prefix starts bare. A game whose launcher stub wants a runtime says so in a
# dialog; give it one with --setup, once.
#
# Set GAMESHELF_WINE_DIR to put the prefixes somewhere else.
set -eu

mode=run
case "${1:-}" in
    --prefix) mode=prefix; shift ;;
    --setup)  mode=setup;  shift ;;
    --selftest) mode=selftest ;;
esac

if [ "$mode" = selftest ]; then
    # The one thing that broke silently in the live run: wine does not create the
    # folder above the prefix, so it has to exist before the game is started.
    tmp=$(mktemp -d)
    mkdir -p "$tmp/dir"          # readlink -f needs every parent to exist
    p=$(GAMESHELF_WINE_DIR="$tmp/wine" "$0" --prefix "$tmp/dir/Game.exe")
    case "$p" in
        "$tmp/wine"/*) ok="prefix is under the configured dir" ;;
        *) ok="" ;;
    esac
    if [ -n "$ok" ] && [ -d "$(dirname "$p")" ]; then
        echo "selftest ok: $p"
    else
        echo "selftest FAILED: parent of $p was not created" >&2
        rm -rf "$tmp"
        exit 1
    fi
    rm -rf "$tmp"
    exit 0
fi

exe=$(readlink -f "$1")
shift
dir=$(dirname "$exe")

for tool in wine md5sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "game-wine.sh: $tool is not installed" >&2; exit 1; }
done

# A repack drops the game into a container folder; name the prefix after the
# game, not after "game".
name=$(basename "$dir")
case "$name" in
    game|games|bin|x64|x86|win64|win32|data|files|dist|release)
        name=$(basename "$(dirname "$dir")") ;;
esac

key=$(printf '%s' "$dir" | md5sum | cut -c1-8)
slug=$(printf '%s' "$name" | tr -cs 'A-Za-z0-9._-' '-' | cut -c1-40)
export WINEPREFIX="${GAMESHELF_WINE_DIR:-$HOME/.local/share/gameshelf/wine}/$slug-$key"
# Wine makes the prefix but not the folder above it, and a missing parent is a
# silent no-launch ("wine: chdir to <prefix>: No such file or directory").
mkdir -p "$(dirname "$WINEPREFIX")"

if [ "$mode" = prefix ]; then
    printf '%s\n' "$WINEPREFIX"
    exit 0
fi

if [ "$mode" = setup ]; then
    command -v winetricks >/dev/null 2>&1 || { echo "game-wine.sh: winetricks is not installed" >&2; exit 1; }
    [ -d "$WINEPREFIX" ] || wineboot -i >/dev/null 2>&1 || true   # a fresh prefix first
    exec winetricks -q "$@"
fi

export WINEDEBUG="${WINEDEBUG:--all}"

# Games read their data files relative to their own folder.
cd "$dir"
exec wine "$exe" "$@"
