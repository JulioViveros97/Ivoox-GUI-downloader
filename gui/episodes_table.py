from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QAbstractItemView

from kernel.episode_model import Episode


class EpisodesTable(QTableWidget):
    COL_CHECK = 0
    COL_INDEX = 1
    COL_TITLE = 2
    COL_ID = 3
    COL_DATE = 4
    COL_DURATION = 5
    COL_PROPOSED = 6
    COL_STATUS = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.episodes: List[Episode] = []

        self.setColumnCount(8)
        self.setHorizontalHeaderLabels([
            "✓", "#", "Título", "ID", "Fecha", "Duración", "Nombre propuesto", "Estado"
        ])
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setDefaultSectionSize(130)

    def set_episodes(self, episodes: List[Episode]):
        self.episodes = episodes
        self._refresh_all_rows()

    def _refresh_all_rows(self):
        self.setRowCount(len(self.episodes))
        for row, ep in enumerate(self.episodes):
            self._populate_row(row, ep)

    def _populate_row(self, row: int, ep: Episode):
        check_item = QTableWidgetItem()
        check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        check_item.setCheckState(Qt.Checked if ep.selected else Qt.Unchecked)
        self.setItem(row, self.COL_CHECK, check_item)

        idx_item = QTableWidgetItem(str(row + 1))
        idx_item.setTextAlignment(Qt.AlignCenter)
        idx_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.setItem(row, self.COL_INDEX, idx_item)

        title_item = QTableWidgetItem(ep.title or "")
        title_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        title_item.setToolTip(ep.title or "")
        self.setItem(row, self.COL_TITLE, title_item)

        id_item = QTableWidgetItem(ep.id)
        id_item.setTextAlignment(Qt.AlignCenter)
        id_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.setItem(row, self.COL_ID, id_item)

        date_item = QTableWidgetItem(ep.date or "")
        date_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.setItem(row, self.COL_DATE, date_item)

        dur_item = QTableWidgetItem(ep.duration or "")
        dur_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.setItem(row, self.COL_DURATION, dur_item)

        prop_item = QTableWidgetItem(ep.proposed_filename or "")
        prop_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
        prop_item.setToolTip(ep.proposed_filename or "")
        self.setItem(row, self.COL_PROPOSED, prop_item)

        status_item = QTableWidgetItem(ep.download_status)
        status_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.setItem(row, self.COL_STATUS, status_item)

    def sync_from_table(self):
        for row, ep in enumerate(self.episodes):
            check_item = self.item(row, self.COL_CHECK)
            if check_item is not None:
                ep.selected = (check_item.checkState() == Qt.Checked)
            prop_item = self.item(row, self.COL_PROPOSED)
            if prop_item is not None:
                text = prop_item.text().strip()
                ep.proposed_filename = text or ""
                prop_item.setToolTip(ep.proposed_filename or "")

    def refresh_proposed_names(self):
        for row, ep in enumerate(self.episodes):
            idx_item = self.item(row, self.COL_INDEX)
            if idx_item:
                idx_item.setText(str(row + 1))
            prop_item = self.item(row, self.COL_PROPOSED)
            if prop_item:
                prop_item.setText(ep.proposed_filename or "")
                prop_item.setToolTip(ep.proposed_filename or "")
            title_item = self.item(row, self.COL_TITLE)
            if title_item:
                title_item.setText(ep.title or "")
                title_item.setToolTip(ep.title or "")

    def refresh_status(self):
        for row, ep in enumerate(self.episodes):
            item = self.item(row, self.COL_STATUS)
            if item:
                item.setText(ep.download_status)

    def select_all(self):
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item:
                item.setCheckState(Qt.Checked)
        self.sync_from_table()

    def deselect_all(self):
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item:
                item.setCheckState(Qt.Unchecked)
        self.sync_from_table()

    def remove_selected_rows(self):
        rows = sorted({idx.row() for idx in self.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return

        self.sync_from_table()

        for row in rows:
            if 0 <= row < len(self.episodes):
                self.episodes.pop(row)

        self._refresh_all_rows()
        self.clearSelection()

    def move_selected_up(self):
        rows = sorted({idx.row() for idx in self.selectionModel().selectedRows()})
        if not rows or rows[0] <= 0:
            return

        self.sync_from_table()

        selected_set = set(rows)
        for row in rows:
            if row - 1 not in selected_set:
                self.episodes[row - 1], self.episodes[row] = self.episodes[row], self.episodes[row - 1]

        new_rows = [row - 1 for row in rows]
        self._refresh_all_rows()
        self.clearSelection()
        for row in new_rows:
            self.selectRow(row)

    def move_selected_down(self):
        rows = sorted({idx.row() for idx in self.selectionModel().selectedRows()}, reverse=True)
        if not rows or rows[0] >= self.rowCount() - 1:
            return

        self.sync_from_table()

        selected_set = set(rows)
        for row in rows:
            if row + 1 not in selected_set:
                self.episodes[row + 1], self.episodes[row] = self.episodes[row], self.episodes[row + 1]

        new_rows = [row + 1 for row in rows]
        self._refresh_all_rows()
        self.clearSelection()
        for row in new_rows:
            self.selectRow(row)

    def current_episode(self):
        # Priorizar currentRow(): con ExtendedSelection puede haber varias filas
        # seleccionadas y selectedRows()[0] no siempre representa la fila activa
        # que el usuario acaba de clicar. Esto es importante para el panel de
        # miniaturas.
        row = self.currentRow()
        if 0 <= row < len(self.episodes):
            return self.episodes[row]

        idxs = self.selectionModel().selectedRows()
        if not idxs:
            return None
        row = idxs[0].row()
        if 0 <= row < len(self.episodes):
            return self.episodes[row]
        return None
