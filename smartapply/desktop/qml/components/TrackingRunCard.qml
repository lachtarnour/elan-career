import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Surface {
    id: root
    property var run: ({})
    property var historyOptions: []
    property int activeRunCount: 0
    property bool controlsEnabled: true
    property bool retryEnabled: true
    readonly property bool generation: run.kind === "generation"
    readonly property string runId: String(run.id || "")
    readonly property var errors: run.errors || []
    readonly property int total: Number(run.total || 0)
    readonly property int produced: Number(generation ? run.generated || 0 : run.analyzed || 0)
    readonly property int issueCount: Math.max(Number(run.failed || 0), errors.length)
    readonly property bool needsAction: ["blocked", "attention", "failed", "paused", "interrupted", "partial"].indexOf(run.status) >= 0
    readonly property var metrics: generation ? [
        {label: "Offres du lot", value: total, tone: "neutral"},
        {label: "Dossiers prêts", value: produced, tone: "success"},
        {label: "À vérifier", value: issueCount, tone: "warning"}
    ] : [
        {label: "Offres du lot", value: total, tone: "neutral"},
        {label: "Offres analysées", value: produced, tone: "success"},
        {label: "Écartées au préfiltre", value: Number(run.screened_out || 0), tone: "neutral"},
        {label: "À vérifier", value: issueCount, tone: "warning"}
    ]
    signal retryRequested(string runId)
    signal cancelRequested(string runId)
    signal abandonRequested(string runId)
    signal runSelected(string runId)
    padding: 0
    verticalPadding: 0
    contentSpacing: 10
    surfaceColor: "transparent"
    surfaceEndColor: "transparent"
    strokeColor: "transparent"

    function tone() {
        if (run.status === "completed") return "neutral"
        if (run.status === "failed") return "danger"
        if (needsAction || run.status === "expired") return "warning"
        if (run.status === "cancelled") return "neutral"
        return "accent"
    }

    function dateLabel(value) {
        if (!value) return ""
        var date = new Date(String(value))
        return isNaN(date.getTime()) ? "" : Qt.formatDateTime(date, "dd/MM/yyyy à HH:mm")
    }

    function actionMessage() {
        if (run.status === "blocked") return "Le traitement attend une reprise. Les résultats déjà obtenus sont conservés."
        if (run.status === "attention") return "L’envoi doit être vérifié avant de poursuivre."
        if (run.status === "partial") return "Une partie des offres reste à traiter. Vous pouvez reprendre les demandes restantes."
        return "Le traitement a été interrompu. Les étapes enregistrées sont conservées pour la reprise."
    }

    RowLayout {
        id: runHeader
        Layout.fillWidth: true
        spacing: 12
        Rectangle {
            Layout.preferredWidth: 36
            Layout.preferredHeight: 36
            radius: 10
            color: Theme.accentSoft
            border.color: "#453A64"
            SvgIcon {
                anchors.centerIn: parent
                width: 18; height: 18
                source: Theme.icon(root.generation ? "document" : "analysis")
                color: Theme.accentBright
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Text {
                    objectName: "analysisRunTitle"
                    Layout.fillWidth: true
                    text: (root.generation ? "Génération de " : "Analyse de ") + root.total
                        + (root.total === 1 ? " offre" : " offres")
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 19
                    font.weight: Font.DemiBold
                    font.letterSpacing: -0.4
                    elide: Text.ElideRight
                }
                Pill {
                    objectName: "analysisRunStatus"
                    text: root.run.status_label || String(root.run.status || "")
                    tone: root.tone()
                    compact: true
                }
            }
            Text {
                objectName: "trackingRunContext"
                Layout.fillWidth: true
                text: ["Traitement n°" + Number(root.run.sequence || 1),
                    root.dateLabel(root.run.created_at),
                    root.generation ? (root.run.mode === "immediate" ? "Immédiat" : "Batch") : "Batch"]
                    .filter(Boolean).join("   ·   ")
                color: Theme.inkMuted
                font.pixelSize: 11
                elide: Text.ElideRight
            }
        }
        ColumnLayout {
            visible: root.historyOptions.length > 1
            Layout.fillWidth: false
            Layout.preferredWidth: root.width < 850 ? 210 : 270
            Layout.minimumWidth: Layout.preferredWidth
            Layout.maximumWidth: Layout.preferredWidth
            spacing: 0
            AppSelect {
                objectName: "analysisRunSelector"
                Layout.fillWidth: true
                implicitHeight: 32
                fontPixelSize: 12
                textRole: "label"
                valueRole: "value"
                model: root.historyOptions
                currentIndex: {
                    for (var i = 0; i < root.historyOptions.length; ++i)
                        if (String(root.historyOptions[i].value) === root.runId) return i
                    return 0
                }
                onActivated: root.runSelected(String(currentValue))
                Accessible.name: root.generation ? "Choisir une génération dans l’historique"
                    : "Choisir une analyse dans l’historique"
            }
        }
    }

    RowLayout {
        visible: root.activeRunCount > 0 || Boolean(root.run.can_retry)
            || Boolean(root.run.can_cancel) || Boolean(root.run.can_abandon)
        Layout.fillWidth: true
        spacing: 10
        Text {
            objectName: "analysisActiveCount"
            visible: root.activeRunCount > 0
            text: root.activeRunCount + (root.activeRunCount === 1 ? " traitement à suivre" : " traitements à suivre")
            color: Theme.inkMuted
            font.pixelSize: 11
        }
        Item { Layout.fillWidth: true }
        AppButton {
            objectName: "analysisRetryAction"
            visible: Boolean(root.run.can_retry)
            text: "Reprendre"
            iconSource: Theme.icon("refresh")
            kind: "primary"
            refined: true
            implicitHeight: 30
            enabled: root.controlsEnabled && root.retryEnabled
            onClicked: root.retryRequested(root.runId)
        }
        AppButton {
            objectName: "analysisCancelAction"
            visible: Boolean(root.run.can_cancel)
            text: root.generation ? "Arrêter" : "Annuler le Batch"
            kind: "danger"
            refined: true
            quiet: true
            implicitHeight: 30
            enabled: root.controlsEnabled
            onClicked: root.cancelRequested(root.runId)
        }
        AppButton {
            objectName: "generationAbandonAction"
            visible: root.generation && Boolean(root.run.can_abandon)
            text: "Abandonner"
            kind: "warning"
            refined: true
            quiet: true
            implicitHeight: 30
            enabled: root.controlsEnabled
            onClicked: root.abandonRequested(root.runId)
        }
    }

    Rectangle {
        objectName: "trackingRunMetrics"
        Layout.fillWidth: true
        Layout.preferredHeight: 52
        radius: 12
        color: "transparent"
        border.color: "transparent"
        RowLayout {
            anchors.fill: parent
            anchors.margins: 0
            spacing: 20
            Repeater {
                model: root.metrics
                delegate: ColumnLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    spacing: 4
                    Text {
                        text: String(modelData.value)
                        color: modelData.value > 0 && modelData.tone === "warning" ? Theme.warning : Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 20
                        font.weight: Font.DemiBold
                        font.letterSpacing: -0.5
                    }
                    Text {
                        Layout.fillWidth: true
                        text: modelData.label
                        color: Theme.inkMuted
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }
            }
        }
    }

    Surface {
        visible: root.needsAction
        Layout.fillWidth: true
        padding: 10
        verticalPadding: 8
        radiusValue: 12
        surfaceColor: Theme.warningSoft
        strokeColor: Theme.warningLine
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            SvgIcon {
                Layout.preferredWidth: 17; Layout.preferredHeight: 17
                source: Theme.icon("alert-circle")
                color: Theme.warning
            }
            Text {
                objectName: "analysisRunError"
                Layout.fillWidth: true
                text: root.actionMessage()
                color: Theme.warning
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Text {
                text: root.generation ? "Parcours de génération" : "Parcours d’analyse"
                color: Theme.inkSoft
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
            Text {
                text: timeline.phases.length + " étapes"
                color: Theme.inkFaint
                font.pixelSize: 11
            }
        }
        AnalysisTimeline {
            id: timeline
            objectName: "analysisTimeline"
            Layout.fillWidth: true
            Layout.preferredHeight: 88
            run: root.run
            compactStages: true
            productStyle: true
        }
    }

    ColumnLayout {
        objectName: "analysisRunDetails"
        Layout.fillWidth: true
        spacing: 8
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Text {
                text: "Détail des étapes"
                color: Theme.ink
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
            Text {
                objectName: "analysisRunProvider"
                Layout.maximumWidth: root.width * 0.5
                text: [root.run.provider_label || (root.run.provider === "anthropic" ? "Anthropic" : root.run.provider === "openai" ? "OpenAI" : ""),
                    root.run.model || ""].filter(Boolean).join(" · ")
                color: Theme.inkFaint
                font.pixelSize: 11
                elide: Text.ElideRight
            }
        }
        GridLayout {
            objectName: "analysisBatchDetails"
            Layout.fillWidth: true
            columns: root.width >= 1140 ? 3 : root.width >= 800 ? 2 : 1
            columnSpacing: 8
            rowSpacing: 8
            Repeater {
                model: timeline.phases
                delegate: TrackingStageDetails {
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.fillHeight: true
                    stage: modelData
                    number: index + 1
                    sourcePhases: root.run.phase_batches || []
                    current: Boolean(root.run.is_active) && !root.needsAction
                        && modelData.sourcePhases.indexOf(root.run.phase) >= 0
                }
            }
        }
        ColumnLayout {
            objectName: "trackingRunIssues"
            visible: root.errors.length > 0
            Layout.fillWidth: true
            spacing: 8
            Text {
                text: root.errors.length + (root.errors.length === 1 ? " offre à vérifier" : " offres à vérifier")
                color: Theme.warning
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
            ListView {
                id: errorList
                objectName: "analysisFailedOffers"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(300, Math.max(80, contentHeight))
                model: root.errors
                clip: true
                reuseItems: true
                spacing: 8
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar { }
                delegate: Surface {
                    required property var modelData
                    width: errorList.width - Theme.scrollGutter
                    padding: 12
                    verticalPadding: 12
                    radiusValue: 10
                    surfaceColor: Theme.warningSoft
                    strokeColor: Theme.warningLine
                    Text {
                        Layout.fillWidth: true
                        text: "#" + modelData.job_id + (modelData.title ? " · " + modelData.title : "")
                        textFormat: Text.PlainText
                        color: Theme.inkSoft
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(modelData.message || "")
                        textFormat: Text.PlainText
                        color: Theme.inkMuted
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
        Text {
            objectName: "analysisLastCheck"
            Layout.fillWidth: true
            text: root.run.updated_at ? "Dernière mise à jour · " + root.dateLabel(root.run.updated_at)
                : "Lancé le " + root.dateLabel(root.run.created_at)
            color: Theme.inkFaint
            font.pixelSize: 11
        }
    }
}
