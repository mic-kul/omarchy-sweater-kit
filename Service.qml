import QtQuick
import Quickshell
import Quickshell.Io

// Keeps the sweater-kit overlay running for the life of the shell session.
Item {
  id: root

  property var shell: null
  readonly property string launcher: Qt.resolvedUrl("bin/sweater-kit").toString().replace(/^file:\/\//, "")
  property int crashes: 0

  Process {
    id: knitter
    command: [root.launcher]
    running: true
    onExited: function(exitCode, exitStatus) {
      root.crashes++
      restart.interval = Math.min(30000, 1000 * Math.pow(2, Math.min(root.crashes, 5)))
      restart.start()
    }
  }

  Timer {
    id: restart
    repeat: false
    onTriggered: knitter.running = true
  }

  Timer {
    interval: 120000
    running: true
    repeat: true
    onTriggered: root.crashes = 0
  }

  Component.onDestruction: knitter.signal(15)
}
