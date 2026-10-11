import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var offers: []
    property var selectedOffers: ({})
    property bool selectionEnabled: true
    property int focusedOfferId: 0
    property bool loading: false
    property bool searchActive: false
    property string sortKey: "published_date"
    property bool ascending: false
    property alias contentY: candidateList.contentY
    readonly property real contentHeight: candidateList.contentHeight
    readonly property real viewportHeight: candidateList.height
    readonly property real usableWidth: Math.max(0, width - Theme.scrollGutter - 24)
    readonly property real tableScale: Math.min(1, Math.max(0, (usableWidth - 540) / 440))
    readonly property int rowHeight: Math.round(68 + 4 * tableScale)
    readonly property int tableTextSize: Math.round(12 + 2 * tableScale)
    readonly property bool showExperience: usableWidth >= 520
    readonly property bool showDate: usableWidth >= 480
    readonly property int gap: Math.round(6 + 4 * tableScale)
    readonly property int selectWidth: 24
    readonly property int experienceWidth: showExperience ? Math.round(52 + 12 * tableScale) : 0
    readonly property int dateWidth: showDate ? Math.round(76 + 12 * tableScale) : 0
    readonly property int llmScoreWidth: Math.round(42 + 6 * tableScale)
    readonly property int operationDateWidth: Math.round(94 + 10 * tableScale)
    readonly property int archiveDateWidth: Math.round(110 + 10 * tableScale)
    readonly property int filterDispositionWidth: 2 * llmScoreWidth + gap
    // Match the reserved right-hand block of the other offer filters.
    readonly property int rightColumnsWidth: showDate ? Math.max(
        dateWidth + gap + filterDispositionWidth,
        llmScoreWidth + gap + Math.max(operationDateWidth, archiveDateWidth)) : 0
    readonly property int dateSpacerWidth: Math.max(0, rightColumnsWidth - dateWidth - gap)
    readonly property int visibleGaps: 3 + (showExperience ? 1 : 0) + (showDate ? 1 : 0)
    readonly property real textColumnsWidth: Math.max(0, usableWidth - selectWidth
        - experienceWidth - rightColumnsWidth - gap * visibleGaps)
    readonly property int companyWidth: Math.round(textColumnsWidth * 0.26)
    readonly property int locationWidth: Math.round(textColumnsWidth * 0.30)
    readonly property int titleWidth: Math.max(65, Math.round(textColumnsWidth
        - companyWidth - locationWidth))
    readonly property var sortedOffers: {
        var rows = (offers || []).slice()
        var key = sortKey
        var direction = ascending ? 1 : -1
        rows.sort(function(a, b) {
            var left = String(a[key] || "").toLocaleLowerCase()
            var right = String(b[key] || "").toLocaleLowerCase()
            if (key === "experience") {
                left = parseFloat(left)
                right = parseFloat(right)
                if (isNaN(left)) left = -1
                if (isNaN(right)) right = -1
            }
            if (left < right) return -direction
            if (left > right) return direction
            return Number(a.id || 0) - Number(b.id || 0)
        })
        return rows
    }
    signal selectionToggled(int jobId, bool checked)
    signal visibleSelectionToggled(bool checked)
    signal offerOpened(int jobId, string status)

    function selected(jobId) { return Boolean(selectedOffers[String(jobId)]) }
    function sortBy(key) {
        if (sortKey === key) ascending = !ascending
        else { sortKey = key; ascending = true }
    }
    function dateLabel(value) {
        if (!value) return "—"
        var date = new Date(String(value))
        return isNaN(date.getTime()) ? "—" : Qt.formatDate(date, "dd/MM/yyyy")
    }
    function sourceLabel(value) {
        var raw = String(value || "").trim()
        var key = raw.toLowerCase().replace(/[\s_-]+/g, "")
        if (key.indexOf("welcometothejungle") >= 0 || key === "wttj") return "WTTJ"
        if (key.indexOf("francetravail") >= 0) return "FRANCE TRAVAIL"
        if (key.indexOf("linkedin") >= 0) return "LINKEDIN"
        if (key.indexOf("serpapi") >= 0) return "WEB"
        return raw.length > 0 ? raw.replace(/[_-]+/g, " ").toUpperCase() : "SOURCE INCONNUE"
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            color: "#191720"
            radius: 0
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12 + Theme.scrollGutter
                spacing: root.gap
                Item {
                    Layout.preferredWidth: root.selectWidth
                    Layout.fillHeight: true
                    SelectionBox {
                        objectName: "analysisSelectVisible"
                        anchors.centerIn: parent
                        visible: root.selectionEnabled
                        checked: root.offers.length > 0 && root.offers.every(function(row) {
                            return root.selected(row.id)
                        })
                        partial: root.offers.some(function(row) { return root.selected(row.id) })
                            && !checked
                        enabled: root.offers.length > 0
                        Accessible.name: "Sélectionner les offres affichées"
                        onToggled: function(checked) { root.visibleSelectionToggled(checked) }
                    }
                }
                SortHeader {
                    objectName: "analysisCompanyHeader"
                    Layout.preferredWidth: root.companyWidth
                    title: "Entreprise"; sortKey: "company"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
                SortHeader {
                    objectName: "analysisTitleHeader"
                    Layout.preferredWidth: root.titleWidth
                    title: "Poste"; sortKey: "title"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
                SortHeader {
                    visible: root.showExperience
                    Layout.preferredWidth: root.experienceWidth
                    title: "Exp."; sortKey: "experience"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
                SortHeader {
                    objectName: "analysisLocationHeader"
                    Layout.preferredWidth: root.locationWidth
                    title: "Lieu"; sortKey: "location"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
                Item {
                    visible: root.showDate
                    Layout.preferredWidth: root.dateSpacerWidth
                    Layout.fillHeight: true
                }
                SortHeader {
                    objectName: "analysisPublishedDateHeader"
                    visible: root.showDate
                    Layout.preferredWidth: root.dateWidth
                    title: "Date"; sortKey: "published_date"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
            }
        }
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#2C2836" }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            ListView {
                id: candidateList
                objectName: "analysisCandidateList"
                anchors.fill: parent
                anchors.margins: 4
                clip: true
                model: root.sortedOffers
                reuseItems: true
                cacheBuffer: 420
                spacing: 2
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar { }
                delegate: Rectangle {
                    id: row
                    required property var modelData
                    width: Math.max(0, candidateList.width - Theme.scrollGutter)
                    height: root.rowHeight
                    radius: 9
                    readonly property bool current: Number(modelData.id) === root.focusedOfferId
                    color: current ? "#252034" : hover.hovered ? "#1D1B26"
                        : root.selected(modelData.id) ? "#1C1927" : "transparent"
                    border.color: current ? "#49405F" : "transparent"
                    border.width: Theme.lineWidth
                    Rectangle {
                        visible: parent.current
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        width: 2
                        height: 24
                        radius: 2
                        color: Theme.accentBright
                    }
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 8
                        anchors.rightMargin: 8
                        spacing: root.gap
                        Item {
                            Layout.preferredWidth: root.selectWidth
                            Layout.fillHeight: true
                            SelectionBox {
                                objectName: "analysisSelectOffer_" + modelData.id
                                anchors.centerIn: parent
                                visible: root.selectionEnabled
                                Accessible.name: "Sélectionner " + modelData.title + " chez " + modelData.company
                                checked: root.selected(modelData.id)
                                onToggled: function(checked) {
                                    root.selectionToggled(Number(modelData.id), checked)
                                }
                            }
                        }
                        ColumnLayout {
                            Layout.preferredWidth: root.companyWidth
                            spacing: 4
                            Text {
                                Layout.fillWidth: true
                                text: String(modelData.company || "—")
                                textFormat: Text.PlainText
                                color: Theme.inkSoft
                                font.pixelSize: root.tableTextSize - 1
                                font.weight: Font.Medium
                                maximumLineCount: 2
                                wrapMode: Text.WordWrap
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.sourceLabel(modelData.source)
                                color: Theme.inkFaint
                                font.pixelSize: 9
                                font.letterSpacing: 0.2
                                elide: Text.ElideRight
                            }
                        }
                        Text {
                            Layout.preferredWidth: root.titleWidth
                            text: String(modelData.title || "Offre sans titre")
                            textFormat: Text.PlainText
                            color: Theme.ink
                            font.pixelSize: root.tableTextSize
                            font.weight: Font.Medium
                            maximumLineCount: 2
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight
                            lineHeight: 1.2
                        }
                        Text {
                            visible: root.showExperience
                            Layout.preferredWidth: root.experienceWidth
                            text: String(modelData.experience || "—")
                            color: Theme.inkMuted
                            font.pixelSize: root.tableTextSize - 1
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.preferredWidth: root.locationWidth
                            text: String(modelData.location || "—")
                            textFormat: Text.PlainText
                            color: Theme.inkMuted
                            font.pixelSize: root.tableTextSize - 1
                            maximumLineCount: 2
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight
                            lineHeight: 1.2
                        }
                        Item {
                            visible: root.showDate
                            Layout.preferredWidth: root.dateSpacerWidth
                            Layout.fillHeight: true
                        }
                        Text {
                            visible: root.showDate
                            Layout.preferredWidth: root.dateWidth
                            text: root.dateLabel(modelData.published_date)
                            color: Theme.inkMuted
                            font.pixelSize: root.tableTextSize - 1
                            horizontalAlignment: Text.AlignHCenter
                        }
                    }
                    HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
                    TapHandler {
                        onTapped: function(eventPoint) {
                            // Checkboxes select for analysis; only the content
                            // area of the row opens the reading pane.
                            if (eventPoint.position.x >= root.selectWidth + 16)
                                root.offerOpened(Number(modelData.id),
                                    String(modelData.offer_status || "scraped"))
                        }
                    }
                    Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                    Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                }
            }
            EmptyState {
                anchors.centerIn: parent
                visible: root.offers.length === 0 && !root.loading
                title: root.searchActive ? "Aucune offre trouvée"
                    : root.selectionEnabled ? "Aucune offre à analyser" : "Aucune offre dans cette analyse"
                message: root.searchActive ? "Essayez un autre terme de recherche."
                    : root.selectionEnabled ? "Les nouvelles offres à analyser apparaîtront ici."
                        : "Sélectionnez une autre analyse dans l’historique."
                iconSource: Theme.icon("analysis")
            }
            BusyIndicator {
                anchors.centerIn: parent
                visible: root.loading && root.offers.length === 0
                running: visible
            }
        }
    }
}
