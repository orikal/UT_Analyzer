"""Checkbox directory tree with lazy loading and tri-state parents."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

from ...utils.fs import iter_subdirs

_PATH_ROLE = Qt.ItemDataRole.UserRole
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1


class FolderTree(QTreeWidget):
    """Shows folders under the project root; each folder has a checkbox."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderLabel("Folders to suggest tests for")
        self.itemExpanded.connect(self._ensure_children)
        self._root_path: Path | None = None

    def set_root(self, root: Path) -> None:
        self.clear()
        self._root_path = root
        root_item = self._make_item(root, root.name or str(root))
        self.addTopLevelItem(root_item)
        root_item.setCheckState(0, Qt.CheckState.Checked)
        self._ensure_children(root_item)
        root_item.setExpanded(True)

    def checked_dirs(self) -> list[Path]:
        """Deepest fully-checked directories (a fully checked parent covers all
        of its children, including ones not yet lazily loaded)."""
        result: list[Path] = []
        for i in range(self.topLevelItemCount()):
            self._collect(self.topLevelItem(i), result)
        return result

    # ---- internals -------------------------------------------------------------

    def _collect(self, item: QTreeWidgetItem, result: list[Path]) -> None:
        state = item.checkState(0)
        path = Path(item.data(0, _PATH_ROLE))
        if state == Qt.CheckState.Checked:
            result.append(path)
        elif state == Qt.CheckState.PartiallyChecked:
            for i in range(item.childCount()):
                self._collect(item.child(i), result)

    def _make_item(self, path: Path, label: str | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label or path.name])
        item.setData(0, _PATH_ROLE, str(path))
        item.setData(0, _LOADED_ROLE, False)
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsAutoTristate
        )
        item.setCheckState(0, Qt.CheckState.Checked)
        if iter_subdirs(path):
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
        return item

    def _ensure_children(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _LOADED_ROLE):
            return
        item.setData(0, _LOADED_ROLE, True)
        path = Path(item.data(0, _PATH_ROLE))
        state = item.checkState(0)
        for sub in iter_subdirs(path):
            child = self._make_item(sub)
            item.addChild(child)
            if state != Qt.CheckState.PartiallyChecked:
                child.setCheckState(0, state)
