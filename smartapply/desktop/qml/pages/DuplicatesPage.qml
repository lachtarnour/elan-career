import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "duplicatesPage"
    signal navigateRequested(string route)

    property string query: ""
    property int selectedId: Number(AppBridge.currentJob.id || 0)
    property var rows: AppBridge.jobs || []
    property bool groupCopies: true
    property var groups: []
    property var expandedGroups: ({})
    property var visibleGroups: []
    function filteredGroups() {
        var needle = String(query || "").trim().toLowerCase()
        if (!needle) return groups
        return groups.filter(function(group) {
            return group.rows.concat(group.versions).some(function(row) {
                return [row.company, row.title, row.location, row.source, row.id]
                    .join(" ").toLowerCase().indexOf(needle) >= 0
            })
        })
    }
    readonly property int visibleRelationCount: visibleGroups.reduce(function(count, group) {
        return count + group.rows.length
    }, 0)
    property var selectedOffer: AppBridge.currentJob || ({})
    property var candidate: selectedOffer.duplicate_candidate || ({})
    readonly property bool selectedPairBusy: (AppBridge.busyJobIds || []).indexOf(Number(selectedId)) >= 0
        || (AppBridge.busyJobIds || []).indexOf(Number(candidate.id || 0)) >= 0
    property int displayedJobId: 0
    property bool hasSelection: Boolean(selectedOffer.id) && Boolean(candidate.id)
    property int decisionJobId: 0
    property var decisionOrder: []
    property bool decisionSaved: false

    function visiblePairIds() {
        var ids = []
        visibleGroups.forEach(function(group) {
            ids.push(Number(group.representative.id))
            group.rows.forEach(function(row) {
                if (Number(row.id) !== Number(group.representative.id)) ids.push(Number(row.id))
            })
        })
        return ids
    }

    function cancelAdvancement() {
        decisionJobId = 0
        decisionOrder = []
        decisionSaved = false
    }

    function resolveSelectedPair(sameOffer) {
        if (decisionJobId || !hasSelection) return
        var ids = visiblePairIds()
        var index = ids.indexOf(selectedId)
        decisionOrder = index < 0 ? ids : ids.slice(index + 1).concat(ids.slice(0, index))
        decisionJobId = selectedId
        decisionSaved = false
        AppBridge.resolveDuplicate(selectedId, sameOffer)
    }

    function advanceAfterDecision() {
        if (!decisionSaved || !visible || selectedId !== decisionJobId) return
        // The refreshed list is the authority: a confirmation alone is not
        // enough, and resolving a pair can remove other pending relations.
        if (rows.some(function(row) { return Number(row.id) === decisionJobId })) return
        var remaining = visiblePairIds()
        var nextId = decisionOrder.find(function(id) { return remaining.indexOf(id) >= 0 })
        if (nextId === undefined) nextId = remaining.length ? remaining[0] : 0
        cancelAdvancement()
        AppBridge.selectJob(nextId)
        Qt.callLater(function() {
            var index = visibleGroups.findIndex(function(group) {
                return group.rows.some(function(row) { return Number(row.id) === nextId })
            })
            if (index >= 0) duplicateList.positionViewAtIndex(index, ListView.Contain)
        })
    }

    function presentationGroups(offers) {
        var result = []
        var byKey = ({})
        offers.forEach(function(row) {
            var ids = groupCopies ? (row.strict_copy_group_ids || []) : []
            var key = ids.length > 1
                ? "copies:" + ids.slice().sort(function(a, b) { return Number(a) - Number(b) }).join(":")
                : "pair:" + row.id
            var group = byKey[key]
            if (!group) {
                group = { key: key, rows: [], representative: row,
                    versions: row.strict_copy_versions || [], ids: ids }
                byKey[key] = group
                result.push(group)
            }
            group.rows.push(row)
            // The service orders copy IDs by publication/fetch date. Keep the
            // most recent visible pair as representative, with a stable key.
            if (ids.length > 1 && ids.indexOf(Number(row.id)) < ids.indexOf(Number(group.representative.id)))
                group.representative = row
        })
        return result
    }

    function updateVisibleGroups() {
        var previousY = duplicateList ? duplicateList.contentY : 0
        visibleGroups = filteredGroups()
        Qt.callLater(function() {
            duplicateList.contentY = Math.min(previousY,
                Math.max(0, duplicateList.contentHeight - duplicateList.height))
        })
    }

    function rebuildGroups() {
        groups = presentationGroups(rows)
        updateVisibleGroups()
    }

    onRowsChanged: {
        rebuildGroups()
        Qt.callLater(advanceAfterDecision)
    }
    onQueryChanged: {
        cancelAdvancement()
        updateVisibleGroups()
    }
    onGroupCopiesChanged: {
        cancelAdvancement()
        rebuildGroups()
    }
    onVisibleChanged: if (!visible) cancelAdvancement()
    onSelectedIdChanged: if (decisionJobId && selectedId !== decisionJobId) cancelAdvancement()

    function groupContainsSelection(group) {
        return group.rows.some(function(row) { return Number(row.id) === root.selectedId })
    }

    function toggleGroup(key) {
        var updated = Object.assign({}, expandedGroups)
        updated[key] = !Boolean(updated[key])
        expandedGroups = updated
    }

    function expandSelectedGroup() {
        var group = groups.find(function(value) { return groupContainsSelection(value) })
        if (group && group.versions.length > 1 && !expandedGroups[group.key]) {
            var updated = Object.assign({}, expandedGroups)
            updated[group.key] = true
            expandedGroups = updated
        }
    }

    function classificationLabel(value) {
        if (value === "rejected") return "Rejeté"
        return value ? "À classer" : ""
    }

    function versionDate(version) {
        return version.published_date && version.published_date !== "—"
            ? "Publiée le " + version.published_date
            : "Collectée le " + (version.scraped_at || "—")
    }

    function confidenceText(value) {
        if (value === undefined || value === null || value === "") return "—"
        return Math.round(Number(value) * 100) + "% de similarité"
    }

    function confidenceTone(value) {
        var score = Number(value || 0)
        return score >= 0.85 ? "warning" : "accent"
    }

    function sourceLabel(value) {
        var labels = {
            serpapi: "Google Jobs",
            francetravail: "France Travail",
            linkedin: "LinkedIn",
            welcometothejungle: "Welcome to the Jungle"
        }
        return labels[String(value || "")] || String(value || "Source inconnue")
    }

    function metadataText(offer) {
        var values = []
        if (offer.contract) values.push(offer.contract)
        if (offer.remote) values.push(offer.remote)
        return values.join(" · ")
    }

    function descriptionPreview(value) {
        var text = String(value || "").replace(/\s+/g, " ").trim()
        if (text.length > 560) return text.slice(0, 560) + "…"
        return text || "Description indisponible."
    }

    function selectRow(row) {
        cancelAdvancement()
        if (row && row.id) AppBridge.selectJob(Number(row.id))
    }

    Connections {
        target: AppBridge
        function onDuplicateResolutionFinished(jobId, resolved) {
            if (jobId !== root.decisionJobId) return
            if (!resolved) root.cancelAdvancement()
            else {
                root.decisionSaved = true
                Qt.callLater(root.advanceAfterDecision)
            }
        }
        function onDuplicatePendingOnlyChanged() { root.cancelAdvancement() }
        function onCurrentJobChanged() {
            if (root.displayedJobId !== root.selectedId) {
                root.displayedJobId = root.selectedId
                compareScroll.contentY = 0
                root.expandSelectedGroup()
            }
        }
    }
    Component.onCompleted: {
        rebuildGroups()
        expandSelectedGroup()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.pageGap

        PageHeader {
            Layout.fillWidth: true
            title: "Doublons"
            CheckBox {
                id: groupCopiesControl
                objectName: "groupDuplicateCopiesSwitch"
                text: "Regrouper les copies"
                checked: root.groupCopies
                font.pixelSize: 12
                Accessible.name: "Regrouper visuellement les copies de contenu identique"
                ToolTip.visible: hovered
                ToolTip.text: "Une carte dépliable conserve chaque version et chaque rapprochement."
                onToggled: root.groupCopies = checked
                indicator: Rectangle {
                    implicitWidth: 18
                    implicitHeight: 18
                    x: groupCopiesControl.leftPadding
                    y: parent.height / 2 - height / 2
                    radius: 5
                    color: groupCopiesControl.checked ? Theme.accent : Theme.surfaceMuted
                    border.color: groupCopiesControl.checked ? Theme.accentBright : Theme.line
                    Text {
                        anchors.centerIn: parent
                        text: "✓"
                        visible: groupCopiesControl.checked
                        color: Theme.ink
                        font.pixelSize: 13
                    }
                }
                contentItem: Text {
                    text: groupCopiesControl.text
                    font: groupCopiesControl.font
                    color: Theme.inkSoft
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: groupCopiesControl.indicator.width + groupCopiesControl.spacing
                }
            }
            Switch {
                id: pendingOnlySwitch
                objectName: "pendingDuplicatesOnlySwitch"
                text: "Deux offres à classer"
                checked: AppBridge.duplicatePendingOnly
                font.pixelSize: 12
                spacing: 8
                Accessible.name: "Afficher seulement les doublons dont les deux offres sont à classer"
                ToolTip.visible: hovered
                ToolTip.text: "Garde les paires dont les deux offres sont À classer, avant l’évaluation de pertinence."
                onToggled: AppBridge.setDuplicatePendingOnly(checked)
                indicator: Rectangle {
                    implicitWidth: 40
                    implicitHeight: 22
                    x: pendingOnlySwitch.leftPadding
                    y: parent.height / 2 - height / 2
                    radius: height / 2
                    color: pendingOnlySwitch.checked ? Theme.accent : Theme.surfaceMuted
                    border.color: pendingOnlySwitch.checked ? Theme.accentBright : Theme.line
                    Rectangle {
                        width: 16
                        height: 16
                        radius: 8
                        x: pendingOnlySwitch.checked ? parent.width - width - 3 : 3
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.ink
                    }
                }
                contentItem: Text {
                    text: pendingOnlySwitch.text
                    font: pendingOnlySwitch.font
                    color: Theme.inkSoft
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: pendingOnlySwitch.indicator.width + pendingOnlySwitch.spacing
                }
            }
            Pill {
                text: root.rows.length + " rapprochements · " + root.groups.length + " cartes"
                tone: root.rows.length > 0 ? "warning" : "success"
            }
            AppButton {
                text: "Actualiser"
                iconSource: Theme.icon("refresh")
                enabled: !AppBridge.shutdownRequested && !AppBridge.jobsLoading
                onClicked: AppBridge.loadJobs("", "duplicate_review", "duplicate_confidence", false)
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.rows.length === 0 && !AppBridge.jobsLoading
            EmptyState {
                anchors.centerIn: parent
                title: AppBridge.duplicatePendingOnly ? "Aucune paire avec deux offres à classer" : "Aucun doublon à vérifier"
                iconSource: Theme.icon("check")
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.rows.length > 0 || AppBridge.jobsLoading
            spacing: 16

            Surface {
                Layout.preferredWidth: Math.min(400, Math.max(320, root.width * 0.30))
                Layout.minimumWidth: 300
                Layout.fillHeight: true
                padding: 16
                surfaceEndColor: Theme.surface

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 12
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            Layout.fillWidth: true
                            text: "À vérifier"
                            color: Theme.inkSoft
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }
                        Text {
                            objectName: "duplicateRelationCount"
                            text: root.visibleRelationCount + " / " + root.rows.length
                            color: Theme.inkMuted
                            font.pixelSize: 12
                        }
                    }
                    AppField {
                        Layout.fillWidth: true
                        implicitHeight: 40
                        iconSource: Theme.icon("search")
                        placeholderText: "Filtrer les rapprochements…"
                        onTextChanged: root.query = text
                    }
                    ListView {
                        id: duplicateList
                        objectName: "duplicateGroupList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 8
                        model: root.visibleGroups
                        ScrollBar.vertical: AppScrollBar { }
                        delegate: Rectangle {
                            id: groupCard
                            required property var modelData
                            readonly property var representative: modelData.representative
                            readonly property bool selected: root.groupContainsSelection(modelData)
                            readonly property bool expanded: Boolean(root.expandedGroups[modelData.key])
                            width: duplicateList.width - Theme.scrollGutter
                            height: groupContent.implicitHeight + 16
                            radius: 14
                            color: selected ? Theme.accentSoft : Theme.surfaceMuted
                            border.color: selected ? Theme.accentLine : Theme.line
                            border.width: selected ? 1.5 : 1
                            ColumnLayout {
                                id: groupContent
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 8
                                spacing: 7
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.margins: 4
                                    spacing: 3
                                    Text {
                                        Layout.fillWidth: true
                                        text: groupCard.representative.company || "Entreprise inconnue"
                                        color: Theme.inkSoft
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: groupCard.representative.title || "Titre indisponible"
                                        color: Theme.ink
                                        font.pixelSize: 12
                                        font.weight: Font.Medium
                                        elide: Text.ElideRight
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 7
                                        Text {
                                            Layout.fillWidth: true
                                            text: root.sourceLabel(groupCard.representative.source)
                                            color: Theme.inkFaint
                                            font.pixelSize: 12
                                            elide: Text.ElideRight
                                        }
                                        Pill {
                                            text: root.confidenceText(groupCard.representative.duplicate_confidence).replace(" de similarité", "")
                                            tone: root.confidenceTone(groupCard.representative.duplicate_confidence)
                                            compact: true
                                        }
                                    }
                                    AppButton {
                                        Layout.fillWidth: true
                                        objectName: "duplicateRepresentativeAction_" + groupCard.representative.id
                                        text: "Comparer #" + groupCard.representative.id + " avec #" + groupCard.representative.possible_duplicate_of_id
                                        implicitHeight: 30
                                        onClicked: root.selectRow(groupCard.representative)
                                    }
                                }
                                AppButton {
                                    Layout.fillWidth: true
                                    visible: groupCard.modelData.versions.length > 1
                                    objectName: "duplicateCopiesToggle_" + groupCard.representative.id
                                    text: (groupCard.expanded ? "Masquer" : "Afficher") + " "
                                        + groupCard.modelData.versions.length + " versions · "
                                        + groupCard.modelData.rows.length + " rapprochement"
                                        + (groupCard.modelData.rows.length > 1 ? "s" : "")
                                    implicitHeight: 32
                                    onClicked: root.toggleGroup(groupCard.modelData.key)
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    visible: groupCard.expanded && groupCard.modelData.versions.length > 1
                                    spacing: 7
                                    Text {
                                        Layout.fillWidth: true
                                        text: "Contenu identique. Chaque rapprochement reste à décider séparément."
                                        color: Theme.inkMuted
                                        font.pixelSize: 11
                                        wrapMode: Text.WordWrap
                                    }
                                    Repeater {
                                        model: groupCard.modelData.versions
                                        delegate: Rectangle {
                                            id: versionCard
                                            required property var modelData
                                            Layout.fillWidth: true
                                            implicitHeight: versionContent.implicitHeight + 16
                                            radius: 9
                                            color: Theme.surface
                                            border.color: Theme.line
                                            ColumnLayout {
                                                id: versionContent
                                                anchors.left: parent.left
                                                anchors.right: parent.right
                                                anchors.top: parent.top
                                                anchors.margins: 8
                                                spacing: 3
                                                RowLayout {
                                                    Layout.fillWidth: true
                                                    Text {
                                                        Layout.fillWidth: true
                                                        text: "#" + versionCard.modelData.id + " · " + root.sourceLabel(versionCard.modelData.source)
                                                        color: Theme.inkSoft
                                                        font.pixelSize: 12
                                                        font.weight: Font.DemiBold
                                                        elide: Text.ElideRight
                                                    }
                                                    AppButton {
                                                        text: "Ouvrir"
                                                        iconSource: Theme.icon("arrow-up-right")
                                                        implicitHeight: 26
                                                        visible: Boolean(versionCard.modelData.url)
                                                        onClicked: AppBridge.openUrl(versionCard.modelData.url)
                                                    }
                                                }
                                                Text {
                                                    Layout.fillWidth: true
                                                    text: root.versionDate(versionCard.modelData)
                                                    color: Theme.inkFaint
                                                    font.pixelSize: 11
                                                    wrapMode: Text.WordWrap
                                                }
                                                Text {
                                                    Layout.fillWidth: true
                                                    text: (versionCard.modelData.location || "Localisation inconnue") + " · "
                                                        + versionCard.modelData.status_label
                                                        + (versionCard.modelData.filter_disposition
                                                            ? " · " + root.classificationLabel(versionCard.modelData.filter_disposition) : "")
                                                    color: Theme.inkMuted
                                                    font.pixelSize: 11
                                                    wrapMode: Text.WordWrap
                                                }
                                            }
                                        }
                                    }
                                    Repeater {
                                        model: groupCard.modelData.rows
                                        delegate: AppButton {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            objectName: "duplicatePairAction_" + modelData.id
                                            text: "Comparer #" + modelData.id + " avec #" + modelData.possible_duplicate_of_id
                                            kind: Number(modelData.id) === root.selectedId ? "primary" : "secondary"
                                            implicitHeight: 32
                                            onClicked: root.selectRow(modelData)
                                        }
                                    }
                                }
                            }
                        }
                        EmptyState {
                            anchors.centerIn: parent
                            visible: root.visibleGroups.length === 0 && !AppBridge.jobsLoading
                            title: root.rows.length === 0
                                ? (AppBridge.duplicatePendingOnly ? "Aucune paire avec deux offres à classer" : "Tout est traité")
                                : "Aucun résultat"
                            iconSource: Theme.icon(root.rows.length === 0 ? "check" : "search")
                        }
                    }
                }
            }

            Surface {
                Layout.fillWidth: true
                Layout.fillHeight: true
                padding: 20
                surfaceEndColor: Theme.surface

                Flickable {
                    id: compareScroll
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    contentWidth: width
                    contentHeight: compareContent.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { }

                    ColumnLayout {
                        id: compareContent
                        width: compareScroll.width - Theme.scrollGutter
                        spacing: 14
                        visible: root.hasSelection
                        opacity: AppBridge.jobDetailLoading ? 0.4 : 1
                        Behavior on opacity { NumberAnimation { duration: 120 } }

                        RowLayout {
                            Layout.fillWidth: true
                            Item { Layout.fillWidth: true }
                            Pill {
                                text: root.confidenceText(root.selectedOffer.duplicate_confidence)
                                tone: root.confidenceTone(root.selectedOffer.duplicate_confidence)
                                compact: true
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12

                            Surface {
                                Layout.fillWidth: true
                                Layout.preferredWidth: 1
                                Layout.alignment: Qt.AlignTop
                                padding: 16
                                surfaceColor: Theme.surfaceMuted
                                surfaceEndColor: Theme.surfaceMuted
                                strokeColor: Theme.accentLine
                                SectionTitle {
                                    title: "Offre récente"
                                    caption: "#" + root.selectedOffer.id
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.selectedOffer.title || "Titre indisponible"
                                    color: Theme.ink
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: (root.selectedOffer.company || "Entreprise inconnue") + (root.selectedOffer.location ? " · " + root.selectedOffer.location : "")
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.metadataText(root.selectedOffer)
                                    visible: text.length > 0
                                    color: Theme.inkMuted
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { Layout.fillWidth: true; text: root.sourceLabel(root.selectedOffer.source); color: Theme.accentBright; font.pixelSize: 12; font.weight: Font.DemiBold }
                                    AppButton {
                                        text: "Ouvrir"
                                        iconSource: Theme.icon("arrow-up-right")
                                        implicitHeight: 30
                                        enabled: Boolean(root.selectedOffer.url)
                                        onClicked: AppBridge.openUrl(root.selectedOffer.url)
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.descriptionPreview(root.selectedOffer.description)
                                    color: Theme.inkSoft
                                    font.pixelSize: 13
                                    lineHeight: 1.3
                                    wrapMode: Text.WordWrap
                                }
                                Pill {
                                    visible: Boolean(root.selectedOffer.application && root.selectedOffer.application.id)
                                    text: root.selectedOffer.application ? (root.selectedOffer.application.status_label || "Candidature existante") : ""
                                    tone: root.selectedOffer.application && root.selectedOffer.application.status === "archived" ? "warning" : "neutral"
                                    compact: true
                                }
                            }

                            Surface {
                                Layout.fillWidth: true
                                Layout.preferredWidth: 1
                                Layout.alignment: Qt.AlignTop
                                padding: 16
                                surfaceColor: Theme.surfaceMuted
                                surfaceEndColor: Theme.surfaceMuted
                                strokeColor: Theme.warningLine
                                SectionTitle {
                                    title: "Offre déjà connue"
                                    caption: "#" + root.candidate.id
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.candidate.title || "Titre indisponible"
                                    color: Theme.ink
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: (root.candidate.company || "Entreprise inconnue") + (root.candidate.location ? " · " + root.candidate.location : "")
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.metadataText(root.candidate)
                                    visible: text.length > 0
                                    color: Theme.inkMuted
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { Layout.fillWidth: true; text: root.sourceLabel(root.candidate.source); color: Theme.warning; font.pixelSize: 12; font.weight: Font.DemiBold }
                                    AppButton {
                                        text: "Ouvrir"
                                        iconSource: Theme.icon("arrow-up-right")
                                        implicitHeight: 30
                                        enabled: Boolean(root.candidate.url)
                                        onClicked: AppBridge.openUrl(root.candidate.url)
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.descriptionPreview(root.candidate.description)
                                    color: Theme.inkSoft
                                    font.pixelSize: 13
                                    lineHeight: 1.3
                                    wrapMode: Text.WordWrap
                                }
                                Pill {
                                    visible: Boolean(root.candidate.application_id)
                                    text: root.candidate.application_status_label || "Candidature existante"
                                    tone: "success"
                                    compact: true
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: decisionContent.implicitHeight + 28
                            radius: 16
                            color: Theme.warningSoft
                            border.color: Theme.warningLine
                            ColumnLayout {
                                id: decisionContent
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 9
                                Text {
                                    Layout.fillWidth: true
                                    text: "Décision pour les offres #" + root.selectedId + " et #" + root.candidate.id
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                }
                                Text {
                                    visible: root.selectedPairBusy || root.decisionJobId !== 0
                                    Layout.fillWidth: true
                                    text: root.decisionJobId
                                        ? "Enregistrement de la décision…"
                                        : "Une offre de cette paire est en cours de traitement. Décision disponible à sa fin."
                                    color: Theme.warning
                                    wrapMode: Text.WordWrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 9
                                    AppButton {
                                        Layout.fillWidth: true
                                        objectName: "duplicateConfirmAction"
                                        text: "Même offre"
                                        iconSource: Theme.icon("check")
                                        kind: "primary"
                                        implicitHeight: 42
                                        enabled: !AppBridge.shutdownRequested && !root.selectedPairBusy && !root.decisionJobId && !AppBridge.jobDetailLoading && !AppBridge.jobsLoading
                                        onClicked: root.resolveSelectedPair(true)
                                    }
                                    AppButton {
                                        Layout.fillWidth: true
                                        objectName: "duplicateDistinctAction"
                                        text: "Offres différentes"
                                        iconSource: Theme.icon("files")
                                        implicitHeight: 42
                                        enabled: !AppBridge.shutdownRequested && !root.selectedPairBusy && !root.decisionJobId && !AppBridge.jobDetailLoading && !AppBridge.jobsLoading
                                        onClicked: root.resolveSelectedPair(false)
                                    }
                                }
                            }
                        }
                    }

                    BusyIndicator {
                        parent: compareScroll
                        anchors.centerIn: parent
                        running: AppBridge.jobsLoading || AppBridge.jobDetailLoading
                        visible: running
                        palette.dark: Theme.accent
                    }
                    EmptyState {
                        parent: compareScroll
                        anchors.centerIn: parent
                        visible: !root.hasSelection && !AppBridge.jobsLoading && !AppBridge.jobDetailLoading
                        title: root.visibleRelationCount === 0
                            ? (AppBridge.duplicatePendingOnly ? "Aucune paire avec deux offres à classer" : "Aucun doublon en attente")
                            : "Aucun rapprochement sélectionné"
                        iconSource: Theme.icon(root.visibleRelationCount === 0 ? "check" : "files")
                    }
                }
            }
        }
    }
}
