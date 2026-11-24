#!/bin/sh
# Start a Windows game with system Wine, or with the Proton build installed on
# this machine, in a prefix of its own.
#
#   game-wine.sh /path/to/Game.exe [game args...]        start the game
#   game-wine.sh --proton /path/Game.exe [args...]       same, through Proton
#   game-wine.sh --prefix /path/to/Game.exe              print its prefix path
#   game-wine.sh --setup /path/to/Game.exe vcrun2022     winetricks into it (Wine)
#   game-wine.sh --in /some/prefix /path/Game.exe        use a prefix given to you
#
# One prefix per game folder, under ~/.local/share/gameshelf/wine/<folder>-<hash>,
# so a winetricks tweak (or a 32-bit game) in one prefix cannot break the next.
# Nothing is written into the game folder, and no launcher owns the game: the
# prefix is deletable on its own.
#
# --proton is there because some installers and newer titles want DXVK and
# Proton's own runtime; it falls back to system Wine when no build is installed.
# Proton keeps its prefix inside the same directory ($data/pfx), so both runtimes
# take the same "prefix dir" and --in works for either.
#
# The prefix starts bare. A game whose launcher stub wants a runtime says so in a
# dialog; give it one with --setup, once.
#
# Set GAMESHELF_WINE_DIR to put the prefixes somewhere else, GAMESHELF_PROTON to
# point at one specific Proton build.
set -eu

mode=run
proton=0
given_prefix=
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)   mode=prefix; shift ;;
        --setup)    mode=setup;  shift ;;
        --selftest) mode=selftest; shift ;;
        --proton)   proton=1; shift ;;
        --in)       given_prefix=$2; shift 2 ;;
        --)         shift; break ;;
        *)          break ;;
    esac
done

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
    # A prefix handed in wins over the computed one (a game an installer made).
    p2=$(GAMESHELF_WINE_DIR="$tmp/wine" "$0" --in "$tmp/explicit" --prefix "$tmp/dir/Game.exe")
    [ "$p2" = "$tmp/explicit" ] || ok=""
    # --setup on a Proton prefix must land in $data/pfx (and use system wine:
    # Proton's own wine cannot run the .NET installer, measured).
    mkdir -p "$tmp/stub"
    printf '#!/bin/sh\necho "$WINEPREFIX|$WINE"\n' > "$tmp/stub/winetricks"
    chmod +x "$tmp/stub/winetricks"
    setup_out=$(PATH="$tmp/stub:$PATH" GAMESHELF_PROTON="$tmp/build" \
        "$0" --proton --setup --in "$tmp/pdata" "$tmp/dir/Game.exe" dotnet48 2>&1)
    [ "$setup_out" = "$tmp/pdata/pfx|" ] || ok=""
    if [ -n "$ok" ] && [ -d "$(dirname "$p")" ]; then
        echo "selftest ok: $p | --in $p2 | --setup $setup_out"
    else
        echo "selftest FAILED: prefix resolution is wrong (got '$p' / '$p2')" >&2
        rm -rf "$tmp"
        exit 1
    fi
    rm -rf "$tmp"
    exit 0
fi

[ -n "${1:-}" ] || { echo "game-wine.sh: no executable given" >&2; exit 2; }
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

if [ -n "$given_prefix" ]; then
    data=$given_prefix
else
    key=$(printf '%s' "$dir" | md5sum | cut -c1-8)
    slug=$(printf '%s' "$name" | tr -cs 'A-Za-z0-9._-' '-' | cut -c1-40)
    data="${GAMESHELF_WINE_DIR:-$HOME/.local/share/gameshelf/wine}/$slug-$key"
fi
# Wine makes the prefix but not the folder above it, and a missing parent is a
# silent no-launch ("wine: chdir to <prefix>: No such file or directory").
mkdir -p "$(dirname "$data")" "$data"

