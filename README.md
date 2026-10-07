# GameShelf

Every game installed on this machine, in one Omarchy bar icon. Click the
controller, pick a game, play. Steam, Lutris, Heroic and any folder you point it
at — grouped by where the game came from.

Written because OmaGames lists *Omarchy game plugins*, which is a different thing:
your Steam/Lutris/Heroic library is not a plugin and can never show up there.

## Install

```bash
bash install.sh              # copies into ~/.config/omarchy/plugins/custom.games, validates, enables
bash install.sh --no-enable  # same but leave it out of the bar
```

Run it again after `git pull`. The repo owns the sources; the plugin folder is a
build artifact, so editing files there is not the way to change behaviour.

## Settings

```bash
omarchy bar set custom.games sources "steam,heroic,folders"   # drop lutris
omarchy bar set custom.games folders "~/Downloads,~/Games"    # where the folders source looks
omarchy bar set custom.games hiddenIds "steam:892970"         # never list this one
```

- `sources` — `steam`, `lutris`, `heroic`, `folders` (comma separated).
- `folders` — searched up to three levels down when `folders` is on.
- `hiddenIds` — ids come from the panel's row, e.g. `steam:892970`,
  `heroic:<app_name>`, `file:/path/to/game`.

## What it reads

| Source | Where | How it starts |
|---|---|---|
| Steam | `appmanifest_*.acf` in every library in `libraryfolders.vdf` (other drives too), minus Proton and the Steam Linux Runtimes | `steam://rungameid/<appid>` |
| Lutris | `~/.config/lutris/games/*.yml` | `lutris:rungame/<slug>` |
| Heroic | side-loaded apps, Epic (`legendary`) and GOG libraries | the game's own executable, else `heroic://launch/<runner>/<app_name>` |
| Folders | one starter per folder (`start.*`, `run.*`, `*.AppImage`), helper scripts and installers skipped | the executable itself |

Every launcher we do not own goes through `xdg-open` on its URI scheme — one code
path, no per-launcher quoting. Nothing is guessed about a game's arguments.

## Manual scan

```bash
python3 scan-games.py '{"sources":["steam","heroic","folders"],"folders":["~/Downloads"]}' | python3 -m json.tool
python3 scan-games.py --selftest    # fixture tree in a temp dir: every source must find its game, and its noise must stay out
```

## Known limits

- One row per folder: a repack shipping both `start.n.sh` and `start.e-w.sh`
  shows once (native preferred). A per-folder override is the fix if that ever
  guesses wrong.
- `.exe` files are skipped — a bare Windows executable needs a Wine prefix,
  which is Lutris'/Heroic's job, not a launcher's.
- Epic and GOG library shapes are read defensively: neither was installed when
  this was written, so those branches are unverified until they hold a game.
