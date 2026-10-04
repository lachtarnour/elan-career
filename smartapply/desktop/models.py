"""Stable offer rows for Qt Quick without resetting delegates on refresh."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt


class OfferListModel(QAbstractListModel):
    row_role = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def roleNames(self):  # noqa: N802
        return {self.row_role: b"modelData"}

    def rowCount(self, parent=None):  # noqa: N802
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == self.row_role and 0 <= index.row() < len(self._rows):
            return self._rows[index.row()]
        return None

    def replace(self, rows: list[dict]) -> None:
        """Diff identities, then update data; keep existing delegates when possible."""
        wanted = {row["id"] for row in rows}
        for position in range(len(self._rows) - 1, -1, -1):
            if self._rows[position]["id"] not in wanted:
                self.beginRemoveRows(QModelIndex(), position, position)
                self._rows.pop(position)
                self.endRemoveRows()
        for position, row in enumerate(rows):
            old = next(
                (i for i in range(position, len(self._rows)) if self._rows[i]["id"] == row["id"]),
                None,
            )
            if old is None:
                self.beginInsertRows(QModelIndex(), position, position)
                self._rows.insert(position, row)
                self.endInsertRows()
            else:
                if old != position:
                    self.beginMoveRows(QModelIndex(), old, old, QModelIndex(), position)
                    self._rows.insert(position, self._rows.pop(old))
                    self.endMoveRows()
                if self._rows[position] != row:
                    self._rows[position] = row
                    index = self.index(position)
                    self.dataChanged.emit(index, index, [self.row_role])
