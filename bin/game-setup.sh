#!/bin/sh
# Turn a download into a game: unpack a repack, or run a game installer.
#
#   game-setup.sh unpack  <archive>              .rar/.zip/.7z -> ~/Games
#   game-setup.sh install <installer.exe>        Proton (or wine) in a prefix of its own
#   game-setup.sh install --verbs dotnet48 <installer.exe>   same, with .NET first
#   game-setup.sh remove  <archive> --trash|--delete
#   game-setup.sh --selftest
#
# Progress and results go to one state file per task in
# $GAMESHELF_STATE_DIR (default ~/.local/share/gameshelf/tasks), which
# scan-games.py reads to show status and to enable Launch. Key/value lines, not
# JSON: that keeps this script dependency-free and the scanner already parses
# text. The file lives and dies with the task, so it also survives closing the
# panel mid-unpack (a 16 GB archive takes minutes).
#
# Set GAMESHELF_GAMES_DIR to unpack somewhere else than ~/Games.
set -eu

HERE=$(dirname "$(readlink -f "$0")")
WINE_RUN="$HERE/game-wine.sh"
GAMES_DIR=${GAMESHELF_GAMES_DIR:-$HOME/Games}
STATE_DIR=${GAMESHELF_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/gameshelf/tasks}

say() {  # say <message>: a desktop notification, if this box has one
    if [ -n "${GAMESHELF_QUIET:-}" ]; then return 0; fi
    command -v notify-send >/dev/null 2>&1 || return 0
    notify-send -a GameShelf GameShelf "$1" 2>/dev/null || true
}

write_state() {  # write_state <state> <kind> <title> [extra key=value...]
    state=$1; kind=$2; title=$3; shift 3
    mkdir -p "$STATE_DIR"
    {
        printf 'state=%s\nkind=%s\ntitle=%s\ntask=%s\nsince=%s\n' \
            "$state" "$kind" "$title" "$task" "$(date +%s)"
        for line in "$@"; do printf '%s\n' "$line"; done
    } > "$state_file.tmp"
    mv "$state_file.tmp" "$state_file"
}

unpack_into() {  # unpack_into <archive> <dir>
    case "$1" in
        *.rar) unrar x -o+ -idq "$1" "$2/" ;;   # 7z cannot read RAR5, unrar can
        *)     7z x -y -o"$2" "$1" >/dev/null ;;
    esac
}

unpack() {
    title=$(basename "$archive"); title=${title%.*}
    mkdir -p "$GAMES_DIR"
    stage="$GAMES_DIR/.gameshelf-unpack.$$"    # not $tmp: the selftest owns that
    rm -rf "$stage"; mkdir -p "$stage"
    write_state running unpack "$title" "dest=$GAMES_DIR"
    say "Unpacking $title…"
    if ! unpack_into "$archive" "$stage"; then
        rm -rf "$stage"                      # the archive is untouched: nothing lost
        write_state failed unpack "$title" "dest=$GAMES_DIR" "error=extract failed"
        say "Could not unpack $title"
        exit 1
    fi
    # A repack carries one top folder; keep its name, so ~/Games looks exactly
    # like a repack unpacked by hand. Anything else moves in as the stem.
    top=
    [ "$(ls -A "$stage" | wc -l)" -eq 1 ] && top=$(ls -A "$stage") && [ -d "$stage/$top" ] || top=
    if [ -n "$top" ]; then
        target=$GAMES_DIR/$top
        [ -e "$target" ] && target="$target (2)"
        mv "$stage/$top" "$target"
        rmdir "$stage" 2>/dev/null || true
    else
        target=$GAMES_DIR/$title
        n=2
        while [ -e "$target" ]; do target="$GAMES_DIR/$title ($n)"; n=$((n + 1)); done
        mv "$stage" "$target"
    fi
    {
        printf 'state=ready\nkind=unpack\ntitle=%s\ntask=%s\ndest=%s\nsince=%s\n' \
            "$title" "$task" "$target" "$(date +%s)"
    } > "$state_file.tmp"
    mv "$state_file.tmp" "$state_file"
    say "$title is ready"
}

