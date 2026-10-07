import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// GameShelf bar widget: one icon for every game installed on this machine.
// Left click opens the launcher panel, right click toggles it. The panel does
// the launching; this widget only counts, so the icon can show a number.
BarWidget {
  id: root
  moduleName: "io.github.alexwest1981.gameshelf"

  // The helper sits next to this file; find it by position, not by plugin name,
  // so a renamed copy still works.
  readonly property string helper: Qt.resolvedUrl("scan-games.py").toString().replace("file://", "")

  property var games: []
  property string scanError: ""

  readonly property int gameCount: games.length
  readonly property string tooltip: {
    if (scanError !== "") return "GameShelf — " + scanError
    if (gameCount === 0) return "GameShelf — no games found"
    return "GameShelf — " + gameCount + " game" + (gameCount === 1 ? "" : "s")
  }

  // Settings arrive as strings; the helper wants a JSON object.
  readonly property string optionsJson: JSON.stringify({
    sources: (settings && settings.sources) ? String(settings.sources) : "steam,lutris,heroic,folders",
    folders: (settings && settings.folders) ? String(settings.folders) : "~/Downloads,~/Games",
    hidden: (settings && settings.hiddenIds)
      ? String(settings.hiddenIds).split(",").filter(function(s) { return s.trim() !== "" })
      : []
  })

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function applyScan(text) {
    try {
      var data = JSON.parse(String(text).trim())
      root.games = data.games || []
      root.scanError = (data.errors && data.errors.length > 0) ? data.errors.join("; ") : ""
    } catch (e) {
      root.scanError = "scan produced no JSON"
    }
  }

  function rescan() {
    if (!root.helper) return
    scanProc.command = ["python3", root.helper, root.optionsJson]
    scanProc.running = true
  }

  Process {
    id: scanProc
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyScan(text)
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.scanError = "scan failed (exit " + exitCode + ")"
    }
  }

  Component.onCompleted: rescan()
  onSettingsChanged: rescan()

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Nerd Font md-controller_classic (U+F0B82): the code point goes through
    // String.fromCodePoint because the literal glyph does not survive file tools.
    text: String.fromCodePoint(0xF0B82)
    fontFamily: "JetBrainsMono Nerd Font"
    foreground: root.gameCount > 0 ? Color.accent : Color.muted
    tooltipText: root.tooltip
    onPressed: function(b) {
      if (!root.bar || !root.bar.shell) return
      if (b === Qt.RightButton) root.bar.shell.toggle("io.github.alexwest1981.gameshelf", "{}")
      else root.bar.shell.summon("io.github.alexwest1981.gameshelf", "{}")
    }
  }
}
