#!/usr/bin/env python3
"""Scan this machine for installed games and print one JSON line.

Sources, each switchable in the plugin settings:

  steam    appmanifest_*.acf in every Steam library (libraryfolders.vdf lists
           them, so games on other drives are found too), minus Proton and
           the Steam Linux Runtimes, which are not games
  shortcuts  non-Steam games added to Steam (userdata/<id>/config/shortcuts.vdf),
           started through Steam so its Proton setup is the one that runs
  lutris   ~/.config/lutris/games/*.yml
  heroic   ~/.config/heroic/ side-loaded apps, Epic (legendary) and GOG
  desktop  .desktop entries with Categories=Game (native, /usr/share and
           Flatpak exports), minus the launchers of the other sources
  folders  executables that look like game launchers, up to DEPTH levels
           below each folder you configure

A download that is not a game yet shows up as a row that turns it into one: a
repack archive (.rar/.zip/.7z holding a starter) gets Unpack, a downloaded
installer (setup.exe, Install.exe) gets Install. bin/game-setup.sh does the work
and writes one <hash>.state file per task under
~/.local/share/gameshelf/tasks; the state is read here so a row can show status
and only offer Launch once the game really exists.

Usage:
  scan-games.py '{"sources":["steam","heroic","folders"],"folders":["~/Downloads"],"hidden":["steam:892970"]}'
  scan-games.py --selftest          # fixture tree in a temp dir, no real home

Output is a single JSON line:
  {"games":[{id,name,source,sourceLabel,detail,launch:[argv]}],"counts":{source:n},"errors":[..]}

`launch` is an argv list for Quickshell.execDetached(). Steam, Lutris and Heroic
all register a URI handler on this box (steam://, lutris://, heroic://), so
every launcher we do not own goes through xdg-open — one code path, no
per-launcher shell quoting.
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

DEPTH = 3  # how deep below a configured folder we look for a game launcher

SOURCE_LABELS = {
    "steam": "Steam",
    "shortcuts": "Steam shortcuts",
    "lutris": "Lutris",
    "heroic": "Heroic",
    "desktop": "Desktop",
    "folders": "Folders",
}

# Proton and the Steam Linux Runtimes live in steamapps like games do.
NOT_A_GAME = re.compile(r"steam linux runtime|proton|^steamworks", re.I)

# A file that looks like something you double-click to play. Note there is no
# bare ".sh" case: a repack folder is full of helper scripts (actions.sh,
# patch.sh) that would each become a "game".
LAUNCHER = re.compile(r"^(start|run|launch|play|game)|\.(AppImage|run)$", re.I)
SKIP_FILE = re.compile(
    r"\.(so|dll|py|desktop|txt|md|json|log|bat|zip|7z|tar|gz)$"
    r"|install|setup|uninstall|patch|update|config|fix|readme|actions|library",
    re.I,
)
# Something that ships inside a game folder but is never the game you came for.
NOT_THE_GAME = re.compile(
    r"unitycrashhandler|crashhandler|crashreport|errorreport|blizzarderror|unins"
    r"|vcredist|vc_redist|dxsetup|dxwebsetup|oalinst|dotnetfx|prereq|redist"
    r"|setup|install|updater|notification_helper|steamclient_loader",
    re.I,
)
# Folders a repack drops the game into; the title comes from further up.
GENERIC_DIR = re.compile(
    r"^(game|games|bin|x64|x86|win64|win32|data|files|dist|build|release|app|contents|windows|linux|macos"
    r"|setup|install|installer)$",
    re.I,
)
# The Wine starter, found next to this file so a renamed plugin folder still works.
WINE_RUN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "bin", "game-wine.sh")
# The unpack/install helper, next to it for the same reason.
GAME_SETUP = os.path.join(os.path.dirname(os.path.realpath(__file__)), "bin", "game-setup.sh")

# A download you have to unpack. What makes an archive a game rather than a
# texture or sound pack is a starter inside it: a Windows/Linux executable, a
# Godot .pck, an Unreal .pak. Measured on this box: 15 of 16 flagged archives in
# ~/Downloads were games, and no asset pack was flagged.
ARCHIVE = re.compile(r"\.(rar|zip|7z)$", re.I)
STARTER = re.compile(r"\.(exe|AppImage|pck|pak)$|_data/", re.I)
# Something you run once to get a game: FitGirl/KaOs/Vanya repacks, a GOG setup.
INSTALLER = re.compile(r"(^|[\s._-])(setup|install|installer)", re.I)
ARCHIVE_TIMEOUT = 60   # seconds to list one archive: a cold 16 GB rar takes ~20,
                       # and this runs in the background pass, not in the scan


def home_of(opts):
    return os.path.expanduser(opts.get("home") or "~")


def run(argv, timeout=10):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


# ── Steam ────────────────────────────────────────────────────────────────────

def steam_libraries(home):
    """Every library path in libraryfolders.vdf, plus the defaults."""
    roots = [f"{home}/.steam/steam/steamapps", f"{home}/.local/share/Steam/steamapps"]
    libs = set()
    for root in roots:
        vdf = os.path.join(root, "libraryfolders.vdf")
        if os.path.exists(vdf):
            text = open(vdf, errors="replace").read()
            libs.update(m.replace("\\\\", "/") for m in re.findall(r'"path"\s+"([^"]+)"', text))
        if os.path.isdir(root):
            libs.add(os.path.dirname(root))
    return libs


def steam_icon(home, appid):
    """Steam's own art for an appid: the portrait capsule first (a square crop of
    it reads like an app icon), then the wide header. Nothing is downloaded."""
    for client in (f"{home}/.local/share/Steam", f"{home}/.steam/steam"):
        for name in ("library_600x900.jpg", "header.jpg"):
            path = f"{client}/appcache/librarycache/{appid}/{name}"
            if os.path.exists(path):
                return path
    return ""


def scan_steam(home, hidden):
    games, seen = [], set()
    for lib in sorted(steam_libraries(home)):
        for man in sorted(glob.glob(f"{lib}/steamapps/appmanifest_*.acf")):
            man = os.path.realpath(man)
            if man in seen:
                continue
            seen.add(man)
            text = open(man, errors="replace").read()
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if not (appid and name):
                continue
            name = name.group(1)
            if NOT_A_GAME.search(name):
                continue
            gid = f"steam:{appid.group(1)}"
            if gid in hidden:
                continue
            entry = {
                "id": gid,
                "name": name,
                "source": "steam",
                "detail": f"appid {appid.group(1)}",
                "launch": ["xdg-open", f"steam://rungameid/{appid.group(1)}"],
            }
            icon = steam_icon(home, appid.group(1))
            if icon:
                entry["iconFile"] = icon
            games.append(entry)
    return games


# ── Steam non-Steam shortcuts ────────────────────────────────────────────────

def read_vdf(data, pos=0):
    """Parse the binary KeyValues Steam writes for shortcuts.

    ponytail: just the three types Valve emits here (object, string, int32);
    an unknown type stops the walk instead of guessing at the rest.
    """
    out = {}
    while pos < len(data):
        kind = data[pos]
        pos += 1
        if kind == 0x08:                      # end of this object
            return out, pos
        end = data.index(b"\x00", pos)
        key = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        if kind == 0x00:                      # nested object
            value, pos = read_vdf(data, pos)
        elif kind == 0x01:                    # string
            end = data.index(b"\x00", pos)
            value = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
        elif kind == 0x02:                    # int32
            value = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        else:
            return out, len(data)
        out[key] = value
    return out, pos


def scan_shortcuts(home, hidden):
    """Games added to Steam by hand. Steam keeps the launchable appid in the
    entry, so no appid has to be recomputed from the exe path."""
    games, seen = [], set()
    for root in (f"{home}/.steam/steam", f"{home}/.local/share/Steam"):
        for path in sorted(glob.glob(f"{root}/userdata/*/config/shortcuts.vdf")):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                entries = read_vdf(open(real, "rb").read())[0].get("shortcuts") or {}
            except Exception:
                continue
            for key in sorted(entries, key=lambda k: int(k) if str(k).isdigit() else 0):
                entry = entries[key]
                name = str(entry.get("AppName") or "").strip()
                appid = entry.get("appid")
                if not name or not isinstance(appid, int):
                    continue
                gid = f"shortcut:{appid}"
                if gid in hidden:
                    continue
                exe = os.path.basename(str(entry.get("Exe") or "").strip('"'))
                games.append({
                    "id": gid,
                    "name": re.sub(r"\.exe$", "", name, flags=re.I),
                    "source": "shortcuts",
                    "detail": f"Steam shortcut · {exe}" if exe else "Steam shortcut",
                    "launch": ["xdg-open", f"steam://rungameid/{appid}"],
                })
    return games


# ── Desktop entries that are games ───────────────────────────────────────────

DESKTOP_DIRS = (
    "{home}/.local/share/applications",
    "/usr/share/applications",
    "{home}/.local/share/flatpak/exports/share/applications",
    "/var/lib/flatpak/exports/share/applications",
)

# Front ends for other libraries or for streaming: they start games, they are
# not games. Whatever is inside them comes from the sources that own it. Matched
# on the entry's own name (a reverse-DNS id ends in it), so a game that merely
# has "steam" in its title is left alone.
LAUNCHER_ENTRY = {
    "steam", "lutris", "heroic", "heroicgameslauncher", "moonlight", "playnite",
    "bottles", "itch", "itchio", "retroarch", "prism", "prismlauncher", "heroic-launcher",
}


def scan_desktop(opts, hidden):
    """Games that ship a .desktop entry. `desktopDirs` overrides where to look
    (the selftest uses it so the real system directories stay out)."""
    home = home_of(opts)
    dirs = opts.get("desktopDirs") or DESKTOP_DIRS
    if isinstance(dirs, str):
        dirs = dirs.split(",")
    games, seen = [], set()
    for pattern in dirs:
        for path in sorted(glob.glob(os.path.join(pattern.format(home=home), "*.desktop"))):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                text = open(real, errors="replace").read()
            except OSError:
                continue
            if not re.search(r"^Categories=.*\bGame\b", text, re.M):
                continue
            if re.search(r"^NoDisplay=true", text, re.M):
                continue
            name = re.search(r"^Name=(.+)$", text, re.M)
            command = re.search(r"^Exec=(.+)$", text, re.M)
            if not (name and command):
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            # "net.lutris.Lutris" -> "lutris": the entry's own name is the last
            # part of a reverse-DNS id.
            if stem.split(".")[-1].lower() in LAUNCHER_ENTRY or NOT_A_GAME.search(name.group(1)):
                continue
            # A shortcut to a Steam game: the Steam source already lists it.
            if "steam://rungameid/" in command.group(1):
                continue
            gid = f"desktop:{stem}"
            if gid in hidden:
                continue
            binary = os.path.basename(command.group(1).split()[0])
            entry = {
                "id": gid,
                "name": name.group(1).strip(),
                "source": "desktop",
                "detail": binary,
                # gio launches by file path and handles Flatpak exports alike.
                "launch": ["gio", "launch", path],
            }
            # The entry's own Icon= is either a path or a theme name; the panel
            # resolves the name itself.
            icon = re.search(r"^Icon=(.+)$", text, re.M)
            if icon:
                icon = icon.group(1).strip()
                entry["iconFile" if icon.startswith("/") else "iconName"] = icon
            games.append(entry)
    return games


# ── Lutris ───────────────────────────────────────────────────────────────────

def scan_lutris(home, hidden):
    games = []
    for yml in sorted(glob.glob(f"{home}/.config/lutris/games/*.yml")):
        text = open(yml, errors="replace").read()
        name = re.search(r"^\s*name:\s*(.+)$", text, re.M)
        slug = re.search(r"^\s*slug:\s*(.+)$", text, re.M)
        # ponytail: no games installed on this box, so the field names come from
        # Lutris' own yml layout; the filename fallback covers a missing slug.
        slug = (slug.group(1).strip() if slug else os.path.basename(yml)[:-4])
        gid = f"lutris:{slug}"
        if gid in hidden:
            continue
        games.append({
            "id": gid,
            "name": (name.group(1).strip() if name else slug),
            "source": "lutris",
            "detail": "lutris:rungame/" + slug,
            "launch": ["xdg-open", f"lutris:rungame/{slug}"],
        })
    return games


# ── Heroic ───────────────────────────────────────────────────────────────────

def _titles(data, runner):
    """Yield (app_name, title, executable) from Heroic's library files.

    The sideload file is a {"games": [...]} dict (measured on this box); Epic
    and GOG keep an app_name -> config mapping. With no Epic or GOG title
    installed here those two shapes are unverified — hence the defensive walk.
    """
    if isinstance(data, dict) and isinstance(data.get("games"), list):
        entries = data["games"]
    elif isinstance(data, dict) and isinstance(data.get("games"), dict):
        entries = [dict(v, app_name=k) for k, v in data["games"].items() if isinstance(v, dict)]
    elif isinstance(data, dict):
        entries = [dict(v, app_name=k) for k, v in data.items() if isinstance(v, dict)]
    else:
        entries = []
    for e in entries:
        app_name = e.get("app_name") or e.get("appName") or e.get("id")
        title = e.get("title") or e.get("name") or app_name
        install = e.get("install") or {}
        exe = install.get("executable") if isinstance(install, dict) else None
        if app_name:
            yield str(app_name), str(title), (os.path.expanduser(str(exe)) if exe else None)


def scan_heroic(home, hidden):
    games, seen_exe = [], set()
    files = [
        ("sideload", f"{home}/.config/heroic/sideload_apps/library.json"),
        ("legendary", f"{home}/.config/heroic/legendaryConfig/legendary/installed.json"),
        ("gog", f"{home}/.config/heroic/gog_store/installed.json"),
    ]
    for runner, path in files:
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path, errors="replace"))
        except ValueError:
            continue
        for app_name, title, exe in _titles(data, runner):
            gid = f"heroic:{app_name}"
            if gid in hidden:
                continue
            # A side-loaded game is a plain executable here, and we already know
            # its path — run it. That also tags the path so the folder scan
            # below does not list the same game twice.
            if exe and os.path.exists(exe):
                launch = [exe]
                seen_exe.add(os.path.dirname(os.path.realpath(exe)))
            else:
                launch = ["xdg-open", f"heroic://launch/{runner}/{app_name}"]
            games.append({
                "id": gid,
                "name": title,
                "source": "heroic",
                "detail": runner,
                "launch": launch,
            })
    return games, seen_exe


# ── Configured folders ───────────────────────────────────────────────────────

def launcher_rank(entry):
    """How good a starter is this file, or None when it is not one at all.

    Native starter first, AppImage next, any other script, then a bare Windows
    executable - which needs Wine and only wins in a folder that has nothing
    better. Rank 5 is not a game at all: it is an installer, and the panel offers
    to run it once instead of launching it.
    """
    name = entry.name
    if name.lower().endswith(".exe"):
        if INSTALLER.search(name) and not re.search(r"unins|redist|crash|dxsetup|vc_redist", name, re.I):
            return 5
        return None if NOT_THE_GAME.search(name) else 4
    if SKIP_FILE.search(name) or not LAUNCHER.search(name):
        return None
    if not (name.endswith((".sh", ".AppImage", ".run")) or os.access(entry.path, os.X_OK)):
        return None
    return 0 if re.match(r"(?i)start\.n", name) else \
           1 if name.endswith(".AppImage") else \
           2 if re.match(r"(?i)start", name) else 3


def walk_launchers(root, depth=DEPTH):
    """Yield (rank, path) for the best starter in each folder below root.

    ponytail: one row per directory, so a repack that ships both a native and a
    Wine starter (start.n.sh / start.e-w.sh) yields one game, not two. Prefer
    the native one; add a per-folder override if that ever guesses wrong.
    """
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
    found = {}
    stack = [(root, 0)]
    while stack:
        path, level = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        best = None
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if level + 1 < depth and not e.name.startswith("."):
                    stack.append((e.path, level + 1))
                continue
            rank = launcher_rank(e)
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, e.path)
        if best and os.path.dirname(best[1]) != os.path.realpath(root):
            found[os.path.dirname(best[1])] = best
    # The .exe a repack keeps under files/game-root is the same game as the
    # start.e-w.sh above it, so a rung only survives when no ancestor folder
    # already has a starter at least as good.
    for d in sorted(found):
        rank, path = found[d]
        if any(d != k and d.startswith(k.rstrip("/") + "/") and found[k][0] <= rank
               for k in found):
            continue
        yield rank, path


def _folder_title(path):
    """Folder name for a game, climbing past the containers a repack puts it in."""
    while True:
        name = os.path.basename(path.rstrip("/"))
        if not GENERIC_DIR.match(name) or path in ("", "/"):
            return name
        path = os.path.dirname(path)


# ── Downloads that are not games yet ─────────────────────────────────────────

def state_dir(opts):
    """Where bin/game-setup.sh leaves one <hash>.state per task it is doing."""
    return (opts.get("stateDir")
            or os.environ.get("GAMESHELF_STATE_DIR")
            or (os.environ.get("XDG_DATA_HOME") or f"{home_of(opts)}/.local/share") + "/gameshelf/tasks")


def read_states(directory):
    """Read the task state files: one key=value line each, `game=` repeatable."""
    states = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return states
    for name in names:
        if not name.endswith(".state"):
            continue
        data = {}
        try:
            lines = open(os.path.join(directory, name), errors="replace").read().splitlines()
        except OSError:
            continue
        for line in lines:
            key, _, value = line.partition("=")
            if key == "game":
                data.setdefault("games", []).append(value)
            elif key:
                data[key] = value
        if data.get("task"):
            states[data["task"]] = data
    return states


def archive_names(path, timeout=ARCHIVE_TIMEOUT):
    """Entry names inside an archive, or None when nothing could tell us.

    ponytail: stdlib zipfile for .zip, unrar for .rar (7z cannot read RAR5), 7z
    for the rest. None means "ask again next scan", never "not a game".
    """
    if path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                return z.namelist()
        except Exception:
            return None
    if path.lower().endswith(".rar"):
        out = run(["unrar", "lb", path], timeout=timeout)
        return out.stdout.splitlines() if out and out.returncode == 0 else None
    out = run(["7z", "l", "-ba", path], timeout=timeout)
    if not (out and out.returncode == 0):
        return None
    # date time attr size packed name -- the name may hold spaces
    return [line.split(None, 5)[5] for line in out.stdout.splitlines() if len(line.split(None, 5)) == 6]


def archive_cache(opts):
    path = os.path.join(os.path.dirname(state_dir(opts)), "archives.json")
    try:
        return path, json.load(open(path))
    except (OSError, ValueError):
        return path, {}


def archive_key(path):
    try:
        return "%s|%d|%d" % (path, os.path.getsize(path), int(os.path.getmtime(path)))
    except OSError:
        return None


def cached_answer(path, cache):
    """1 = a game archive, 0 = not one, None = never classified.

    Listing a 16 GB cold rar takes ~20 s, so the scan itself never lists one:
    --classify does that out of band and the panel rescans when it is done.
    """
    key = archive_key(path)
    return cache.get(key) if key else None


def classify_archive(path, cache, timeout=ARCHIVE_TIMEOUT):
    """List one archive and remember the answer. False when nothing could tell."""
    key = archive_key(path)
    names = archive_names(path, timeout=timeout)
    if names is None or key is None:
        return False
    cache[key] = 1 if any(STARTER.search(n) for n in names) else 0
    return True


def folder_list(opts):
    folders = opts.get("folders") or []
    if isinstance(folders, str):
        folders = folders.split(",")
    return [os.path.expanduser(str(f).strip()) for f in folders if str(f).strip()]


def classify_archives(opts):
    """Fill the archive cache. One archive per new download, so this only costs
    time the first time an archive is seen."""
    cache_path, cache = archive_cache(opts)
    done = 0
    for folder in folder_list(opts):
        for arc in archives_below(folder):
            real = os.path.realpath(arc)
            if cached_answer(real, cache) is not None:
                continue
            if os.path.isdir(ARCHIVE.sub("", real)):
                continue    # unpacked by hand: it gets a folder row, not an archive row
            if classify_archive(real, cache):
                done += 1
    save_cache(cache_path, cache)
    return done


def save_cache(path, cache):
    try:
        json.dump(cache, open(path, "w"))
    except OSError:
        pass


def archives_below(root, depth=DEPTH):
    """Every archive in the folders you configured, to the same depth."""
    stack = [(root, 0)]
    while stack:
        path, level = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if level + 1 < depth and not e.name.startswith("."):
                    stack.append((e.path, level + 1))
            elif ARCHIVE.search(e.name):
                yield e.path


def human_size(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000:
            return "%d %s" % (n, unit)
        n //= 1000
    return "%d TB" % n


STALE_AFTER = 2 * 3600  # a helper killed mid-run leaves state=running behind;
                        # past this the row offers a retry instead of "Installing…"


def is_stale(state):
    """Only a running task can go stale: a finished one carries no timer."""
    if state.get("state") != "running":
        return False
    try:
        since = int(state.get("since") or 0)
    except ValueError:
        return False
    # A file from before `since` existed is not evidence of a kill: only a
    # timestamped run that is old is.
    return since > 0 and time.time() - since > STALE_AFTER


def failure_status(state, what):
    """'Install failed: <what the installer wrote>' when it said anything."""
    error = (state.get("error") or "").strip()
    if error:
        return what + " failed: " + error[:90]
    return what + " failed — press " + what + " to retry"


def clean_title(name):
    """'Borderlands.4.v1.11.REPACK-KaOs' -> 'Borderlands 4'. A scene tag or a
    version is not part of the title, and 'Install Borderlands.4.v1.11.REPACK-KaOs'
    reads like a file listing instead of a game."""
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)                     # [FitGirl Repack], (x64)
    s = re.sub(r"(?i)[._ -]v\d[\d.]*", " ", s)                         # .v1.11, .v20260901
    s = re.sub(r"\b\d+(\.\d+)+", " ", s)                               # 12.4.5, 1.11
    # A tag only counts next to a separator: a game may be called Rune Factory.
    s = re.sub(r"(?i)[._ -](repack|fitgirl|kaos|vanya|dodi|elamigos|tenoke"
               r"|rune|codex|plaza|skidrow|jc141)\b", " ", s)
    return re.sub(r"[._-]+", " ", s).strip()


def first_game(root):
    """The starter the folder walk likes best under root, or None."""
    for rank, path in walk_launchers(root, depth=DEPTH + 2):
        if rank < 5:
            return path
    return None


def scan_folders(opts, hidden, known_dirs, states, cache):
    """Rows for the folders you pointed at: games that are ready, and downloads
    that are not - an archive to unpack, an installer to run."""
    games, pending = [], 0
    home = home_of(opts)
    for folder in folder_list(opts):

        # A download keeps a row of its own: before unpacking it is all that
        # exists, and afterwards that row owns the game it produced (so the walk
        # below does not list the same game twice) with Remove archive as its
        # button.
        for arc in sorted(archives_below(folder)):
            real = os.path.realpath(arc)
            gid = "file:" + real
            if gid in hidden:
                continue
            state = states.get(real) or {}
            if not state and os.path.isdir(ARCHIVE.sub("", real)):
                continue  # unpacked by hand already: the folder row has it
            if not state:
                answer = cached_answer(real, cache)
                if answer is None:
                    pending += 1   # --classify answers; the panel rescans then
                    continue
                if answer == 0:
                    continue
            row = {
                "id": gid,
                "name": clean_title(os.path.basename(ARCHIVE.sub("", real))),
                "source": "folders",
                "detail": "Archive · " + human_size(os.path.getsize(real)) + " · " + real.replace(home, "~"),
                "launch": [],
                "action": "Unpack",
                "run": [GAME_SETUP, "unpack", real],
            }
            if state.get("state") == "running" and not is_stale(state):
                row["state"] = "running"
                row["status"] = "Unpacking…"
            elif state.get("state") == "failed" or is_stale(state):
                row["state"] = "failed"
                row["status"] = (failure_status(state, "Unpack") if state.get("error")
                                 else "Unpack was interrupted — press Unpack to retry")
            elif state.get("state") == "ready":
                starter = first_game(state.get("dest") or "")
                row["action"] = "Remove archive"
                del row["run"]
                # Remove archive asks the panel for trash-or-delete, so the row
                # only carries those once there is nothing left to unpack.
                row["remove"] = {"trash": [GAME_SETUP, "remove", real, "--trash"],
                                 "delete": [GAME_SETUP, "remove", real, "--delete"]}
                if starter:
                    known_dirs.add(os.path.dirname(starter))
                    row["launch"] = [WINE_RUN, "--proton", starter]
                    row["detail"] = "Unpacked · " + starter.replace(home, "~")
            games.append(row)

        for rank, starter in walk_launchers(folder):
            real = os.path.realpath(starter)
            gid = "file:" + real
            if gid in hidden:
                continue
            state = states.get(real) or {}
            if rank == 5:
                # An installer: run it once, then the games it left in the prefix
                # get rows of their own - they sit inside the prefix, where this
                # walk cannot reach them.
                title = clean_title(_folder_title(os.path.dirname(real)))
                known_dirs.add(os.path.dirname(real))
                if state.get("state") == "running" and not is_stale(state):
                    games.append({
                        "id": gid, "name": title, "source": "folders",
                        "detail": "Installing · " + real.replace(home, "~"),
                        "launch": [], "action": "Install " + title,
                        "run": [GAME_SETUP, "install", real],
                        "state": "running", "status": "Installing…",
                    })
                elif state.get("games"):
                    for exe in state["games"]:
                        games.append({
                            "id": "file:" + exe,
                            "name": title,
                            "source": "folders",
                            "detail": "Installed · " + exe.replace(home, "~"),
                            "launch": [WINE_RUN, "--proton", "--in", state.get("prefix", ""), exe],
                        })
                else:
                    row = {
                        "id": gid, "name": title, "source": "folders",
                        "detail": "Installer · " + human_size(os.path.getsize(real)) + " · " + real.replace(home, "~"),
                        "launch": [], "action": "Install " + title,
                        "run": [GAME_SETUP, "install", real],
                    }
                    if state.get("state") == "failed" or is_stale(state):
                        row["state"] = "failed"
                        row["status"] = (failure_status(state, "Install") if state.get("error")
                                         else "Install was interrupted — press Install to retry")
                        if state.get("needs"):
                            # The log said what was missing, so one press does it:
                            # the verbs into the prefix, then the installer again.
                            row["action"] = "Prepare .NET"
                            row["run"] = [GAME_SETUP, "install", "--verbs", state["needs"], real]
                    games.append(row)
                continue
            if os.path.dirname(real) in known_dirs:
                continue  # a launcher already lists this folder
            known_dirs.add(os.path.dirname(real))
            name = re.sub(r"[._-]+", " ", _folder_title(os.path.dirname(real))).strip()
            if real.lower().endswith(".exe"):
                # A bare Windows game: Wine, in a prefix of its own.
                games.append({
                    "id": gid,
                    "name": name or os.path.basename(real),
                    "source": "folders",
                    "detail": "Wine \u00b7 " + real.replace(home, "~"),
                    "launch": [WINE_RUN, real],
                })
                continue
            # A repack that lost its exec bits in the unpack still starts: a
            # script without +x is given to bash, because the kernel's EACCES is
            # silent in the panel (nothing happens, no error anywhere).
            launch = [real]
            if real.endswith((".sh", ".run")) and not os.access(real, os.X_OK):
                launch = ["bash", real]
            games.append({
                "id": gid,
                "name": name or real,
                "source": "folders",
                "detail": real.replace(home, "~"),
                "launch": launch,
            })
    return games, pending


# ── Assembly ─────────────────────────────────────────────────────────────────

# ── Covers for rows that came from a folder ─────────────────────────────────
#
# A repack on disk carries no picture, so the name is looked up once in Steam's
# store search (no API key) and its header art is cached. Nothing is guessed: a
# name that does not match exactly gets no cover rather than the wrong one, and
# a miss is remembered so it is never asked again.

STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?cc=se&l=en&term="
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails?l=en&appids="
COVER_TIMEOUT = 15


def cover_key(name):
    """'Tidy.Up.the.Royal.Armory.v1.2' and 'Tidy Up the Royal Armory' are one.

    A colon becomes a space, so runs of spaces are collapsed too — otherwise
    'ReStory …' and 'Restory: …' would never meet.
    """
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", clean_title(name).lower()).split())


def cover_cache(opts):
    path = os.path.join(os.path.dirname(state_dir(opts)), "covers.json")
    try:
        return path, json.load(open(path))
    except (OSError, ValueError):
        return path, {}


def match_cover(items, query):
    """The appid whose name is this game, or None.

    Exact after normalising, else the shortest result the query prefixes — Steam
    puts an artbook or a DLC above the game itself ('ReStory … - Digital
    Artbook' before 'ReStory …').
    """
    want = cover_key(query)
    if not want:
        return None
    exact, prefixed = [], []
    for item in items or []:
        got = cover_key(str(item.get("name") or ""))
        if not got or not item.get("id"):
            continue
        if got == want:
            exact.append(item)
        elif got.startswith(want + " ") or want.startswith(got + " "):
            prefixed.append(item)
    for bucket in (exact, sorted(prefixed, key=lambda i: len(str(i.get("name"))))):
        if bucket:
            return str(bucket[0]["id"])
    return None


def http_json(url, timeout=COVER_TIMEOUT):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def http_file(url, dest, timeout=COVER_TIMEOUT):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
        if len(data) < 1024:          # a placeholder or an error page
            return False
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


def fetch_cover(name, cache_dir):
    """Download one cover. Returns a cache entry: {file} or {miss}."""
    found = http_json(STEAM_SEARCH + urllib_quote(name))
    appid = match_cover((found or {}).get("items"), name)
    if not appid:
        return {"miss": 1}
    detail = http_json(STEAM_DETAILS + appid)
    data = ((detail or {}).get(appid) or {}).get("data") or {}
    url = data.get("header_image") or ""
    if not url:
        return {"miss": 1}
    dest = os.path.join(cache_dir, hashlib.md5(cover_key(name).encode()).hexdigest() + ".jpg")
    if not http_file(url, dest):
        return {"miss": 1}
    return {"appid": appid, "name": data.get("name") or name, "file": dest}


def urllib_quote(text):
    import urllib.parse
    return urllib.parse.quote(text)


def covers_pass(opts):
    """Answer every row that has no picture. Runs out of band, one at a time."""
    if os.environ.get("GAMESHELF_COVERS") == "0":
        return 0
    path, cache = cover_cache(opts)
    cache_dir = os.path.join(os.path.dirname(path), "covers")
    games = scan(opts)["games"]
    done = 0
    for game in games:
        if game.get("iconFile") or game.get("iconName") or game.get("state"):
            continue                       # it has art, or it is a download row
        key = cover_key(game.get("name") or "")
        if not key or key in cache:
            continue
        os.makedirs(cache_dir, exist_ok=True)
        cache[key] = fetch_cover(game.get("name") or "", cache_dir)
        save_cache(path, cache)            # one at a time: a kill keeps the rest
        done += 1
        time.sleep(0.2)                    # be a good guest at Valve's door
    return done


def scan(opts):
    home = home_of(opts)
    sources = opts.get("sources") or list(SOURCE_LABELS)
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",")]
    hidden = set(opts.get("hidden") or [])
    errors, games = [], []

    if "steam" in sources:
        try:
            games += scan_steam(home, hidden)
        except Exception as e:
            errors.append(f"steam: {e}")
    if "shortcuts" in sources:
        try:
            games += scan_shortcuts(home, hidden)
        except Exception as e:
            errors.append(f"shortcuts: {e}")
    if "lutris" in sources:
        try:
            games += scan_lutris(home, hidden)
        except Exception as e:
            errors.append(f"lutris: {e}")

    used_exe = set()
    if "heroic" in sources:
        try:
            heroic, used_exe = scan_heroic(home, hidden)
            games += heroic
        except Exception as e:
            errors.append(f"heroic: {e}")
    if "desktop" in sources:
        try:
            games += scan_desktop(opts, hidden)
        except Exception as e:
            errors.append(f"desktop: {e}")
    pending_archives = 0
    if "folders" in sources:
        try:
            _, cache = archive_cache(opts)
            found, pending_archives = scan_folders(opts, hidden, used_exe,
                                                   read_states(state_dir(opts)), cache)
            games += found
        except Exception as e:
            errors.append(f"folders: {e}")

    for g in games:
        g.setdefault("detail", "")
    covers_on = os.environ.get("GAMESHELF_COVERS") != "0"
    _, covers = cover_cache(opts) if covers_on else (None, {})
    missing = set()
    for g in games:
        if g.get("iconFile") or g.get("iconName") or g.get("state"):
            continue
        entry = covers.get(cover_key(g.get("name") or "")) or {}
        if entry.get("file") and os.path.exists(entry["file"]):
            g["iconFile"] = entry["file"]
        elif covers_on:
            missing.add(cover_key(g.get("name") or ""))
    games.sort(key=lambda g: (g["source"], str(g["name"]).lower()))
    counts = {}
    for g in games:
        counts[g["source"]] = counts.get(g["source"], 0) + 1
    return {"games": games, "counts": counts, "errors": errors,
            "pendingClassification": pending_archives, "pendingCovers": len(missing)}


# ── Self-check ───────────────────────────────────────────────────────────────

def selftest():
    """Fixture tree: every source must find its game and skip the noise."""
    tmp = tempfile.mkdtemp(prefix="gameshelf-")
    # Mirror the real layout: the default library is listed in the vdf, and the
    # appmanifests live in <library>/steamapps/.
    os.makedirs(f"{tmp}/.steam/steam/steamapps")
    os.makedirs(f"{tmp}/steam/steamapps")
    open(f"{tmp}/.steam/steam/steamapps/libraryfolders.vdf", "w").write(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s/steam"\n\t}\n}\n' % tmp)
    open(f"{tmp}/steam/steamapps/appmanifest_123.acf", "w").write(
        '"AppState"\n{\n\t"appid"\t\t"123"\n\t"name"\t\t"Test Game"\n}\n')
    open(f"{tmp}/steam/steamapps/appmanifest_9.acf", "w").write(
        '"AppState"\n{\n\t"appid"\t\t"9"\n\t"name"\t\t"Proton Experimental"\n}\n')
    os.makedirs(f"{tmp}/.config/lutris/games")
    open(f"{tmp}/.config/lutris/games/demo.yml", "w").write("name: Lutris Demo\nslug: lutris-demo\n")
    os.makedirs(f"{tmp}/.config/heroic/sideload_apps")
    os.makedirs(f"{tmp}/games/vc")
    open(f"{tmp}/games/vc/start.sh", "w").write("#!/bin/sh\n")
    os.chmod(f"{tmp}/games/vc/start.sh", 0o755)
    # A folder whose only starter is a bare Windows executable, with the crash
    # handler that ships beside it (which is not a second game).
    os.makedirs(f"{tmp}/games/Toys.Shop.Tidy.Up/game")
    for exe in ("ToyShopTidyUp.exe", "UnityCrashHandler64.exe"):
        open(f"{tmp}/games/Toys.Shop.Tidy.Up/game/{exe}", "w").write("MZ")
    # A repack with a native starter and a Windows exe deeper down: one row.
    os.makedirs(f"{tmp}/games/OldPack")
    open(f"{tmp}/games/OldPack/start.sh", "w").write("#!/bin/sh\n")   # mode 0644 on purpose
    os.makedirs(f"{tmp}/games/Guacamelee/files/game-root")
    open(f"{tmp}/games/Guacamelee/files/game-root/Guacamelee.exe", "w").write("MZ")
    open(f"{tmp}/games/Guacamelee/start.n.sh", "w").write("#!/bin/sh\n")
    os.chmod(f"{tmp}/games/Guacamelee/start.n.sh", 0o755)
    json.dump({"games": [{"app_name": "abc", "title": "Vampire Crawler", "runner": "sideload",
                          "install": {"executable": f"{tmp}/games/vc/start.sh"}}]},
              open(f"{tmp}/.config/heroic/sideload_apps/library.json", "w"))

    # Non-Steam shortcuts: the binary KeyValues Steam writes them in.
    def bin_vdf(entries):
        blob = b"\x00shortcuts\x00"
        for index, entry in enumerate(entries):
            blob += b"\x00" + str(index).encode() + b"\x00"
            blob += b"\x01AppName\x00" + entry["name"].encode() + b"\x00"
            if "exe" in entry:
                blob += b"\x01Exe\x00" + entry["exe"].encode() + b"\x00"
            if "appid" in entry:
                blob += b"\x02appid\x00" + entry["appid"].to_bytes(4, "little")
            blob += b"\x08"
        return blob + b"\x08"

    shortcuts_dir = f"{tmp}/.steam/steam/userdata/0/config"
    os.makedirs(shortcuts_dir)
    open(f"{shortcuts_dir}/shortcuts.vdf", "wb").write(bin_vdf([
        {"name": "Tiny Bubbles.exe", "exe": f"{tmp}/games/TinyBubbles.exe", "appid": 987654},
        {"name": "No appid at all"},   # nothing to launch with: must be skipped
    ]))

    # Desktop entries: a game, a launcher, a Steam shortcut and a hidden one.
    apps = f"{tmp}/.local/share/applications"
    os.makedirs(apps)

    def desktop(stem, name, command, extra=""):
        open(f"{apps}/{stem}.desktop", "w").write(
            f"[Desktop Entry]\nType=Application\nName={name}\nExec={command}\n"
            f"Categories=Game;\n{extra}")

    desktop("tiny-game", "Tiny Desktop Game", "tiny-game --play", extra="Icon=/usr/share/pixmaps/tiny.png\n")
    desktop("pic-game", "Pic Game", "pic-game", extra="Icon=pic-game\n")
    desktop("steam", "Steam", "/usr/bin/steam %U")
    desktop("net.lutris.Lutris", "Lutris", "lutris %U")
    desktop("valheim", "Valheim", "steam steam://rungameid/892970")
    desktop("nodisplay", "Secret Game", "secret", extra="NoDisplay=true\n")

    out = scan({"home": tmp, "sources": ["steam", "shortcuts", "lutris", "heroic", "desktop", "folders"],
                "folders": [f"{tmp}/games"], "desktopDirs": [apps]})
    ids = {g["id"]: g for g in out["games"]}
    # An entry's own Icon= is a path or a theme name, and nothing else gets one.
    assert ids["desktop:tiny-game"].get("iconFile") == "/usr/share/pixmaps/tiny.png", \
        ids["desktop:tiny-game"]
    assert ids["desktop:pic-game"].get("iconName") == "pic-game", ids["desktop:pic-game"]
    assert "desktop:nodisplay" not in ids, "a NoDisplay entry is never a row"
    assert "steam:123" in ids, ids
    assert "steam:9" not in ids, "Proton is not a game"
    assert ids["heroic:abc"]["launch"] == [f"{tmp}/games/vc/start.sh"], ids["heroic:abc"]
    assert "lutris:lutris-demo" in ids, ids
    folders = [g for g in out["games"] if g["source"] == "folders"]
    assert sorted(g["name"] for g in folders) == ["Guacamelee", "OldPack", "Toys Shop Tidy Up"], folders
    # realpath: TMPDIR is a symlinked path on this box and the ids are real paths.
    old = os.path.realpath(f"{tmp}/games/OldPack/start.sh")
    assert ids["file:" + old]["launch"] == ["bash", old], \
        "a starter without the exec bit goes through bash, not EACCES"
    toy = os.path.realpath(f"{tmp}/games/Toys.Shop.Tidy.Up/game/ToyShopTidyUp.exe")
    assert ids["file:" + toy]["launch"] == [WINE_RUN, toy], folders
    guac = os.path.realpath(f"{tmp}/games/Guacamelee/start.n.sh")
    assert ids["file:" + guac]["launch"] == [guac], \
        "the native starter wins over the .exe deeper in the same repack"
    assert out["counts"]["shortcuts"] == 1, "the entry without an appid is not launchable"
    assert ids["shortcut:987654"]["name"] == "Tiny Bubbles", ids["shortcut:987654"]
    assert ids["shortcut:987654"]["launch"] == ["xdg-open", "steam://rungameid/987654"]
    assert ids["desktop:tiny-game"]["launch"][:2] == ["gio", "launch"], ids["desktop:tiny-game"]
    assert not [i for i in ("desktop:steam", "desktop:net.lutris.Lutris", "desktop:valheim",
                            "desktop:nodisplay") if i in ids], \
        "a launcher, a reverse-DNS launcher, a Steam shortcut and a NoDisplay entry are not games"
    # ── Downloads that are not games yet ────────────────────────────────
    dl = f"{tmp}/dl"
    os.makedirs(dl)

    def zip_of(path, entries):
        with zipfile.ZipFile(path, "w") as z:
            for name in entries:
                z.writestr(name, "MZ")

    zip_of(f"{dl}/Ball.Game.zip", ["Ball.Game/game/Ball.exe"])
    zip_of(f"{dl}/Fresh.Game.zip", ["Fresh.Game/game/Fresh.exe"])
    zip_of(f"{dl}/Texture.Pack.zip", ["Texture.Pack/wood.png"])
    zip_of(f"{dl}/Has.Twin.zip", ["Has.Twin/game/Has.Twin.exe"])
    os.makedirs(f"{dl}/Has.Twin")
    open(f"{dl}/Has.Twin/start.sh", "w").write("#!/bin/sh\n")
    os.chmod(f"{dl}/Has.Twin/start.sh", 0o755)
    os.makedirs(f"{dl}/Teardown [FitGirl Repack]")
    open(f"{dl}/Teardown [FitGirl Repack]/Setup.exe", "w").write("MZ")

    states_dir = f"{tmp}/state"
    os.makedirs(states_dir)

    def state_of(path, *lines):
        real = os.path.realpath(path)
        name = hashlib.md5(real.encode()).hexdigest()[:16] + ".state"
        open(f"{states_dir}/{name}", "w").write("\n".join([f"task={real}"] + list(lines)) + "\n")

    # Unpacked by GameShelf: the same row now offers Remove archive and the game.
    os.makedirs(f"{tmp}/Games/Ball.Game/game")
    open(f"{tmp}/Games/Ball.Game/game/Ball.exe", "w").write("MZ")
    state_of(f"{dl}/Ball.Game.zip", "state=ready", "kind=unpack", f"dest={tmp}/Games/Ball.Game")
    # Being unpacked right now.
    open(f"{dl}/Town.to.City.rar", "w").write("Rar!")
    state_of(f"{dl}/Town.to.City.rar", "state=running", "kind=unpack", f"dest={tmp}/Games")
    # An installer that left a game inside its prefix.
    os.makedirs(f"{tmp}/pfx/drive_c/Games/Borderlands/game")
    open(f"{tmp}/pfx/drive_c/Games/Borderlands/game/Borderlands4.exe", "w").write("MZ")
    os.makedirs(f"{dl}/Borderlands.4.v1.11.REPACK-KaOs")
    open(f"{dl}/Borderlands.4.v1.11.REPACK-KaOs/Install.exe", "w").write("MZ")
    state_of(f"{dl}/Borderlands.4.v1.11.REPACK-KaOs/Install.exe", "state=ready", "kind=install",
             f"prefix={tmp}/pfx", f"game={tmp}/pfx/drive_c/Games/Borderlands/game/Borderlands4.exe")
    # A failed install has to say what the installer wrote, not just "failed".
    os.makedirs(f"{dl}/Broken.Game")
    open(f"{dl}/Broken.Game/setup.exe", "w").write("MZ")
    state_of(f"{dl}/Broken.Game/setup.exe", "state=failed", "kind=install",
             f"prefix={tmp}/pfx2", "error=Setup needs .NET Framework 4.8", "needs=dotnet48")
    # A helper killed mid-run leaves state=running; the row must not sit on
    # "Installing…" for ever, but a fresh run must still say it is working.
    os.makedirs(f"{dl}/Stuck.Install")
    open(f"{dl}/Stuck.Install/setup.exe", "w").write("MZ")
    state_of(f"{dl}/Stuck.Install/setup.exe", "state=running", "kind=install",
             f"prefix={tmp}/pfx3", "since=1")
    os.makedirs(f"{dl}/Fresh.Install")
    open(f"{dl}/Fresh.Install/setup.exe", "w").write("MZ")
    state_of(f"{dl}/Fresh.Install/setup.exe", "state=running", "kind=install",
             f"prefix={tmp}/pfx4", "since=%d" % time.time())

    # The scan never lists an archive itself — that would hold the panel for a
    # minute on a cold 16 GB rar — it only reports how many are unanswered.
    opts3 = {"home": tmp, "sources": ["folders"], "stateDir": states_dir,
             "folders": [dl, f"{tmp}/Games"]}
    warm = scan(opts3)
    assert warm["pendingClassification"] == 2, warm["pendingClassification"]
    assert not [g for g in warm["games"] if "Fresh.Game.zip" in g["id"]], \
        "an unclassified archive gets no row yet"
    classify_archives(opts3)

    out3 = scan(opts3)
    assert out3["pendingClassification"] == 0, out3["pendingClassification"]

    # A cover the pass cached lands on the folder row; the lookup itself is
    # conservative, so an artbook above the game never becomes its picture.
    cover = f"{tmp}/covers/toys.jpg"
    os.makedirs(f"{tmp}/covers", exist_ok=True)
    open(cover, "wb").write(b"\xff\xd8" + b"0" * 4096)
    toys = next(g for g in out3["games"]
                if g["source"] == "folders" and not g.get("state"))
    json.dump({cover_key(toys["name"]): {"appid": "1", "file": cover}},
              open(f"{tmp}/covers.json", "w"))
    covered = {g["id"]: g for g in scan(opts3)["games"]}
    assert covered[toys["id"]].get("iconFile") == cover, covered[toys["id"]]
    assert match_cover([{"id": "5034500", "name": "Restory: Chill Electronics Repairs - Digital Artbook"},
                        {"id": "3812600", "name": "ReStory: Chill Electronics Repairs"}],
                       "ReStory Chill Electronics Repairs") == "3812600"
    assert match_cover([{"id": "1", "name": "Tale Sweeper"}], "Tidy Up") is None

    ids3 = {g["id"]: g for g in out3["games"]}
    fresh = ids3["file:" + os.path.realpath(f"{dl}/Fresh.Game.zip")]
    assert fresh["action"] == "Unpack" and fresh["name"] == "Fresh Game", fresh
    assert fresh["launch"] == [] and fresh["run"][-1] == os.path.realpath(f"{dl}/Fresh.Game.zip"), fresh
    ball = ids3["file:" + os.path.realpath(f"{dl}/Ball.Game.zip")]
    assert ball["action"] == "Remove archive", ball
    assert ball["launch"] == [WINE_RUN, "--proton",
                              os.path.realpath(f"{tmp}/Games/Ball.Game/game/Ball.exe")], ball
    assert ball["remove"]["trash"][-1] == "--trash" and ball["remove"]["delete"][-1] == "--delete", ball
    assert "file:" + os.path.realpath(f"{tmp}/Games/Ball.Game/game/Ball.exe") not in ids3, \
        "the archive row owns its unpacked game, so it is not listed twice"
    town = ids3["file:" + os.path.realpath(f"{dl}/Town.to.City.rar")]
    assert town["state"] == "running" and town["status"] == "Unpacking…", town
    assert town["launch"] == [] and town["action"] == "Unpack", town
    fit = ids3["file:" + os.path.realpath(f"{dl}/Teardown [FitGirl Repack]/Setup.exe")]
    assert fit["action"] == "Install Teardown" and fit["name"] == "Teardown", fit
    broken = ids3["file:" + os.path.realpath(f"{dl}/Broken.Game/setup.exe")]
    assert broken["state"] == "failed" and ".NET Framework" in broken["status"], broken
    # A failure the log blamed on .NET turns the row's one button into the fix:
    # the verbs go into the prefix and the installer runs again, in one press.
    assert broken["action"] == "Prepare .NET", broken
    assert broken["run"] == [GAME_SETUP, "install", "--verbs", "dotnet48",
                             os.path.realpath(f"{dl}/Broken.Game/setup.exe")], broken
    stuck = ids3["file:" + os.path.realpath(f"{dl}/Stuck.Install/setup.exe")]
    assert stuck["state"] == "failed" and "interrupted" in stuck["status"], stuck
    fresh = ids3["file:" + os.path.realpath(f"{dl}/Fresh.Install/setup.exe")]
    assert fresh["state"] == "running" and fresh["status"] == "Installing…", fresh
    border = ids3["file:" + os.path.realpath(f"{tmp}/pfx/drive_c/Games/Borderlands/game/Borderlands4.exe")]
    assert border["name"] == "Borderlands 4", border
    assert border["launch"] == [WINE_RUN, "--proton", "--in", os.path.realpath(f"{tmp}/pfx"),
                                os.path.realpath(f"{tmp}/pfx/drive_c/Games/Borderlands/game/Borderlands4.exe")], border
    assert "file:" + os.path.realpath(f"{dl}/Borderlands.4.v1.11.REPACK-KaOs/Install.exe") not in ids3, \
        "an installed installer is not offered again"
    assert not [i for i in ids3 if "Texture.Pack.zip" in i], "a texture pack is not an unpackable game"
    assert not [i for i in ids3 if "Has.Twin.zip" in i], \
        "an archive with an unpacked twin next to it is not offered again"
    assert ids3["file:" + os.path.realpath(f"{dl}/Has.Twin/start.sh")]["launch"] != [], \
        "the folder row still lists the unpacked twin"

    out2 = scan({"home": tmp, "sources": ["steam"], "folders": [f"{tmp}/games"],
                 "hidden": ["steam:123"]})
    assert out2["games"] == [], out2
    print("selftest ok:", sorted(ids), "| counts", out["counts"])


def main():
    if "--selftest" in sys.argv:
        return selftest()
    opts = {}
    for arg in sys.argv[1:]:
        if arg.startswith("{"):
            try:
                opts = json.loads(arg)
            except ValueError:
                pass
    # --classify: the background pass that lists the archives once, so a panel
    # open never waits for a 16 GB rar.
    # --covers: the out-of-band pass that finds pictures for rows that came from
    # a folder, so a panel open never waits for Steam.
    if "--covers" in sys.argv:
        covers_pass(opts)
        return
    if "--classify" in sys.argv:
        classify_archives(opts)
        return
    print(json.dumps(scan(opts)))


if __name__ == "__main__":
    main()
