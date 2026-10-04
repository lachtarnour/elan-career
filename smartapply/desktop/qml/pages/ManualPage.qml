import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    signal navigateRequested(string route)
    readonly property bool formValid: titleField.text.trim().length > 0
        && companyField.text.trim().length > 0 && descriptionField.text.trim().length >= 40
    function resetForm() {
        titleField.clear()
        companyField.clear()
        locationField.clear()
        urlField.clear()
        descriptionField.text = ""
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
            width: Math.min(parent.width - Theme.scrollGutter, 1320)
            x: (parent.width - Theme.scrollGutter - width) / 2
            spacing: Theme.pageGap
            PageHeader {
                Layout.fillWidth: true
                title: "Ajouter une offre"
            }
            Surface {
                Layout.fillWidth: true
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    uniformCellWidths: true
                    columnSpacing: 16
                    rowSpacing: 20
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        spacing: 7
                        FormLabel { text: "INTITULÉ DU POSTE *" }
                        AppField { id: titleField; Accessible.name: "Intitulé du poste"; objectName: "manualTitle"; Layout.fillWidth: true; placeholderText: "Machine Learning Engineer" }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        spacing: 7
                        FormLabel { text: "ENTREPRISE *" }
                        AppField { id: companyField; Accessible.name: "Entreprise"; objectName: "manualCompany"; Layout.fillWidth: true; placeholderText: "Nom de l’entreprise" }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 7
                        FormLabel { text: "LOCALISATION" }
                        AppField { id: locationField; Accessible.name: "Localisation"; Layout.fillWidth: true; iconSource: Theme.icon("map-pin"); placeholderText: "Paris, France" }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 7
                        FormLabel { text: "LIEN DE L’OFFRE" }
                        AppField { id: urlField; Accessible.name: "Lien de l’offre"; Layout.fillWidth: true; placeholderText: "https://entreprise.com/jobs/…"; iconSource: Theme.icon("arrow-up-right") }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    FormLabel { text: "DESCRIPTION COMPLÈTE *" }
                    AppTextArea {
                        id: descriptionField
                        objectName: "manualDescription"
                        Accessible.name: "Description complète de l’offre"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 260
                        placeholderText: "Missions, compétences, conditions…"
                    }
                    Text {
                        objectName: "manualDescriptionValidation"
                        Layout.fillWidth: true
                        visible: descriptionField.text.trim().length > 0 && descriptionField.text.trim().length < 40
                        text: "40 caractères minimum"
                        color: Theme.warning
                        font.pixelSize: 12
                    }
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Item { Layout.fillWidth: true }
                    AppButton { text: "Réinitialiser"; quiet: true; onClicked: root.resetForm() }
                    AppButton {
                        objectName: "manualCreate"
                        text: "Créer la candidature"
                        iconSource: Theme.icon("sparkle")
                        kind: "primary"
                        enabled: !AppBridge.shutdownRequested && root.formValid
                        onClicked: AppBridge.createManualApplication(titleField.text, companyField.text, locationField.text, descriptionField.text, urlField.text)
                    }
                }
            }
        }
    }
}
