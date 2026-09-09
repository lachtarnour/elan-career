import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "companiesPage"
    property real pendingScrollY: -1
    readonly property var priorityLabels: ["Non définie", "À candidater maintenant", "Très bonne cible", "Bonne cible", "Secondaire", "Faible priorité"]
    readonly property var filteredCompanies: {
        var query = searchField.text.trim().toLocaleLowerCase()
        return AppBridge.companies.filter(function(company) {
            if (checkedFilter.currentIndex === 1 && company.checked) return false
            if (checkedFilter.currentIndex === 2 && !company.checked) return false
            if (priorityFilter.currentIndex > 0 && company.priority !== priorityFilter.currentIndex) return false
            return !query || [company.name, company.category, company.company_type].join(" ").toLocaleLowerCase().indexOf(query) >= 0
        })
    }
    readonly property int checkedCount: AppBridge.companies.filter(function(company) { return company.checked }).length

    component CellText: Text {
        color: Theme.inkSoft
        font.pixelSize: 12
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        elide: Text.ElideRight
        maximumLineCount: 3
        ToolTip.visible: cellHover.hovered && text.length > 0
        ToolTip.delay: 500
        ToolTip.text: text
        HoverHandler { id: cellHover }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 20

        PageHeader {
            Layout.fillWidth: true
            title: "Entreprises"
            AppButton {
                objectName: "companyAddButton"
                text: "Ajouter une entreprise"
                iconSource: Theme.icon("plus")
                kind: "primary"
                enabled: !AppBridge.companiesSaving
                onClicked: addDialog.open()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            AppField {
                id: searchField
                objectName: "companySearch"
                Layout.fillWidth: true
                placeholderText: "Rechercher une entreprise, une catégorie…"
                Accessible.name: "Rechercher une entreprise"
                iconSource: Theme.icon("search")
            }
            AppSelect {
                id: priorityFilter
                objectName: "companyPriorityFilter"
                Layout.preferredWidth: 185
                Accessible.name: "Filtrer par priorité"
                model: ["Toutes les priorités", "Priorité 1", "Priorité 2", "Priorité 3", "Priorité 4", "Priorité 5"]
            }
            AppSelect {
                id: checkedFilter
                objectName: "companyCheckedFilter"
                Layout.preferredWidth: 175
                Accessible.name: "Filtrer les entreprises vérifiées"
                model: ["Toutes", "À vérifier", "Déjà vérifiées"]
            }
            Text {
                text: root.checkedCount + " / " + AppBridge.companies.length + " vérifiées"
                color: Theme.inkMuted
                font.pixelSize: 12
            }
        }

        Rectangle {
            id: table
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Theme.radiusLarge
            color: Theme.surface
            border.color: Theme.line
            readonly property real flexibleWidth: width - 32 - Theme.scrollGutter - 112 - 72
            readonly property var columnWidths: [68, 44, flexibleWidth * 0.18, flexibleWidth * 0.19, flexibleWidth * 0.21, flexibleWidth * 0.16, flexibleWidth * 0.26]

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10
                Row {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 32
                    spacing: 12
                    Repeater {
                        model: ["CHECKED", "N°", "ENTREPRISE", "PRIORITÉ", "CATÉGORIE / TYPE", "ANGLAIS / SÉLECTIVITÉ", "RAISON DU CLASSEMENT"]
                        Text {
                            required property int index
                            required property string modelData
                            width: table.columnWidths[index]
                            height: 32
                            text: modelData
                            color: Theme.inkMuted
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                            wrapMode: Text.WordWrap
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
                ListView {
                    id: companyList
                    objectName: "companyList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    model: root.filteredCompanies
                    clip: true
                    spacing: 4
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { }
                    delegate: Rectangle {
                        id: companyRow
                        required property var modelData
                        width: companyList.width - Theme.scrollGutter
                        height: Math.max(76, cells.implicitHeight + 28)
                        radius: Theme.radiusSmall
                        color: rowHover.hovered ? Theme.surfaceHover : "transparent"
                        HoverHandler { id: rowHover }
                        Row {
                            id: cells
                            width: parent.width
                            y: 14
                            spacing: 12
                            Item {
                                width: table.columnWidths[0]
                                height: 28
                                SelectionBox {
                                    objectName: "companyCheck-" + companyRow.modelData.id
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    checked: companyRow.modelData.checked
                                    enabled: !AppBridge.companiesSaving && !AppBridge.companiesLoading
                                    Accessible.name: "Entreprise vérifiée : " + companyRow.modelData.name
                                    onToggled: function(checked) {
                                        root.pendingScrollY = companyList.contentY
                                        AppBridge.setCompanyChecked(companyRow.modelData.id, checked)
                                    }
                                }
                            }
                            CellText { width: table.columnWidths[1]; text: companyRow.modelData.sort_order; color: Theme.inkFaint }
                            RowLayout {
                                width: table.columnWidths[2]
                                spacing: 4
                                CellText {
                                    Layout.fillWidth: true
                                    Layout.alignment: Qt.AlignTop
                                    text: companyRow.modelData.name
                                    color: Theme.ink
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                }
                                AppButton {
                                    id: copyButton
                                    objectName: "companyCopy-" + companyRow.modelData.id
                                    Layout.preferredWidth: 28
                                    Layout.preferredHeight: 28
                                    Layout.alignment: Qt.AlignTop
                                    iconSource: Theme.icon(copyFeedback.running ? "check" : "copy")
                                    iconSize: 14
                                    kind: copyFeedback.running ? "success" : "secondary"
                                    quiet: true
                                    Accessible.name: "Copier le nom de " + companyRow.modelData.name
                                    ToolTip.visible: hovered || visualFocus
                                    ToolTip.delay: 400
                                    ToolTip.text: copyFeedback.running ? "Copié" : "Copier le nom"
                                    onClicked: {
                                        if (AppBridge.copyText(companyRow.modelData.name)) copyFeedback.restart()
                                    }
                                    Timer { id: copyFeedback; interval: 1500 }
                                }
                            }
                            Column {
                                width: table.columnWidths[3]
                                spacing: 6
                                CellText { width: parent.width; text: companyRow.modelData.priority ? "Priorité " + companyRow.modelData.priority : "Non définie"; color: companyRow.modelData.priority === 1 ? Theme.accentBright : Theme.ink; font.weight: Font.DemiBold }
                                CellText { width: parent.width; text: companyRow.modelData.priority ? companyRow.modelData.priority_label : ""; color: Theme.inkMuted }
                            }
                            Column {
                                width: table.columnWidths[4]
                                spacing: 6
                                CellText { width: parent.width; text: companyRow.modelData.category || "—" }
                                CellText { width: parent.width; text: companyRow.modelData.company_type; color: Theme.inkFaint; maximumLineCount: 2 }
                            }
                            Column {
                                width: table.columnWidths[5]
                                spacing: 6
                                CellText { width: parent.width; text: companyRow.modelData.english_level || "—" }
                                CellText { width: parent.width; text: companyRow.modelData.selectivity; color: Theme.inkFaint; maximumLineCount: 2 }
                            }
                            CellText { width: table.columnWidths[6]; text: companyRow.modelData.ranking_reason || "—"; color: Theme.inkMuted; maximumLineCount: 5 }
                        }
                    }
                    Text {
                        anchors.centerIn: parent
                        visible: companyList.count === 0
                        text: AppBridge.companiesLoading ? "Chargement…" : "Aucune entreprise"
                        color: Theme.inkMuted
                        font.pixelSize: 14
                    }
                }
            }
        }

        Text {
            text: root.filteredCompanies.length + (root.filteredCompanies.length === 1 ? " entreprise" : " entreprises")
            color: Theme.inkFaint
            font.pixelSize: 12
        }
    }

    Dialog {
        id: addDialog
        objectName: "companyAddDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(740, parent.width - 48)
        implicitHeight: form.implicitHeight + padding * 2
        padding: 24
        modal: true
        focus: true
        closePolicy: AppBridge.companiesSaving ? Popup.NoAutoClose : Popup.CloseOnEscape
        Accessible.name: "Ajouter une entreprise"
        property string errorMessage: ""
        background: Rectangle { radius: Theme.radiusLarge; color: Theme.surfaceRaised; border.color: Theme.lineStrong }
        Overlay.modal: Rectangle { color: Theme.scrim }
        onOpened: {
            errorMessage = ""
            companyName.forceActiveFocus()
        }
        onClosed: {
            companyName.clear()
            companyPriority.currentIndex = 0
            companyCategory.clear()
            companyType.clear()
            companyEnglish.clear()
            companySelectivity.clear()
            companyReason.text = ""
            errorMessage = ""
        }
        contentItem: ColumnLayout {
            id: form
            spacing: 18
            Text { text: "Ajouter une entreprise"; color: Theme.ink; font.pixelSize: 21; font.weight: Font.DemiBold }
            GridLayout {
                Layout.fillWidth: true
                columns: 2
                uniformCellWidths: true
                columnSpacing: 16
                rowSpacing: 14
                enabled: !AppBridge.companiesSaving
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    spacing: 7
                    FormLabel { text: "ENTREPRISE *" }
                    AppField { id: companyName; objectName: "companyNameInput"; Layout.fillWidth: true; placeholderText: "Nom de l’entreprise"; maximumLength: 255 }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 7
                    FormLabel { text: "PRIORITÉ" }
                    AppSelect {
                        id: companyPriority
                        objectName: "companyPriorityInput"
                        Layout.fillWidth: true
                        model: root.priorityLabels.map(function(label, index) { return index ? index + " · " + label : label })
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 7
                    FormLabel { text: "CATÉGORIE" }
                    AppField { id: companyCategory; Layout.fillWidth: true; placeholderText: "Conseil, santé, industrie…"; maximumLength: 255 }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 7
                    FormLabel { text: "TYPE" }
                    AppField { id: companyType; Layout.fillWidth: true; placeholderText: "Startup, grand groupe, ESN…"; maximumLength: 255 }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 7
                    FormLabel { text: "ANGLAIS ESTIMÉ" }
                    AppField { id: companyEnglish; Layout.fillWidth: true; placeholderText: "Faible, variable, fréquent…"; maximumLength: 255 }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    spacing: 7
                    FormLabel { text: "SÉLECTIVITÉ ESTIMÉE" }
                    AppField { id: companySelectivity; Layout.fillWidth: true; placeholderText: "Accessible, modérée, élevée…"; maximumLength: 255 }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    spacing: 7
                    FormLabel { text: "RAISON DU CLASSEMENT" }
                    AppTextArea { id: companyReason; Layout.fillWidth: true; Layout.preferredHeight: 86; placeholderText: "Pourquoi cibler cette entreprise…" }
                }
            }
            Text {
                objectName: "companyFormError"
                Layout.fillWidth: true
                visible: text.length > 0
                text: addDialog.errorMessage
                textFormat: Text.PlainText
                color: Theme.danger
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Item { Layout.fillWidth: true }
                AppButton { text: "Annuler"; enabled: !AppBridge.companiesSaving; onClicked: addDialog.close() }
                AppButton {
                    objectName: "companySaveButton"
                    text: AppBridge.companiesSaving ? "Enregistrement…" : "Ajouter"
                    kind: "primary"
                    enabled: companyName.text.trim().length > 0 && !AppBridge.companiesSaving
                    onClicked: {
                        addDialog.errorMessage = ""
                        AppBridge.addCompany({
                            name: companyName.text,
                            priority: companyPriority.currentIndex,
                            category: companyCategory.text,
                            company_type: companyType.text,
                            english_level: companyEnglish.text,
                            selectivity: companySelectivity.text,
                            ranking_reason: companyReason.text
                        })
                    }
                }
            }
        }
    }

    Connections {
        target: AppBridge
        function onCompaniesChanged() {
            if (root.pendingScrollY < 0) return
            var previousY = root.pendingScrollY
            root.pendingScrollY = -1
            Qt.callLater(function() {
                companyList.contentY = Math.max(0, Math.min(previousY, companyList.contentHeight - companyList.height))
            })
        }
        function onCompanyAdded() {
            var addedName = companyName.text.trim().replace(/\s+/g, " ")
            addDialog.close()
            searchField.clear()
            checkedFilter.currentIndex = 0
            priorityFilter.currentIndex = 0
            Qt.callLater(function() {
                for (var i = 0; i < root.filteredCompanies.length; i++) {
                    if (root.filteredCompanies[i].name === addedName) {
                        companyList.positionViewAtIndex(i, ListView.Contain)
                        break
                    }
                }
            })
        }
        function onCompanySaveFailed(message) {
            root.pendingScrollY = -1
            if (addDialog.opened) addDialog.errorMessage = message
        }
    }
}