find_games() {  # find_games <prefix dir>: likely game executables, biggest first
    find "$1" -maxdepth 7 -type f -iname '*.exe' 2>/dev/null \
        | grep -viE '/(windows|windows\.old|ProgramData|users/[^/]+/(AppData|Desktop|Documents|Downloads|Saved Games))' \
        | grep -viE 'unins|redist|vcrun|dxsetup|dxwebsetup|oalinst|dotnet|crashhandler|crashreport|errorreport|updater|notification_helper|steamclient_loader|install|setup' \
        | grep -viE '/(Steam|Internet Explorer|Windows Media Player|Common Files)/' \
        | grep -viE '/(steam|steamwebhelper|gameoverlayui|iexplore|wmplayer|wineboot|explorer|cmd|regedit|notepad|wordpad|winhlp32)\.exe$' \
        | while IFS= read -r exe; do printf '%s\t%s\n' "$(stat -c %s "$exe")" "$exe"; done \
        | sort -rn | head -3 | cut -f2-
}

install() {
    title=$(basename "$archive"); title=${title%.*}
    prefix=$("$WINE_RUN" --proton --prefix "$archive")
    mkdir -p "$STATE_DIR"
    # A prefix that already holds a game — a run that ended non-zero after a
    # successful extract — is adopted instead of installing all of it again.
    games=$(find_games "$prefix") || games=
    if [ -n "$games" ]; then
        write_ready
        say "$title is ready"
        return 0
    fi
    # A game whose installer (or the game itself) wants a runtime: name the
    # winetricks verbs in GAMESHELF_VERBS and they land in this same prefix
    # before the installer runs — unquoted on purpose, verbs are space-separated.
    if [ -n "${GAMESHELF_VERBS:-}" ]; then
        write_state running install "$title" "prefix=$prefix"
        say "Preparing $title…"
        # shellcheck disable=SC2086
        if ! setup_log=$("$WINE_RUN" --proton --setup --in "$prefix" "$archive" $GAMESHELF_VERBS 2>&1); then
            # Say what winetricks said: a busy prefix and a verb that does not
            # apply here look identical otherwise (measured: two runs in one
            # prefix fail in 10 s with nothing to show for it).
            error=$(printf '%s\n' "$setup_log" | tr -d '\r' | grep -v '^[[:space:]]*$' \
                | grep -vE '^-+$' | tail -1 | cut -c1-160)
            [ -n "$error" ] || error="could not add $GAMESHELF_VERBS to the prefix"
            write_state failed install "$title" "prefix=$prefix" "error=$error"
            say "Could not prepare $title"
            exit 1
        fi
    fi
    # Inno Setup writes a real log here; an installer that does not know /LOG
    # ignores the argument.
    inno_log="$STATE_DIR/$(basename "$state_file" .state)-install.log"
    # A repack's default target is beside the setup file — and that file is on
    # Z:, so "{sd}\Games" means the root of the filesystem and Inno dies with
    # "Access denied". Point it inside the prefix instead (measured on FitGirl's
    # DOOM + DOOM II: /DIR is the difference between failure and a game).
    win_dir="C:\\Games\\$(printf '%s' "$title" | tr -d '\\/:*?"<>|')"
    write_state running install "$title" "prefix=$prefix" "dest=$win_dir"
    say "Installing $title…"
    rc=0
    "$WINE_RUN" --proton --in "$prefix" "$archive" "/LOG=Z:$inno_log" "/DIR=$win_dir" >/dev/null 2>&1 || rc=$?
    # The exit code is not the answer: a repack often ends non-zero after a
    # successful extract (its last page starts the game, and that child dies).
    # What counts is whether a game is in the prefix - measured on this box:
    # ReStory/Vanya installed fine and still exited non-zero.
    games=$(find_games "$prefix") || games=
    if [ -z "$games" ]; then
        error=$(log_tail "$inno_log")
        [ -n "$error" ] || error="installer exited $rc and left no game in the prefix"
        if needs=$(log_hint "$inno_log") && [ -n "$needs" ]; then
            write_state failed install "$title" "prefix=$prefix" "error=$error" "needs=$needs"
        else
            write_state failed install "$title" "prefix=$prefix" "error=$error"
        fi
        say "Installer for $title failed"
        exit 1
    fi
    write_ready
    say "$title is ready"
}

