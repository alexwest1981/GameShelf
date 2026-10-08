import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// GameShelf launcher panel: every game found on this machine, grouped by where
// it came from. Summon with:  omarchy-shell shell summon io.github.alexwest1981.gameshelf '<options>'
// The bar widget passes its own settings in that payload, so the panel and the
// widget always scan with the same sources and folders.
Item {
  id: root

  property var bar: null
  property var shell: null
  property var service: null
  property var manifest: null

  property bool opened: false
  property var games: []
  property bool scanning: false
  property string scanError: ""

  readonly property string pluginId: "io.github.alexwest1981.gameshelf"
  readonly property string helper: Qt.resolvedUrl("scan-games.py").toString().replace("file://", "")

  readonly property color themeBg: Color.background
  readonly property color themeFg: Color.foreground
  readonly property color themeAccent: Color.accent
  readonly property color themeMuted: Color.muted
  readonly property color themeUrgent: Color.urgent
  readonly property color surfaceBg: Qt.rgba(themeBg.r, themeBg.g, themeBg.b, 0.96)
  readonly property color lineColor: Qt.rgba(themeFg.r, themeFg.g, themeFg.b, 0.14)
  readonly property string fontFamily: {
    var families = Qt.fontFamilies()
    if (families.indexOf("JetBrainsMono Nerd Font") >= 0) return "JetBrainsMono Nerd Font"
    if (families.indexOf("JetBrains Mono") >= 0) return "JetBrains Mono"
    return families[0] || "sans-serif"
  }

  readonly property var sectionOrder: ["steam", "lutris", "heroic", "folders"]
  readonly property var sectionLabels: ({
    steam: "Steam", lutris: "Lutris", heroic: "Heroic", folders: "Folders"
  })

  // Section header + game rows flattened into one model for a single ListView.
  readonly property var rows: {
    var out = []
    for (var i = 0; i < sectionOrder.length; i++) {
      var key = sectionOrder[i]
      var mine = games.filter(function(g) { return g.source === key })
      if (mine.length === 0) continue
      out.push({ header: sectionLabels[key], count: mine.length })
      for (var j = 0; j < mine.length; j++) out.push({ game: mine[j] })
    }
    return out
  }

  // ── Panel lifecycle ───────────────────────────────────────────────────
  property string optionsJson: ""

  function defaultOptions() {
    var d = (manifest && manifest.barWidget && manifest.barWidget.defaults) || {}
    return {
      sources: d.sources || "steam,lutris,heroic,folders",
      folders: d.folders || "~/Downloads,~/Games",
      hidden: []
    }
  }

  function open(payloadJson) {
    opened = true
    var opts = defaultOptions()
    try {
      var given = JSON.parse(String(payloadJson || ""))
      // The payload is whatever the caller sends; the scanner ignores keys it
      // does not know, so pass everything through (a test can point `home` at
      // a made-up library, for instance).
      for (var key in given) {
        if (given[key] !== undefined && given[key] !== null) opts[key] = given[key]
      }
    } catch (e) { /* empty payload: defaults are fine */ }
    optionsJson = JSON.stringify(opts)
    rescan()
  }

  function close() {
    opened = false
  }

  function applyScan(text) {
    scanning = false
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
    scanning = true
    scanProc.command = ["python3", root.helper, root.optionsJson]
    scanProc.running = true
  }

  function launch(game) {
    if (!game || !game.launch || game.launch.length === 0) return
    root.close()
    // Detached: Steam/Heroic/Lutris are started through their URI handler, a
    // folder game is its own executable. Nothing here may die with the panel.
    Quickshell.execDetached(game.launch)
  }

  Process {
    id: scanProc
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyScan(text)
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.scanning = false
        root.scanError = "scan failed (exit " + exitCode + ")"
      }
    }
  }

  FloatingWindow {
    id: window
    title: "GameShelf"
    implicitWidth: 560
    implicitHeight: 620
    minimumSize: Qt.size(460, 380)
    color: root.themeBg
    visible: root.opened

    onVisibleChanged: {
      if (visible) {
        Qt.callLater(function() { content.forceActiveFocus() })
        return
      }
      if (root.opened) root.close()
    }

    Item {
      id: content
      anchors.fill: parent
      focus: true
      Keys.onEscapePressed: root.close()

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.space(16)
        spacing: Style.space(8)

        // ── Header ──────────────────────────────────────────────────
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(10)

          Rectangle {
            Layout.preferredWidth: 32
            Layout.preferredHeight: 32
            radius: 6
            color: root.themeAccent
            Text {
              anchors.centerIn: parent
              text: String.fromCodePoint(0xF0B82)
              color: root.themeBg
              font.family: root.fontFamily
              font.pixelSize: 17
            }
          }

          ColumnLayout {
            Layout.fillWidth: true
            spacing: 1
            Text {
              text: "GameShelf"
              color: root.themeFg
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              text: {
                if (root.scanning && root.games.length === 0) return "Scanning…"
                if (root.games.length === 0) return "No games found in the selected sources"
                var n = root.games.length
                return n + " game" + (n === 1 ? "" : "s") + " · grouped by launcher"
              }
              color: root.themeMuted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Button {
            text: "Refresh"
            foreground: root.themeFg
            tooltipText: "Scan the sources again"
            onClicked: root.rescan()
          }

          Button {
            text: "Close"
            foreground: root.themeFg
            tooltipText: "Close (Esc)"
            onClicked: root.close()
          }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: root.lineColor }

        Text {
          Layout.fillWidth: true
          visible: root.scanError !== ""
          text: root.scanError
          color: root.themeUrgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        // ── Game list, grouped by source ────────────────────────────
        ListView {
          id: listView
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          spacing: Style.space(6)
          model: root.rows
          ScrollBar.vertical: ScrollBar {}

          // One delegate for both kinds of row: a Loader would create the item
          // in its own context, where the delegate's modelData is not visible
          // (measured: every header and name rendered as "undefined").
          delegate: Item {
            id: delegateRoot
            width: listView.width
            readonly property bool isHeader: modelData !== undefined && modelData.header !== undefined
            readonly property var game: (modelData && modelData.game) ? modelData.game : null
            implicitHeight: isHeader ? headerLabel.implicitHeight + Style.space(6) : rowCard.height

            Text {
              id: headerLabel
              visible: delegateRoot.isHeader
              anchors.left: parent.left
              anchors.bottom: parent.bottom
              text: delegateRoot.isHeader ? (modelData.header + "  ·  " + modelData.count) : ""
              color: root.themeAccent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            Rectangle {
              id: rowCard
              visible: !delegateRoot.isHeader
              width: parent.width
              height: Math.max(56, rowLayout.implicitHeight + Style.space(12))
              radius: Style.cornerRadius
              color: rowHover.hovered
                ? Qt.rgba(root.themeFg.r, root.themeFg.g, root.themeFg.b, 0.07)
                : root.surfaceBg
              border.color: root.lineColor
              border.width: 1

              HoverHandler { id: rowHover }

              RowLayout {
                id: rowLayout
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(10)
                spacing: Style.space(10)

                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: 1
                  Text {
                    Layout.fillWidth: true
                    text: delegateRoot.game ? (delegateRoot.game.name || delegateRoot.game.id) : ""
                    color: root.themeFg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    elide: Text.ElideRight
                  }
                  Text {
                    Layout.fillWidth: true
                    text: delegateRoot.game ? String(delegateRoot.game.detail || "") : ""
                    color: root.themeMuted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }

                Button {
                  text: "Launch"
                  foreground: root.themeAccent
                  selected: true
                  bordered: true
                  tooltipText: "Start " + (delegateRoot.game ? delegateRoot.game.name : "")
                  onClicked: root.launch(delegateRoot.game)
                }
              }
            }
          }
        }

        Text {
          Layout.fillWidth: true
          visible: !root.scanning && root.games.length === 0 && root.scanError === ""
          horizontalAlignment: Text.AlignHCenter
          topPadding: Style.space(20)
          text: "Nothing found in the sources you selected."
          color: root.themeMuted
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        // ── Footer: where the settings live ─────────────────────────
        Text {
          Layout.fillWidth: true
          text: "Sources and folders are settings:\n"
            + "omarchy bar set io.github.alexwest1981.gameshelf sources \"steam,heroic,folders\"\n"
            + "omarchy bar set io.github.alexwest1981.gameshelf folders \"~/Downloads,~/Games\"\n"
            + "omarchy bar set io.github.alexwest1981.gameshelf hiddenIds \"steam:892970\""
          color: root.themeMuted
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}
