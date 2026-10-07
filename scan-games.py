#!/usr/bin/env python3
"""Scan this machine for installed games and print one JSON line.

Sources, each switchable in the plugin settings:

  steam    appmanifest_*.acf in every Steam library (libraryfolders.vdf lists
           them, so games on other drives are found too), minus Proton and
           the Steam Linux Runtimes, which are not games
  lutris   ~/.config/lutris/games/*.yml
  heroic   ~/.config/heroic/ side-loaded apps, Epic (legendary) and GOG
  folders  executables that look like game launchers, up to DEPTH levels
           below each folder you configure

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
import json
import os
import re
import subprocess
import sys
import tempfile

DEPTH = 3  # how deep below a configured folder we look for a game launcher

SOURCE_LABELS = {
    "steam": "Steam",
    "lutris": "Lutris",
    "heroic": "Heroic",
    "folders": "Folders",
}

# Proton and the Steam Linux Runtimes live in steamapps like games do.
NOT_A_GAME = re.compile(r"steam linux runtime|proton|^steamworks", re.I)

# A file that looks like something you double-click to play. Note there is no
# bare ".sh" case: a repack folder is full of helper scripts (actions.sh,
# patch.sh) that would each become a "game".
LAUNCHER = re.compile(r"^(start|run|launch|play|game)|\.(AppImage|run)$", re.I)
SKIP_FILE = re.compile(
    r"\.(so|dll|py|desktop|txt|md|json|log|exe|bat|zip|7z|tar|gz)$"
    r"|install|setup|uninstall|patch|update|config|fix|readme|actions|library",
    re.I,
)


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
            games.append({
                "id": gid,
                "name": name,
                "source": "steam",
                "detail": f"appid {appid.group(1)}",
                "launch": ["xdg-open", f"steam://rungameid/{appid.group(1)}"],
            })
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

def walk_launchers(root, depth=DEPTH):
    """Yield (directory, best_launcher) for each folder holding a game starter.

    ponytail: one row per directory, so a repack that ships both a native and a
    Wine starter (start.n.sh / start.e-w.sh) yields one game, not two. Prefer
    the native one; add a per-folder override if that ever guesses wrong.
    """
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
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
            if SKIP_FILE.search(e.name) or not LAUNCHER.search(e.name):
                continue
            if not (e.name.endswith((".sh", ".AppImage", ".run")) or os.access(e.path, os.X_OK)):
                continue
            # native starter first, AppImage next, Wine/other last
            rank = 0 if re.match(r"(?i)start\.n", e.name) else \
                   1 if e.name.endswith(".AppImage") else \
                   2 if re.match(r"(?i)start", e.name) else 3
            if best is None or rank < best[0]:
                best = (rank, e.path)
        if best and os.path.dirname(best[1]) != os.path.realpath(root):
            yield best[1]


def scan_folders(opts, hidden, known_dirs):
    games = []
    folders = opts.get("folders") or []
    if isinstance(folders, str):
        folders = folders.split(",")
    home = home_of(opts)
    for folder in folders:
        if not str(folder).strip():
            continue
        for exe in walk_launchers(str(folder).strip()):
            real = os.path.realpath(exe)
            if os.path.dirname(real) in known_dirs:
                continue  # a launcher already lists this folder
            known_dirs.add(os.path.dirname(real))
            gid = "file:" + real
            if gid in hidden:
                continue
            name = re.sub(r"[._-]+", " ", os.path.basename(os.path.dirname(real))).strip()
            games.append({
                "id": gid,
                "name": name or real,
                "source": "folders",
                "detail": real.replace(home, "~"),
                "launch": [real],
            })
    return games


# ── Assembly ─────────────────────────────────────────────────────────────────

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
    if "folders" in sources:
        try:
            games += scan_folders(opts, hidden, used_exe)
        except Exception as e:
            errors.append(f"folders: {e}")

    for g in games:
        g.setdefault("detail", "")
    games.sort(key=lambda g: (g["source"], str(g["name"]).lower()))
    counts = {}
    for g in games:
        counts[g["source"]] = counts.get(g["source"], 0) + 1
    return {"games": games, "counts": counts, "errors": errors}


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
    json.dump({"games": [{"app_name": "abc", "title": "Vampire Crawler", "runner": "sideload",
                          "install": {"executable": f"{tmp}/games/vc/start.sh"}}]},
              open(f"{tmp}/.config/heroic/sideload_apps/library.json", "w"))

    out = scan({"home": tmp, "sources": ["steam", "lutris", "heroic", "folders"],
                "folders": [f"{tmp}/games"]})
    ids = {g["id"]: g for g in out["games"]}
    assert "steam:123" in ids, ids
    assert "steam:9" not in ids, "Proton is not a game"
    assert ids["heroic:abc"]["launch"] == [f"{tmp}/games/vc/start.sh"], ids["heroic:abc"]
    assert "lutris:lutris-demo" in ids, ids
    assert not any(g["source"] == "folders" for g in out["games"]), \
        "the side-loaded executable must not also be listed as a folder game"
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
    print(json.dumps(scan(opts)))


if __name__ == "__main__":
    main()