proton_build() {  # the build --proton would run, or nothing
    if [ -n "${GAMESHELF_PROTON:-}" ]; then
        printf '%s\n' "$GAMESHELF_PROTON"
        return 0
    fi
    # Name order puts "Proton - Experimental" before "Proton 9.0"; any
    # installed build will do, so pin one with GAMESHELF_PROTON only if the
    # pick is ever wrong.
    for candidate in "$HOME/.local/share/Steam/steamapps/common/Proton"* \
                     "$HOME/.steam/steam/steamapps/common/Proton"*; do
        [ -x "$candidate/proton" ] && { printf '%s\n' "$candidate"; return 0; }
    done
    return 1
}

if [ "$mode" = prefix ]; then
    printf '%s\n' "$data"
    exit 0
fi

if [ "$mode" = setup ]; then
    command -v winetricks >/dev/null 2>&1 || { echo "game-wine.sh: winetricks is not installed" >&2; exit 1; }
    if [ "$proton" = 1 ]; then
        # Let Proton create its own prefix first: a Wine-made prefix in Proton's
        # slot works, but Proton then has to adopt it, and the button path would
        # hit exactly that on a fresh game.
        if [ ! -d "$data/pfx" ] && build=$(proton_build) && [ -x "$build/proton" ]; then
            STEAM_COMPAT_CLIENT_INSTALL_PATH="${STEAM_COMPAT_CLIENT_INSTALL_PATH:-$HOME/.local/share/Steam}" \
            STEAM_COMPAT_DATA_PATH="$data" "$build/proton" run wineboot.exe >/dev/null 2>&1 || true
            # Proton's wineserver outlives that run; system wine then sees a busy
            # prefix and winetricks gives up in seconds (measured). Shut it down
            # with Proton's own wineserver so the protocols match, then wait.
            WINEPREFIX="$data/pfx" "$build/files/bin/wineserver" -k >/dev/null 2>&1 || true
            sleep 1
        fi
        # Proton keeps its prefix in $data/pfx. Proton's own wine cannot run the
        # .NET installer (measured: dotNetFx40 ends status 67, no v4 key at all,
        # mscoree=d changes nothing); system wine installs into the same prefix
        # and Proton reads the result (Release 528049). The game still starts
        # under Proton, so DXVK is intact.
        # shortcut: two 11.x wines share one prefix; if a verb ever half-applies,
        # use a bare-Wine prefix (no --proton) for that game instead.
        export WINEPREFIX="$data/pfx"
    else
        export WINEPREFIX=$data
        [ -d "$WINEPREFIX" ] || wineboot -i >/dev/null 2>&1 || true   # a fresh prefix first
    fi
    exec winetricks -q "$@"
fi

export WINEDEBUG="${WINEDEBUG:--all}"

# Games read their data files relative to their own folder.
cd "$dir"

if [ "$proton" = 1 ]; then
    if build=$(proton_build) && [ -x "$build/proton" ]; then
        export STEAM_COMPAT_CLIENT_INSTALL_PATH="${STEAM_COMPAT_CLIENT_INSTALL_PATH:-$HOME/.local/share/Steam}"
        export STEAM_COMPAT_DATA_PATH=$data
        # A game that hangs gives no hint by itself. GAMESHELF_PROTON_LOG=1 keeps
        # Proton's own log next to the prefixes, so the next one can be read.
        # Proton's setup_logging() bails out unless SteamGameId is set — that is
        # the whole reason an earlier attempt wrote no log at all.
        if [ -n "${GAMESHELF_PROTON_LOG:-}" ]; then
            export PROTON_LOG=1
            export PROTON_LOG_DIR="${GAMESHELF_PROTON_LOG_DIR:-$HOME/.local/share/gameshelf/proton-logs}"
            export SteamGameId="${GAMESHELF_STEAM_APP_ID:-0}"
            mkdir -p "$PROTON_LOG_DIR"
        fi
        exec "$build/proton" run "$exe" "$@"
    fi
    echo "game-wine.sh: no Proton build found, falling back to wine" >&2
fi

export WINEPREFIX=$data
exec wine "$exe" "$@"
