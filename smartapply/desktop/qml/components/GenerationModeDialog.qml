import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    objectName: "generationModeDialog"
    property var jobIds: []
    property bool shortlist: false
    property int offerCount: jobIds.length
    property bool controlsEnabled: true
    signal modeChosen(string mode, var jobIds, bool shortlist)
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(460, parent ? parent.width - 48 : 460)
    implicitHeight: content.implicitHeight + padding * 2
    modal: true
    focus: true
    padding: 24
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: Rectangle {
        radius: Theme.radiusLarge
        color: Theme.surfaceRaised
        border.color: Theme.lineStrong
    }
    Overlay.modal: Rectangle { color: Theme.scrim }

    function choose(mode) {
        if (!controlsEnabled || offerCount <= 0) return
        modeChosen(mode, jobIds, shortlist)
        close()
    }

    contentItem: ColumnLayout {
        id: content
        spacing: 18
        Text {
            Layout.fillWidth: true
            text: "Créer les candidatures"
            color: Theme.ink
            font.pixelSize: 19
            font.weight: Font.DemiBold
            wrapMode: Text.WordWrap
        }
        Text {
            Layout.fillWidth: true
            text: root.offerCount + (root.offerCount === 1 ? " offre analysée." : " offres analysées.")
                + " Le CV, la lettre et les étapes suivantes s’enchaînent automatiquement. Les étapes terminées sont conservées pour une reprise."
            color: Theme.inkSoft
            font.pixelSize: 13
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            AppButton {
                text: "Retour"
                refined: true
                onClicked: root.close()
            }
            Item { Layout.fillWidth: true }
            AppButton {
                objectName: "generationImmediateModeAction"
                text: "Immédiat"
                refined: true
                enabled: root.controlsEnabled && root.offerCount > 0
                onClicked: root.choose("immediate")
            }
            AppButton {
                objectName: "generationBatchModeAction"
                text: "Batch"
                kind: "primary"
                refined: true
                enabled: root.controlsEnabled && root.offerCount > 0
                onClicked: root.choose("batch")
            }
        }
    }
}