write_ready() {
    {
        printf 'state=ready\nkind=install\ntitle=%s\ntask=%s\nprefix=%s\nsince=%s\n' \
            "$title" "$task" "$prefix" "$(date +%s)"
        printf '%s\n' "$games" | sed 's/^/game=/'
    } > "$state_file.tmp"
    mv "$state_file.tmp" "$state_file"
}

# The line an installer actually wrote that explains a failure, so the row says
# something useful: Inno's "Log closed." is the *last* line and means nothing.
log_tail() {
    [ -f "$1" ] || return 0
    tr -d '\r' < "$1" \
        | grep -v '^[[:space:]]*$' \
        | grep -vE '^-+$' \
        | grep -viE 'Log (opened|closed)|Deinitializing Setup|Rolling back|Starting the (un)?installation|User chose|Initializing Setup' \
        | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:.]+[[:space:]]+//' \
        | awk 'tolower($0) ~ /unable to|cannot|missing|not found|requires|denied|error [0-9]/ { print; done = 1; exit }
               want { print; done = 1; exit }
               tolower($0) ~ /fatal|error/ { want = 1; next }
               { last = $0 }
               END { if (!done && last != "") print last }' \
        | cut -c1-200
}

# What the log says the game is missing, as winetricks verbs — the one thing
# worth a button on the row. Only the framework is detected: it is what fails
# here in practice (measured: Wine-Mono is not enough for a repack's setup.exe).
log_hint() {
    [ -f "$1" ] || return 0
    if tr -d '\r' < "$1" | grep -qiE '\.net framework|dotnet|clr error|netfx'; then
        printf '%s\n' dotnet48
    fi
}

remove() {
    case "${1:-}" in
        --trash)  gio trash "$archive" ;;
        --delete) rm -f -- "$archive" ;;
        *) echo "game-setup.sh remove: say --trash or --delete" >&2; exit 2 ;;
    esac
    rm -f "$state_file"
    say "Removed $(basename "$archive")"
}

