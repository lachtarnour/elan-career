import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    signal navigateRequested(string route)

    readonly property int jobCount: Number(AppBridge.dashboard.jobs || 0)
    readonly property int pendingCount: Number(AppBridge.dashboard.pending || 0)
    readonly property int readyCount: Number(AppBridge.dashboard.ready || 0)
    readonly property int sentCount: Number(AppBridge.dashboard.sent || 0)

    Flickable {
        anchors.fill: parent
        z: 1
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
                title: "Accueil"
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 13

                MetricCard {
                    Layout.fillWidth: true
                    label: "Offres"
                    value: String(root.jobCount)
                    accent: Theme.accent
                    iconSource: Theme.icon("search")
                    progress: AppBridge.dashboard.analysis_progress || 0
                    interactive: true
                    onActivated: root.navigateRequested("jobs?status=analyzed")
                }
                MetricCard {
                    Layout.fillWidth: true
                    label: "À analyser"
                    value: String(root.pendingCount)
                    accent: "#8A72F2"
                    iconSource: Theme.icon("briefcase")
                    progress: Math.min(1, root.pendingCount / Math.max(1, root.jobCount))
                    interactive: true
                    onActivated: root.navigateRequested("analysis")
                }
                MetricCard {
                    Layout.fillWidth: true
                    label: "Prêtes"
                    value: String(root.readyCount)
                    accent: Theme.success
                    iconSource: Theme.icon("check")
                    progress: AppBridge.dashboard.ready_progress || 0
                    interactive: true
                    onActivated: root.navigateRequested("jobs?status=ready_for_form_submission")
                }
                MetricCard {
                    Layout.fillWidth: true
                    label: "Envoyées"
                    value: String(root.sentCount)
                    accent: Theme.blue
                    iconSource: Theme.icon("arrow-up-right")
                    progress: AppBridge.dashboard.sent_progress || 0
                    interactive: true
                    onActivated: root.navigateRequested("jobs?status=sent")
                }
            }

            SectionTitle {
                title: "Envois par jour"
            }
            Item {
                id: dailyProgress
                Layout.fillWidth: true
                Layout.preferredHeight: 236
                property var chartPoints: AppBridge.dashboard.sent_by_day || []

                function chartMax() {
                    var maximum = 1
                    var points = chartPoints || []
                    for (var i = 0; i < points.length; ++i)
                        maximum = Math.max(maximum, Number(points[i].count || 0))
                    var step = maximum <= 20 ? 5 : maximum <= 100 ? 25 : 50
                    return Math.ceil(maximum / step) * step
                }

                onChartPointsChanged: plot.requestPaint()
                onWidthChanged: plot.requestPaint()
                onHeightChanged: plot.requestPaint()

                Item {
                    id: chartArea
                    anchors.fill: parent

                    Canvas {
                        id: plot
                        anchors.fill: parent

                        onPaint: {
                            var ctx = getContext("2d")
                            ctx.clearRect(0, 0, width, height)

                            var points = dailyProgress.chartPoints || []
                            var left = 34
                            var right = 14
                            var top = 17
                            var bottom = 29
                            var plotWidth = Math.max(1, width - left - right)
                            var plotHeight = Math.max(1, height - top - bottom)
                            var maximum = dailyProgress.chartMax()
                            var visibleCount = points.length
                            var hasData = false
                            for (var dataIndex = 0; dataIndex < points.length; ++dataIndex) {
                                if (Number(points[dataIndex].count || 0) > 0) {
                                    hasData = true
                                    break
                                }
                            }

                            ctx.font = "10px sans-serif"
                            ctx.fillStyle = Qt.rgba(0.69, 0.66, 0.75, 0.72)
                            ctx.textAlign = "right"

                            // Keep the reading frame quiet and regular so the
                            // curve stays legible without adding visual noise.
                            ctx.lineWidth = 1
                            for (var guideIndex = 0; guideIndex < 5; ++guideIndex) {
                                var guideRatio = guideIndex / 4
                                var guideY = top + plotHeight * (1 - guideRatio)
                                ctx.strokeStyle = guideIndex === 0
                                    ? Qt.rgba(0.55, 0.50, 0.75, 0.24)
                                    : Qt.rgba(0.55, 0.50, 0.75, 0.12)
                                ctx.beginPath()
                                ctx.moveTo(left, guideY)
                                ctx.lineTo(width - right, guideY)
                                ctx.stroke()
                            }

                            var levels = [0, maximum / 2, maximum]
                            for (var levelIndex = 0; levelIndex < levels.length; ++levelIndex) {
                                var level = levels[levelIndex]
                                if (level > maximum) continue
                                var levelY = top + plotHeight - (level / maximum) * plotHeight
                                ctx.fillText(String(level), left - 9, levelY + 3)
                                ctx.strokeStyle = Qt.rgba(0.55, 0.50, 0.75, 0.52)
                                ctx.beginPath()
                                ctx.moveTo(left - 5, levelY)
                                ctx.lineTo(left, levelY)
                                ctx.stroke()
                            }

                            if (points.length > 0 && hasData) {
                                var coordinates = []
                                for (var i = 0; i < points.length; ++i) {
                                    var value = Math.max(0, Number(points[i].count || 0))
                                    var x = points.length === 1
                                        ? left + plotWidth / 2
                                        : left + plotWidth * i / (points.length - 1)
                                    var y = top + plotHeight - (value / maximum) * plotHeight
                                    coordinates.push({x: x, y: y, value: value})
                                }

                                ctx.beginPath()
                                ctx.moveTo(coordinates[0].x, coordinates[0].y)
                                for (var lineIndex = 0; lineIndex < visibleCount - 1; ++lineIndex) {
                                    var first = coordinates[Math.max(0, lineIndex - 1)]
                                    var start = coordinates[lineIndex]
                                    var end = coordinates[lineIndex + 1]
                                    var last = coordinates[Math.min(visibleCount - 1, lineIndex + 2)]
                                    var minY = Math.min(start.y, end.y)
                                    var maxY = Math.max(start.y, end.y)
                                    var controlOneY = Math.max(minY, Math.min(maxY, start.y + (end.y - first.y) / 6))
                                    var controlTwoY = Math.max(minY, Math.min(maxY, end.y - (last.y - start.y) / 6))
                                    var controlOffset = (end.x - start.x) / 3
                                    ctx.bezierCurveTo(
                                        start.x + controlOffset, controlOneY,
                                        end.x - controlOffset, controlTwoY,
                                        end.x, end.y
                                    )
                                }
                                ctx.lineWidth = 2.4
                                ctx.lineJoin = "round"
                                ctx.lineCap = "round"
                                ctx.strokeStyle = "#B9AEFF"
                                ctx.stroke()

                                for (var dotIndex = 0; dotIndex < visibleCount; ++dotIndex) {
                                    var point = coordinates[dotIndex]
                                    ctx.beginPath()
                                    var pointRadius = dotIndex === visibleCount - 1 ? 4 : (dotIndex === 0 ? 3.5 : 2.2)
                                    ctx.arc(point.x, point.y, pointRadius, 0, Math.PI * 2)
                                    ctx.fillStyle = dotIndex === visibleCount - 1 ? "#E0DCFF" : (point.value === 0 ? "#716A8C" : "#8E7CFF")
                                    ctx.fill()
                                    if (dotIndex === visibleCount - 1 && visibleCount === coordinates.length) {
                                        ctx.beginPath()
                                        ctx.arc(point.x, point.y, 8, 0, Math.PI * 2)
                                        ctx.strokeStyle = Qt.rgba(0.66, 0.62, 1.0, 0.28)
                                        ctx.lineWidth = 1
                                        ctx.stroke()
                                    }
                                }

                                ctx.fillStyle = Qt.rgba(0.69, 0.66, 0.75, 0.72)
                                ctx.textAlign = "center"
                                var labelStep = points.length > 10 ? 3 : 2
                                for (var labelIndex = 0; labelIndex < points.length; labelIndex += labelStep)
                                    ctx.fillText(points[labelIndex].label, coordinates[labelIndex].x, height - 5)
                                if (points.length > 1 && (points.length - 1) % labelStep !== 0)
                                    ctx.fillText(points[points.length - 1].label, coordinates[points.length - 1].x, height - 5)
                            }
                        }
                    }

                    Text {
                        visible: {
                            var points = dailyProgress.chartPoints || []
                            if (points.length === 0) return true
                            for (var i = 0; i < points.length; ++i)
                                if (Number(points[i].count || 0) > 0) return false
                            return true
                        }
                        anchors.centerIn: parent
                        text: "Aucune candidature envoyée"
                        color: Theme.inkFaint
                        font.pixelSize: 11
                        font.weight: Font.Medium
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 5
                columnSpacing: 16
                rowSpacing: 16

                Surface {
                    id: recentApplicationsPanel
                    Layout.columnSpan: 5
                    Layout.fillWidth: true
                    Layout.preferredHeight: (AppBridge.dashboard.recent || []).length > 0 ? 258 : 238
                    padding: 18
                    surfaceEndColor: Theme.surface

                    SectionTitle {
                        title: "Candidatures récentes"
                        AppButton {
                            text: "Toutes"
                            implicitHeight: 36
                            iconSource: Theme.icon("chevron-right")
                            onClicked: root.navigateRequested("jobs")
                        }
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.fillHeight: true

                        EmptyState {
                            visible: (AppBridge.dashboard.recent || []).length === 0
                            anchors.centerIn: parent
                            iconSource: Theme.icon("files")
                            title: "Aucune candidature"
                        }

                        ColumnLayout {
                            visible: (AppBridge.dashboard.recent || []).length > 0
                            anchors.fill: parent
                            spacing: 3

                            Repeater {
                                model: (AppBridge.dashboard.recent || []).slice(0, 3)
                                delegate: Rectangle {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 56
                                    radius: 13
                                    color: recentHover.hovered ? Theme.surfaceHover : "transparent"
                                    border.color: recentHover.hovered ? Theme.accentLine : "transparent"

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 10
                                        anchors.rightMargin: 10
                                        spacing: 12

                                        Rectangle {
                                            Layout.preferredWidth: 35; Layout.preferredHeight: 35; radius: 11
                                            color: recentHover.hovered ? Theme.accentLine : Theme.accentSoft
                                            Text { anchors.centerIn: parent; text: modelData.initial; color: Theme.accentDark; font.pixelSize: 11; font.weight: Font.Bold }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 1
                                            Text { Layout.fillWidth: true; text: modelData.company; color: Theme.ink; font.pixelSize: 12; font.weight: Font.DemiBold; elide: Text.ElideRight }
                                            Text { Layout.fillWidth: true; text: modelData.title; color: Theme.inkMuted; font.pixelSize: 12; elide: Text.ElideRight }
                                        }
                                        Pill { text: modelData.status_label; tone: modelData.tone; compact: true }
                                        Text { text: modelData.updated_at; color: Theme.inkFaint; font.pixelSize: 11 }
                                        SvgIcon { source: Theme.icon("chevron-right"); color: recentHover.hovered ? Theme.accent : Theme.inkFaint; Layout.preferredWidth: 14; Layout.preferredHeight: 14 }
                                    }

                                    activeFocusOnTab: true
                                    Accessible.role: Accessible.Button
                                    Accessible.name: modelData.company + " — " + modelData.title
                                    Accessible.onPressAction: AppBridge.openApplication(modelData.id)
                                    Keys.onReturnPressed: AppBridge.openApplication(modelData.id)
                                    Keys.onSpacePressed: AppBridge.openApplication(modelData.id)
                                    HoverHandler { id: recentHover; cursorShape: Qt.PointingHandCursor }
                                    TapHandler {
                                        onTapped: {
                                            AppBridge.openApplication(modelData.id)
                                        }
                                    }
                                    Behavior on color { ColorAnimation { duration: 130 } }
                                    Behavior on border.color { ColorAnimation { duration: 130 } }
                                }
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }
                }

            }

        }
    }
}
