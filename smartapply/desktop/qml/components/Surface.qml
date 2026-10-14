import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property alias content: body.data
    property int padding: Theme.cardPadding
    property int verticalPadding: padding
    property int contentSpacing: 16
    property color surfaceColor: Theme.surface
    property color surfaceEndColor: surfaceColor
    property color strokeColor: Theme.line
    property int radiusValue: Theme.radiusLarge
    property bool elevated: false
    property bool luminous: false
    default property alias contentData: body.data

    implicitHeight: body.implicitHeight + verticalPadding * 2

    Rectangle {
        visible: root.elevated
        anchors.fill: parent
        anchors.leftMargin: 1
        anchors.rightMargin: -1
        anchors.topMargin: 8
        anchors.bottomMargin: -8
        radius: root.radiusValue + 4
        color: Theme.shadow
    }
    Rectangle {
        visible: root.elevated && root.luminous
        anchors.fill: parent
        anchors.margins: -2
        radius: root.radiusValue + 3
        color: "transparent"
        border.color: "#245F4BFF"
        border.width: Theme.lineWidth
    }
    Rectangle {
        anchors.fill: parent
        radius: root.radiusValue
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: root.surfaceColor }
            GradientStop { position: 1; color: root.surfaceEndColor }
        }
        border.color: root.strokeColor
        border.width: Theme.lineWidth

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.radiusValue
            anchors.rightMargin: root.radiusValue
            anchors.top: parent.top
            height: Theme.lineWidth
            color: root.strokeColor.a > 0 ? Theme.highlight : "transparent"
        }
    }

    ColumnLayout {
        id: body
        anchors.fill: parent
        anchors.leftMargin: root.padding
        anchors.rightMargin: root.padding
        anchors.topMargin: root.verticalPadding
        anchors.bottomMargin: root.verticalPadding
        spacing: root.contentSpacing
    }
}