selftest() {
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    # The paths are already expanded at load time, so override the variables.
    GAMES_DIR="$tmp/Games"
    STATE_DIR="$tmp/state"
    GAMESHELF_QUIET=1
    XDG_DATA_HOME="$tmp/xdg"          # gio trash writes here, not in the real home
    export GAMESHELF_QUIET XDG_DATA_HOME

    python3 - "$tmp/My.Game.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("My.Game/game/My.Game.exe", "MZ")
    z.writestr("My.Game/game/My.Game_Data/level0", "x")
PY
    set -- unpack "$tmp/My.Game.zip"; mode=$1; archive=$(readlink -f "$2")
    task=$archive; state_file="$STATE_DIR/$(printf '%s' "$task" | md5sum | cut -c1-16).state"
    unpack
    [ -x "$tmp/Games/My.Game/game/My.Game.exe" ] || [ -f "$tmp/Games/My.Game/game/My.Game.exe" ] \
        || { echo "selftest FAILED: game not in ~/Games"; exit 1; }
    grep -qx 'state=ready' "$state_file" || { echo "selftest FAILED: state is not ready"; exit 1; }
    grep -qx "dest=$tmp/Games/My.Game" "$state_file" || { echo "selftest FAILED: dest is not the game dir"; exit 1; }

    remove --trash
    [ ! -e "$archive" ] || { echo "selftest FAILED: archive still there"; exit 1; }
    [ ! -e "$state_file" ] || { echo "selftest FAILED: state file left behind"; exit 1; }
    # Where the trash is, is gio's business (a different mount gets its own
    # .Trash-<uid>), so only the exit status and the removal are asserted here.

    # --delete is the other answer the panel can send: no trash involved.
    python3 - "$tmp/Other.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("Other/game/Other.exe", "MZ")
PY
    set -- unpack "$tmp/Other.zip"; archive=$(readlink -f "$2")
    task=$archive; state_file="$STATE_DIR/$(printf '%s' "$task" | md5sum | cut -c1-16).state"
    unpack
    remove --delete
    [ ! -e "$archive" ] || { echo "selftest FAILED: --delete left the archive"; exit 1; }
    [ ! -e "$state_file" ] || { echo "selftest FAILED: --delete left the state file"; exit 1; }

    # Candidate search: only the game, not the uninstaller or Windows' own exes —
    # and not the plumbing Proton puts in every prefix (measured: a fresh prefix
    # holds Program Files (x86)/Steam/steam.exe, and that was offered as the game).
    mkdir -p "$tmp/pfx/drive_c/Games/Shelf/game" "$tmp/pfx/drive_c/windows/system32" \
             "$tmp/pfx/drive_c/Program Files (x86)/Steam" "$tmp/pfx/drive_c/Program Files/Internet Explorer"
    : > "$tmp/pfx/drive_c/Games/Shelf/game/Shelf.exe"
    : > "$tmp/pfx/drive_c/Games/Shelf/unins000.exe"
    : > "$tmp/pfx/drive_c/windows/system32/edit.exe"
    : > "$tmp/pfx/drive_c/Program Files (x86)/Steam/steam.exe"
    : > "$tmp/pfx/drive_c/Program Files/Internet Explorer/iexplore.exe"
    found=$(find_games "$tmp/pfx")
    [ "$found" = "$tmp/pfx/drive_c/Games/Shelf/game/Shelf.exe" ] \
        || { echo "selftest FAILED: candidate search found '$found'"; exit 1; }

    # A prefix that already holds a game is adopted instead of installing it all
    # over again: the copy gets a stub game-wine.sh that only answers --prefix.
    mkdir -p "$tmp/adopt/drive_c/Games/Adopted/game" "$tmp/fake/bin" "$tmp/inst"
    : > "$tmp/adopt/drive_c/Games/Adopted/game/AdoptedGame.exe"
    : > "$tmp/inst/Setup.exe"
    cp "$(readlink -f "$0")" "$tmp/fake/bin/game-setup.sh"
    printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "$FAKE_CALLS"\nfor a in "$@"; do [ "$a" = --prefix ] && echo "$FAKE_PREFIX"; done\nif [ "${2:-}" = --setup ] && [ -n "${FAKE_SETUP_FAIL:-}" ]; then echo "winetricks: prefix is busy" >&2; exit 1; fi\nexit 0\n' > "$tmp/fake/bin/game-wine.sh"
    chmod +x "$tmp/fake/bin/game-wine.sh"
    GAMESHELF_STATE_DIR="$tmp/state-adopt" GAMESHELF_QUIET=1 FAKE_PREFIX="$tmp/adopt" FAKE_CALLS="$tmp/calls" \
        sh "$tmp/fake/bin/game-setup.sh" install "$tmp/inst/Setup.exe"
    adopt_state=$(ls "$tmp/state-adopt"/*.state)
    grep -qx 'state=ready' "$adopt_state" || { echo "selftest FAILED: adopt did not end ready"; exit 1; }
    grep -q 'AdoptedGame.exe' "$adopt_state" || { echo "selftest FAILED: adopt lost the game"; exit 1; }

    # GAMESHELF_VERBS puts the runtime into the same prefix before the installer,
    # so the prefix is prepared with --setup in Proton mode.
    mkdir -p "$tmp/empty/drive_c/Games" "$tmp/inst2"
    : > "$tmp/inst2/Setup.exe"
    GAMESHELF_STATE_DIR="$tmp/state-verbs" GAMESHELF_QUIET=1 FAKE_PREFIX="$tmp/empty" FAKE_CALLS="$tmp/calls" \
        GAMESHELF_VERBS="dotnet48 vcrun2022" sh "$tmp/fake/bin/game-setup.sh" install "$tmp/inst2/Setup.exe" \
        || true   # no game in the prefix, so it ends failed on purpose
    grep -q -- '--proton --setup --in .* dotnet48 vcrun2022' "$tmp/calls" \
        || { echo "selftest FAILED: GAMESHELF_VERBS never reached --setup"; exit 1; }

    # The panel sends the verbs as argv, and a log that names the framework makes
    # the failure carry needs=dotnet48 — that is what the row's button reads.
    mkdir -p "$tmp/inst3" "$tmp/state-hint"; : > "$tmp/inst3/Setup.exe"
    : > "$tmp/calls3"
    GAMESHELF_STATE_DIR="$tmp/state-hint" GAMESHELF_QUIET=1 FAKE_PREFIX="$tmp/empty" FAKE_CALLS="$tmp/calls3" \
        sh "$tmp/fake/bin/game-setup.sh" install --verbs dotnet48 "$tmp/inst3/Setup.exe" || true
    grep -q -- '--proton --setup --in .* dotnet48$' "$tmp/calls3" \
        || { echo "selftest FAILED: install --verbs did not reach --setup"; exit 1; }
    h=$(printf '%s' "$(readlink -f "$tmp/inst3/Setup.exe")" | md5sum | cut -c1-16)
    printf 'Setup requires .NET Framework 4.8 or later\n' > "$tmp/state-hint/$h-install.log"
    GAMESHELF_STATE_DIR="$tmp/state-hint" GAMESHELF_QUIET=1 FAKE_PREFIX="$tmp/empty" FAKE_CALLS="$tmp/calls3" \
        sh "$tmp/fake/bin/game-setup.sh" install "$tmp/inst3/Setup.exe" || true
    grep -qx 'needs=dotnet48' "$tmp/state-hint/$h.state" \
        || { echo "selftest FAILED: a .NET log did not mark the state"; exit 1; }
    # A setup step that fails says why, instead of a generic line.
    GAMESHELF_STATE_DIR="$tmp/state-fail" GAMESHELF_QUIET=1 FAKE_PREFIX="$tmp/empty" FAKE_CALLS="$tmp/calls3" \
        FAKE_SETUP_FAIL=1 sh "$tmp/fake/bin/game-setup.sh" install --verbs dotnet48 "$tmp/inst3/Setup.exe" || true
    grep -q 'prefix is busy' "$tmp/state-fail"/*.state \
        || { echo "selftest FAILED: a failed setup step lost its reason"; exit 1; }

    # What a failed install says: the reason, not Inno's "Log closed." — and a
    # repack is never told to extract into the root of Z:.
    printf '2026-11-24 14:54:07.865   Creating directory: Z:\\Games\n2026-11-24 14:54:07.865   Fatal exception during installation process (Exception):\n                          Setup was unable to create the directory "Z:\\Games".\n2026-11-24 14:54:14.628   Log closed.\n' > "$tmp/fitgirl.log"
    said=$(log_tail "$tmp/fitgirl.log")
    case "$said" in
        *"unable to create"*) ;;
        *) echo "selftest FAILED: log_tail said '$said'"; exit 1 ;;
    esac
    grep -q -- '/DIR=C:\\Games' "$tmp/calls3" \
        || { echo "selftest FAILED: the installer got no /DIR inside the prefix"; exit 1; }
    echo "selftest ok: unpack, state, trash, candidates, adopt, verbs, 2 reasons, /DIR"
}

mode=${1:-}
case "$mode" in
    unpack|install|remove|--selftest) ;;
    *) sed -n '2,9p' "$0" >&2; exit 2 ;;
esac
[ "$mode" = --selftest ] && { selftest; exit 0; }

# install may name the runtime the prefix needs, as argv so the panel can send
# it: install --verbs dotnet48 <installer>.
if [ "$mode" = install ] && [ "${2:-}" = --verbs ]; then
    GAMESHELF_VERBS="${3:-}"
    set -- "$mode" "${4:-}"     # mode stays at $1, the installer at $2
fi
export GAMESHELF_VERBS="${GAMESHELF_VERBS:-}"

archive=$(readlink -f "${2:-.}" 2>/dev/null) || archive=
[ -e "$archive" ] || { echo "game-setup.sh: no such file: ${2:-}" >&2; exit 1; }
task=$archive
state_file="$STATE_DIR/$(printf '%s' "$task" | md5sum | cut -c1-16).state"

case "$mode" in
    unpack)  unpack ;;
    install) install ;;
    remove)  remove "${3:-}" ;;
esac
