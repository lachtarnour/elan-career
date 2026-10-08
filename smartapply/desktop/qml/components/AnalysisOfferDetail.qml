import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var offer: ({})
    property int requestedId: 0
    property bool loading: false
    property bool archiveEnabled: false
    property real pageWidth: 1400
    property real textZoom: 1.15
    readonly property int offerId: Number(offer.id || 0)
    readonly property real fontScale: Math.min(1.25, Math.max(1, pageWidth / 1400)) * textZoom
    signal sourceRequested(string url)
    signal retryRequested()
    signal archiveRequested(int jobId)
    onOfferIdChanged: detailViewport.contentY = 0

    EmptyState {
        id: emptyState
        anchors.centerIn: parent
        visible: !root.offerId && !root.loading
        iconSource: Theme.icon("briefcase")
        title: root.requestedId ? "Offre indisponible" : "Aucune offre ouverte"
        message: root.requestedId ? "Le contenu de cette offre n’a pas pu être chargé."
            : "Cliquez sur une ligne du tableau pour lire l’offre."
    }
    AppButton {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: emptyState.bottom
        anchors.topMargin: 12
        visible: root.requestedId > 0 && !root.offerId && !root.loading
        text: "Réessayer"
        refined: true
        onClicked: root.retryRequested()
    }
    BusyIndicator {
        anchors.centerIn: parent
        visible: root.loading && !root.offerId
        running: visible
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: 4
        anchors.rightMargin: 6
        visible: Boolean(root.offerId)
        spacing: 12

        Flickable {
            id: detailViewport
            objectName: "analysisOfferDetailViewport"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: detailContent.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            ScrollBar.vertical: AppScrollBar { }

            ColumnLayout {
                id: detailContent
                width: detailViewport.width - Theme.scrollGutter - 8
                spacing: 12

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        objectName: "analysisOfferTitle"
                        Layout.fillWidth: true
                        text: root.offer.title || ""
                        textFormat: Text.PlainText
                        color: Theme.ink
                        font.pixelSize: Math.round(20 * root.fontScale)
                        font.weight: Font.DemiBold
                        font.letterSpacing: -0.25
                        lineHeight: 1.15
                        wrapMode: Text.WordWrap
                    }
                    AppButton {
                        id: zoomButton
                        objectName: "analysisOfferZoom"
                        Layout.alignment: Qt.AlignTop
                        implicitWidth: 30
                        implicitHeight: 30
                        text: "Aa"
                        fontPixelSize: 10
                        quiet: true
                        Accessible.name: "Ajuster la taille du texte de l’offre"
                        onClicked: zoomPopup.opened ? zoomPopup.close() : zoomPopup.open()
                        Popup {
                            id: zoomPopup
                            x: zoomButton.width - width
                            y: zoomButton.height + 4
                            width: 118
                            height: 38
                            padding: 3
                            focus: true
                            closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                            background: Rectangle {
                                radius: 10
                                color: Theme.surfaceRaised
                                border.color: Theme.lineStrong
                            }
                            contentItem: RowLayout {
                                spacing: 2
                                AppButton {
                                    text: "A−"
                                    Layout.fillWidth: true
                                    implicitWidth: 32; implicitHeight: 30
                                    quiet: true
                                    enabled: root.textZoom > 0.85
                                    onClicked: root.textZoom = Math.max(0.85, root.textZoom - 0.1)
                                }
                                AppButton {
                                    text: "Aa"
                                    Layout.fillWidth: true
                                    implicitWidth: 32; implicitHeight: 30
                                    quiet: true
                                    onClicked: root.textZoom = 1.15
                                }
                                AppButton {
                                    text: "A+"
                                    Layout.fillWidth: true
                                    implicitWidth: 32; implicitHeight: 30
                                    quiet: true
                                    enabled: root.textZoom < 1.55
                                    onClicked: root.textZoom = Math.min(1.55, root.textZoom + 0.1)
                                }
                            }
                        }
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: [root.offer.company, root.offer.location].filter(Boolean).join(" · ")
                    textFormat: Text.PlainText
                    color: Theme.inkMuted
                    font.pixelSize: Math.round(11 * root.fontScale)
                    wrapMode: Text.WordWrap
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                Rectangle {
                    visible: Boolean(root.offer.classification_summary)
                    Layout.fillWidth: true
                    implicitHeight: classificationContent.implicitHeight + 24
                    radius: Theme.radiusMedium
                    color: Theme.surfaceMuted
                    border.color: Theme.line
                    ColumnLayout {
                        id: classificationContent
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 6
                        Text {
                            text: "Résultat du filtrage"
                            color: Theme.ink
                            font.pixelSize: Math.round(11 * root.fontScale)
                            font.weight: Font.DemiBold
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.offer.classification_summary || ""
                            textFormat: Text.PlainText
                            color: Theme.inkMuted
                            font.pixelSize: Math.round(11 * root.fontScale)
                            wrapMode: Text.WordWrap
                        }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Rectangle { Layout.preferredWidth: 3; Layout.preferredHeight: 13; radius: 2; color: Theme.accent }
                    FormLabel { text: "DESCRIPTION DU POSTE"; fontPixelSize: Math.round(11 * root.fontScale) }
                    Item { Layout.fillWidth: true }
                }
                OfferDescription {
                    objectName: "analysisOfferDescription"
                    Layout.fillWidth: true
                    Layout.preferredHeight: implicitHeight
                    description: root.offer.description || ""
                    fontPixelSize: Math.round(14 * root.fontScale)
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            AppButton {
                objectName: "analysisOfferSourceAction"
                text: "Offre d’origine"
                iconSource: Theme.icon("arrow-up-right")
                visible: Boolean(root.offer.url)
                refined: true
                quiet: true
                fontPixelSize: 11
                implicitHeight: 32
                onClicked: root.sourceRequested(String(root.offer.url))
            }
            AppButton {
                objectName: "analysisArchiveOfferAction"
                text: root.offer.status === "archived" ? "Archivée" : "Archiver"
                iconSource: Theme.icon("archive")
                kind: "warning"
                refined: true
                fontPixelSize: 11
                implicitHeight: 32
                enabled: root.archiveEnabled && root.offerId > 0
                onClicked: root.archiveRequested(root.offerId)
            }
        }
    }
}
