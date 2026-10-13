import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var offer: ({})
    property int requestedId: 0
    property bool loading: false
    property bool archiveEnabled: false
    property bool deleteEnabled: false
    property bool trackingAvailable: false
    property bool trackingExpanded: false
    property real pageWidth: 1400
    property real textZoom: 1.15
    readonly property int offerId: Number(offer.id || 0)
    readonly property bool hasAnalysis: Boolean(offer.has_analysis)
    readonly property real fontScale: Math.min(1.25, Math.max(1, pageWidth / 1400)) * textZoom
    signal sourceRequested(string url)
    signal retryRequested()
    signal archiveRequested(int jobId)
    signal deleteRequested(int jobId, string title, string company)
    signal trackingToggled()
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
        spacing: 12

        Flickable {
            id: detailViewport
            objectName: "analysisOfferDetailViewport"
            Layout.fillWidth: true
            Layout.fillHeight: true
            opacity: root.offerId > 0 ? 1 : 0
            enabled: root.offerId > 0
            contentWidth: width
            contentHeight: detailContent.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            ScrollBar.vertical: AppScrollBar { }

            ColumnLayout {
                id: detailContent
                width: detailViewport.width - Theme.scrollGutter
                spacing: 10

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
                        opacity: hovered || visualFocus || zoomPopup.opened ? 1 : 0.45
                        Accessible.name: "Ajuster la taille du texte de l’offre"
                        onClicked: zoomPopup.opened ? zoomPopup.close() : zoomPopup.open()
                        Behavior on opacity { NumberAnimation { duration: 130 } }
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
                                    fontPixelSize: 11
                                    quiet: true
                                    enabled: root.textZoom > 0.85
                                    onClicked: root.textZoom = Math.max(0.85, root.textZoom - 0.1)
                                }
                                AppButton {
                                    text: "Aa"
                                    Layout.fillWidth: true
                                    implicitWidth: 32; implicitHeight: 30
                                    fontPixelSize: 11
                                    quiet: true
                                    onClicked: root.textZoom = 1.15
                                }
                                AppButton {
                                    text: "A+"
                                    Layout.fillWidth: true
                                    implicitWidth: 32; implicitHeight: 30
                                    fontPixelSize: 11
                                    quiet: true
                                    enabled: root.textZoom < 1.55
                                    onClicked: root.textZoom = Math.min(1.55, root.textZoom + 0.1)
                                }
                            }
                        }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    Text {
                        Layout.maximumWidth: Math.max(150, detailContent.width * 0.42)
                        text: root.offer.company || "Entreprise non indiquée"
                        textFormat: Text.PlainText
                        color: Theme.ink
                        font.pixelSize: Math.round(11 * root.fontScale)
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }
                    Text { text: "·"; color: Theme.inkFaint; font.pixelSize: Math.round(11 * root.fontScale) }
                    Text {
                        Layout.fillWidth: true
                        text: root.offer.location || "Lieu non indiqué"
                        textFormat: Text.PlainText
                        color: Theme.inkMuted
                        font.pixelSize: Math.round(11 * root.fontScale)
                        elide: Text.ElideRight
                    }
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                OfferAnalysisSummary {
                    objectName: "analysisOfferSummary"
                    visible: root.hasAnalysis
                    Layout.fillWidth: true
                    offer: root.offer
                    fontPixelSize: Math.round(11 * root.fontScale)
                    labelFontPixelSize: Math.round(11 * root.fontScale)
                    fitObjectName: "analysisOfferFitEvaluation"
                }
                RowLayout {
                    visible: !root.hasAnalysis
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    spacing: 8
                    Rectangle { Layout.preferredWidth: 3; Layout.preferredHeight: 13; radius: 2; color: Theme.accent }
                    FormLabel { text: "DESCRIPTION DU POSTE"; fontPixelSize: Math.round(11 * root.fontScale) }
                    Item { Layout.fillWidth: true }
                }
                OfferDescription {
                    objectName: "analysisOfferDescription"
                    visible: !root.hasAnalysis
                    Layout.fillWidth: true
                    Layout.preferredHeight: implicitHeight
                    description: root.offer.description || ""
                    fontPixelSize: Math.round(14 * root.fontScale)
                }
            }
        }
        RowLayout {
            objectName: "analysisOfferActions"
            Layout.fillWidth: true
            visible: root.offerId > 0 || root.trackingAvailable
            spacing: 8
            AppButton {
                objectName: "analysisOfferSourceAction"
                text: "Offre d’origine"
                iconSource: Theme.icon("arrow-up-right")
                visible: root.offerId > 0 && Boolean(root.offer.url)
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
                visible: root.offerId > 0
                refined: true
                fontPixelSize: 11
                implicitHeight: 32
                enabled: root.archiveEnabled && root.offerId > 0
                onClicked: root.archiveRequested(root.offerId)
            }
            AppButton {
                objectName: "analysisDeleteOfferAction"
                text: "Supprimer"
                iconSource: Theme.icon("trash")
                kind: "danger"
                visible: root.offerId > 0
                refined: true
                fontPixelSize: 11
                implicitHeight: 32
                enabled: root.deleteEnabled
                onClicked: root.deleteRequested(root.offerId,
                    String(root.offer.title || ""), String(root.offer.company || ""))
            }
            Item { Layout.fillWidth: true }
            AppButton {
                objectName: "analysisToggleDetails"
                visible: root.trackingAvailable
                text: root.trackingExpanded ? "Masquer" : "Voir avancement"
                iconSource: Theme.icon("sliders")
                refined: true
                quiet: true
                fontPixelSize: 11
                implicitHeight: 32
                implicitWidth: contentItem.implicitWidth + 14
                leftPadding: 6
                rightPadding: 6
                onClicked: root.trackingToggled()
                Accessible.name: text
            }
        }
    }
}
