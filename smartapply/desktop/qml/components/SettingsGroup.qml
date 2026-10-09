import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    property string title: ""
    property string caption: ""
    property var fields: []
    property var drafts: ({})
    property bool collapsible: false
    property bool expanded: !collapsible
    property string statusLabel: ""
    property string statusTone: "neutral"
    signal edited(string key, string value)
    Layout.fillWidth: true
    implicitHeight: content.implicitHeight + 36
    radius: Theme.radiusMedium
    color: Theme.canvasLift
    border.color: Theme.line
    ColumnLayout {
        id: content
        width: parent.width - 36
        x: 18; y: 18
        spacing: 18
        Item {
            Layout.fillWidth: true
            implicitHeight: heading.implicitHeight
            activeFocusOnTab: root.collapsible
            Accessible.role: Accessible.Button
            Accessible.name: root.title + (root.collapsible ? root.expanded ? " · Replier" : " · Déplier" : "")
            Accessible.onPressAction: if (root.collapsible) root.expanded = !root.expanded
            Rectangle { anchors.fill: parent; anchors.margins: -7; radius: Theme.radiusSmall; color: parent.activeFocus ? Theme.accentSoft : "transparent" }
            RowLayout {
                id: heading
                width: parent.width
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 5
                    Text { Layout.fillWidth: true; text: root.title; color: Theme.ink; font.pixelSize: 14; font.weight: Font.DemiBold; wrapMode: Text.WordWrap }
                    Text { visible: root.caption.length > 0; Layout.fillWidth: true; text: root.caption; color: Theme.inkMuted; font.pixelSize: 12; wrapMode: Text.WordWrap }
                }
                Pill { visible: root.statusLabel.length > 0; text: root.statusLabel; tone: root.statusTone; compact: true }
                SvgIcon { visible: root.collapsible; source: Theme.icon("chevron-right"); rotation: root.expanded ? 90 : 0; color: Theme.inkFaint; Layout.preferredWidth: 16; Layout.preferredHeight: 16 }
            }
            HoverHandler { cursorShape: root.collapsible ? Qt.PointingHandCursor : Qt.ArrowCursor }
            TapHandler { enabled: root.collapsible; onTapped: root.expanded = !root.expanded }
            Keys.onReturnPressed: if (root.collapsible) root.expanded = !root.expanded
            Keys.onSpacePressed: if (root.collapsible) root.expanded = !root.expanded
        }
        GridLayout {
            visible: root.expanded
            Layout.fillWidth: true
            columns: width >= 620 ? 2 : 1
            uniformCellWidths: true
            columnSpacing: 20; rowSpacing: 20
            Repeater {
                model: root.fields
                delegate: SettingsField {
                    required property var modelData
                    configuration: modelData
                    drafts: root.drafts
                    onEdited: (key, value) => root.edited(key, value)
                }
            }
        }
    }
}
