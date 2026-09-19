import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// A ball of yarn in the bar. Click to take the sweaters off or put them back on.
BarWidget {
  id: root
  moduleName: "mickul.sweater-kit"

  readonly property string launcher: Qt.resolvedUrl("bin/sweater-kit").toString().replace(/^file:\/\//, "")
  readonly property string stateFile: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/sweater-kit.state"
  property bool knitting: true

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  FileView {
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.knitting = text().trim() !== "off"
    onLoadFailed: root.knitting = true
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "🧶"
    opacity: root.knitting ? 1 : 0.4
    tooltipText: root.knitting ? "Sweaters on" : "Sweaters off"
    onPressed: function(b) {
      if (root.bar) root.bar.run(root.launcher + " toggle")
    }
  }
}
