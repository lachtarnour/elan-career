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
    readonly property bool showExperience: usableWidth >= 520
    readonly property bool showDate: usableWidth >= 480
    readonly property int gap: 6
    readonly property int selectWidth: 24
    readonly property int companyWidth: Math.min(240, Math.round(usableWidth * 0.16))
    readonly property int experienceWidth: showExperience ? (usableWidth >= 800 ? 70 : 50) : 0
    readonly property int locationWidth: Math.round(usableWidth * 0.16)
    readonly property int dateWidth: showDate ? (usableWidth >= 800 ? 80 : 68) : 0
    readonly property int stateWidth: usableWidth >= 900 ? 128 : 110
    readonly property int visibleGaps: 4 + (showExperience ? 1 : 0) + (showDate ? 1 : 0)
    readonly property int titleWidth: Math.max(65, Math.round(usableWidth - selectWidth
        - companyWidth - experienceWidth - locationWidth - dateWidth - stateWidth
        - gap * visibleGaps))
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
        return isNaN(date.getTime()) ? "—" : Qt.formatDate(date, "dd/MM/yy")
    }
    function sourceLabel(value) {
        var source = String(value || "").toLowerCase()
        if (source.indexOf("welcometothejungle") >= 0 || source === "wttj") return "WTTJ"
        if (source.indexOf("francetravail") >= 0) return "FRANCE TRAVAIL"
        if (source.indexOf("linkedin") >= 0) return "LINKEDIN"
        return source ? source.toUpperCase() : ""
    }

    Surface {
        objectName: "analysisTableSurface"
        anchors.fill: parent
        padding: 0
        elevated: false
        radiusValue: 16
        surfaceColor: "#121119"
        surfaceEndColor: surfaceColor
        strokeColor: "#2C2836"
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            color: "#191720"
            radius: 16
            Rectangle {
                anchors.left: parent.left; anchors.right: parent.right
                anchors.bottom: parent.bottom; height: 14; color: parent.color
            }
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
                    Layout.preferredWidth: root.locationWidth
                    title: "Lieu"; sortKey: "location"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
                SortHeader {
                    visible: root.showDate
                    Layout.preferredWidth: root.dateWidth
                    title: "Date"; sortKey: "published_date"
                    active: root.sortKey === sortKey; ascending: root.ascending
                    onSortRequested: function(key) { root.sortBy(key) }
                }
                SortHeader {
                    Layout.preferredWidth: root.stateWidth
                    objectName: "analysisStateHeader"
                    title: "État"
                    sortKey: "state_label"
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
                    height: 62
                    radius: 9
                    color: root.selected(modelData.id) ? "#252034"
                        : Number(modelData.id) === root.focusedOfferId ? Theme.surfaceRaised
                        : hover.hovered ? "#1D1B26" : "transparent"
                    border.color: root.selected(modelData.id) ? "#49405F"
                        : Number(modelData.id) === root.focusedOfferId ? Theme.lineStrong : "transparent"
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
                            spacing: 3
                            Text {
                                Layout.fillWidth: true
                                text: String(modelData.company || "—")
                                textFormat: Text.PlainText
                                color: Theme.inkSoft
                                font.pixelSize: 12
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
                                elide: Text.ElideRight
                            }
                        }
                        Text {
                            Layout.preferredWidth: root.titleWidth
                            text: String(modelData.title || "Offre sans titre")
                            textFormat: Text.PlainText
                            color: Theme.ink
                            font.pixelSize: 12
                            font.weight: Font.Medium
                            maximumLineCount: 2
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight
                        }
                        Text {
                            visible: root.showExperience
                            Layout.preferredWidth: root.experienceWidth
                            text: String(modelData.experience || "—")
                            color: Theme.inkMuted
                            font.pixelSize: 11
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.preferredWidth: root.locationWidth
                            text: String(modelData.location || "—")
                            textFormat: Text.PlainText
                            color: Theme.inkMuted
                            font.pixelSize: 11
                            maximumLineCount: 2
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight
                        }
                        Text {
                            visible: root.showDate
                            Layout.preferredWidth: root.dateWidth
                            text: root.dateLabel(modelData.published_date)
                            color: Theme.inkMuted
                            font.pixelSize: 11
                            horizontalAlignment: Text.AlignHCenter
                        }
                        Text {
                            Layout.preferredWidth: root.stateWidth
                            text: String(modelData.state_label || "À extraire")
                            color: root.selectionEnabled ? Theme.accentBright : Theme.inkMuted
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                            ToolTip.visible: statusHover.hovered
                            ToolTip.text: text
                            HoverHandler { id: statusHover }
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
