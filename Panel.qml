import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
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

  // The folders the scanner walks, as the panel's own list: shown under the
  // game list, added with a folder picker, written back as a bar setting.
  property var foldersList: []

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

  readonly property var sectionOrder: ["steam", "shortcuts", "lutris", "heroic", "desktop", "folders"]
  readonly property var sectionLabels: ({
    steam: "Steam", shortcuts: "Steam shortcuts", lutris: "Lutris",
    heroic: "Heroic", desktop: "Desktop", folders: "Folders"
  })

  // The filter narrows what the list shows; sections without a hit drop out
  // by themselves because rows skips empty ones.
  property string filter: ""
  readonly property var visibleGames: {
    var q = filter.trim().toLowerCase()
    if (q === "") return games
    return games.filter(function(g) {
      return (String(g.name) + " " + String(g.detail) + " " + String(g.source)).toLowerCase().indexOf(q) >= 0
    })
  }

  // Section header + game rows flattened into one model for a single ListView.
  readonly property var rows: {
    var out = []
    for (var i = 0; i < sectionOrder.length; i++) {
      var key = sectionOrder[i]
      var mine = visibleGames.filter(function(g) { return g.source === key })
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
      sources: d.sources || "steam,shortcuts,lutris,heroic,desktop,folders",
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
    foldersList = parseList(opts.folders)
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

  // ── Folders: pick them here, no CLI ──────────────────────────────────
  // A comma string in the settings, a trimmed list in the panel. Anything
  // else (a payload from a test, an array) is accepted too.
  function parseList(value) {
    if (value === undefined || value === null) return []
    var raw = (typeof value === "string") ? value.split(",") : value
    var out = []
    for (var i = 0; i < raw.length; i++) {
      var item = String(raw[i]).trim()
      if (item !== "" && out.indexOf(item) < 0) out.push(item)
    }
    return out
  }

  function homePath() {
    var opts = {}
    try { opts = JSON.parse(root.optionsJson) } catch (e) { }
    return String(opts.home || "")
  }

  // Show a path the way the settings carry it: home as ~.
  function prettyPath(path) {
    var home = homePath()
    if (home !== "" && path.indexOf(home + "/") === 0) return "~" + path.substring(home.length)
    var m = String(path).match("^/home/[^/]+")
    return m ? "~" + path.substring(m[0].length) : path
  }

  function setFolders(list) {
    foldersList = list
    var value = list.join(",")
    // The shell owns the settings; writing them is the CLI's job, and the
    // widget re-reads them from shell.json and rescans (onSettingsChanged).
    setProc.command = ["omarchy", "bar", "set", root.pluginId, "folders", value]
    setProc.running = true
    // The panel scans with what it just picked even before that lands.
    try {
      var opts = JSON.parse(root.optionsJson)
      opts.folders = value
      optionsJson = JSON.stringify(opts)
    } catch (e) { }
    rescan()
  }

  function addFolder(folder) {
    var path = String(folder || "")
    if (path.indexOf("file://") === 0) path = path.substring(7)
    try { path = decodeURIComponent(path) } catch (e) { }
    // A trailing slash is the same folder as no slash: FolderDialog sends one.
    while (path.length > 1 && path.charAt(path.length - 1) === "/") path = path.slice(0, -1)
    if (path === "" || foldersList.indexOf(path) >= 0) return
    setFolders(foldersList.concat([path]))
  }

  function removeFolder(folder) {
    setFolders(foldersList.filter(function(f) { return f !== folder }))
  }

  // The picker lives in the window (a Qt dialog needs the window that opens
  // it), and the button goes through here so a test can open it too.
  function openFolderPicker() {
    folderDialog.open()
  }

  Process {
    id: setProc
    running: false
    stderr: StdioCollector { id: setErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode === 0) return
      root.scanError = "could not save the folder setting (exit " + exitCode + ")"
        + (setErr.text ? ": " + String(setErr.text).trim() : "")
    }
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
                if (root.filter.trim() !== "") {
                  return root.visibleGames.length + " of " + root.games.length + " games match"
                }
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

        // ── Filter: with every source on, the list is longer than the panel
        TextField {
          id: filterField
          Layout.fillWidth: true
          placeholderText: "Type to filter " + root.games.length + " games"
          onTextChanged: root.filter = text
          // Escape empties the field first, and only then closes the panel.
          Keys.onEscapePressed: function(event) {
            if (text !== "") { text = ""; event.accepted = true }
            else event.accepted = false
          }
        }

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

        // ── Folders: picked here, stored as a bar setting ───────────
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: root.lineColor }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          Text {
            text: "Folders"
            color: root.themeMuted
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Flow {
            Layout.fillWidth: true
            spacing: Style.space(6)

            Repeater {
              model: root.foldersList
              delegate: Rectangle {
                id: chip
                readonly property string folder: String(modelData)
                width: Math.min(chipRow.implicitWidth + Style.space(14), 240)
                height: Style.space(24)
                radius: Style.cornerRadius
                color: "transparent"
                border.color: root.lineColor
                border.width: 1

                RowLayout {
                  id: chipRow
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(7)
                  anchors.rightMargin: Style.space(3)
                  spacing: Style.space(2)

                  Text {
                    Layout.fillWidth: true
                    text: root.prettyPath(chip.folder)
                    color: root.themeFg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideMiddle
                  }

                  Text {
                    Layout.preferredWidth: Style.space(16)
                    text: String.fromCodePoint(0x00D7)
                    color: removeHover.hovered ? root.themeUrgent : root.themeMuted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: removeHover }
                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.removeFolder(chip.folder)
                    }
                  }
                }
              }
            }
          }

          Button {
            text: "+ Add"
            foreground: root.themeAccent
            bordered: true
            tooltipText: "Pick a folder that holds your game launchers"
            onClicked: root.openFolderPicker()
          }
        }
      }

      // A Qt dialog needs the window that opens it, so the picker belongs to
      // this window, not to the panel object outside it.
      FolderDialog {
        id: folderDialog
        title: "Add a folder to scan"
        onAccepted: root.addFolder(folderDialog.selectedFolder)
      }
    }
  }
}
