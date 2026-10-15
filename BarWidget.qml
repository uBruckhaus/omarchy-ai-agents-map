import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "I18n.js" as I18n

BarWidget {
  id: root

  moduleName: "ubruckhaus.ai-agents-map"

  readonly property string togglePath: Qt.resolvedUrl("ai-agents-map-toggle").toString().replace(/^file:\/\//, "")
  readonly property string settingsPath: Quickshell.env("HOME") + "/.config/omarchy/ai-agents-map/settings.json"

  // Interface language: the one picked in AI Agents Map's settings, else the system's.
  property string languageSetting: "auto"
  readonly property string systemLang: I18n.systemLanguage(Quickshell.env("LC_ALL"), Quickshell.env("LC_MESSAGES"), Quickshell.env("LANG"))
  readonly property string lang: I18n.resolve(languageSetting, systemLang)
  function t(key, args) { return I18n.tr(lang, key, args) }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function parseSettings(text) {
    var data = {}
    try { data = JSON.parse(text) || {} } catch (e) { data = {} }
    languageSetting = typeof data.language === "string" ? data.language : "auto"
  }

  FileView {
    id: settingsFile
    path: root.settingsPath
    watchChanges: true
    onFileChanged: reload()
    onLoaded: root.parseSettings(text())
    onLoadFailed: root.parseSettings("{}")
  }

  // settings.json is replaced atomically, which a file watch can miss.
  Timer {
    interval: 15000
    running: true
    repeat: true
    onTriggered: settingsFile.reload()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // U+F06A9 (Material Design robot) from the Nerd Font.
    text: "\u{F06A9}"
    tooltipText: root.t("tip")

    onPressed: function(mouseButton) {
      if (!root.bar) return
      // Detached so a click is never dropped while a previous toggle is still running.
      Quickshell.execDetached([root.togglePath])
    }
  }
}
