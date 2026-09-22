import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    signal navigateRequested(string route)

    function sourceStatusText(source) {
        if (source.state === "available") return "Disponible"
        if (source.state === "checking") return "Vérification…"
        if (source.state === "unavailable") return "Indisponible"
        return "À configurer"
    }
    function sourceStatusTone(source) {
        if (source.state === "available") return "success"
        if (source.state === "unavailable") return "danger"
        if (source.state === "checking") return "neutral"
        return "warning"
    }

    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight + 12
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: AppScrollBar { }

        ColumnLayout {
            id: content
            width: Math.min(1320, parent.width - Theme.scrollGutter)
            x: (parent.width - Theme.scrollGutter - width) / 2
            spacing: Theme.pageGap
            PageHeader {
                Layout.fillWidth: true
                title: "Réglages"
                AppButton {
                    text: AppBridge.diagnosticsChecking ? "Vérification…" : "Actualiser"
                    iconSource: Theme.icon("refresh")
                    enabled: !AppBridge.diagnosticsChecking
                    onClicked: AppBridge.refreshDiagnostics()
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.sectionGap
                Surface {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 3
                    Layout.alignment: Qt.AlignTop
                    SectionTitle { title: "Stockage local" }
                    Repeater {
                        model: [
                            {label: "Base de données", path: AppBridge.diagnostics.database_path || "", icon: Theme.icon("database")},
                            {label: "CV et lettres", path: AppBridge.diagnostics.output_dir || "", icon: Theme.icon("folder")},
                            {label: "Profil", path: AppBridge.diagnostics.profile_dir || "", icon: Theme.icon("user")},
                            {label: "Configuration", path: AppBridge.diagnostics.env_file || "", icon: Theme.icon("settings")}
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            implicitHeight: 76
                            radius: Theme.radiusMedium
                            color: pathHover.hovered ? Theme.surfaceHover : Theme.surfaceMuted
                            border.color: activeFocus ? Theme.accent : Theme.line
                            activeFocusOnTab: true
                            enabled: Boolean(modelData.path)
                            Accessible.role: Accessible.Button
                            Accessible.name: "Ouvrir " + modelData.label
                            Accessible.description: modelData.path
                            Accessible.onPressAction: AppBridge.openPath(modelData.path)
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 14
                                SvgIcon { source: modelData.icon; color: Theme.inkMuted; Layout.preferredWidth: 20; Layout.preferredHeight: 20 }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 5
                                    Text { text: modelData.label; color: Theme.inkSoft; font.pixelSize: 13; font.weight: Font.DemiBold }
                                    Text { Layout.fillWidth: true; text: modelData.path || "Non configuré"; color: Theme.inkMuted; font.pixelSize: 11; elide: Text.ElideMiddle }
                                }
                                SvgIcon { source: Theme.icon("arrow-up-right"); color: Theme.inkFaint; Layout.preferredWidth: 15; Layout.preferredHeight: 15 }
                            }
                            HoverHandler { id: pathHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: AppBridge.openPath(modelData.path) }
                            Keys.onReturnPressed: AppBridge.openPath(modelData.path)
                            Keys.onEnterPressed: AppBridge.openPath(modelData.path)
                            Keys.onSpacePressed: AppBridge.openPath(modelData.path)
                            ToolTip.visible: pathHover.hovered
                            ToolTip.delay: 700
                            ToolTip.text: modelData.path
                        }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 2
                    Layout.alignment: Qt.AlignTop
                    spacing: Theme.sectionGap
                    Surface {
                        Layout.fillWidth: true
                        SectionTitle { title: "Connexions" }
                        Repeater {
                            model: AppBridge.diagnostics.sources || []
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 8
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 12
                                    Text { Layout.fillWidth: true; text: modelData.label; color: Theme.inkSoft; font.pixelSize: 13; font.weight: Font.DemiBold; elide: Text.ElideRight }
                                    Pill { text: root.sourceStatusText(modelData); tone: root.sourceStatusTone(modelData); compact: true }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    visible: modelData.state !== "available" && text.length > 0
                                    text: modelData.message || ""
                                    color: Theme.inkMuted
                                    font.pixelSize: 12
                                    lineHeight: 1.25
                                    wrapMode: Text.Wrap
                                }
                                Item { Layout.preferredHeight: 4 }
                            }
                        }
                        Text {
                            visible: !(AppBridge.diagnostics.sources || []).length
                            Layout.fillWidth: true
                            text: "Aucune source configurée"
                            color: Theme.inkMuted
                            font.pixelSize: 13
                            wrapMode: Text.WordWrap
                        }
                    }
                    Surface {
                        Layout.fillWidth: true
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 14
                            Image { source: Qt.resolvedUrl("../../resources/app_icon.svg"); Layout.preferredWidth: 40; Layout.preferredHeight: 40; smooth: true; mipmap: true }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                Text { text: "Élan"; color: Theme.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                Text { text: "Version 0.1.0 · macOS"; color: Theme.inkMuted; font.pixelSize: 12 }
                            }
                        }
                        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
                        Text { text: "MODÈLE D’ANALYSE"; color: Theme.inkFaint; font.pixelSize: 10; font.weight: Font.DemiBold; font.letterSpacing: 1 }
                        Text {
                            Layout.fillWidth: true
                            text: [AppBridge.diagnostics.llm_provider, AppBridge.diagnostics.llm_model].filter(Boolean).join(" / ") || "Non configuré"
                            color: Theme.inkSoft
                            font.pixelSize: 13
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }
        }
    }
}
