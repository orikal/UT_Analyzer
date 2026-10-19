"""QTabWidget that draws a green notification dot on unread tabs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QTabBar, QTabWidget, QWidget

_NOTIFY_COLOR = QColor("#22c55e")
_DOT_DIAMETER = 8
_DOT_MARGIN = 5


class NotifyingTabBar(QTabBar):
    """Tab bar that overlays a green dot on tabs marked as unread."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notified_widgets: set[QWidget] = set()

    def mark_notified(self, widget: QWidget) -> None:
        if widget is None:
            return
        self._notified_widgets.add(widget)
        self.update()

    def clear_notification(self, widget: QWidget) -> None:
        if widget in self._notified_widgets:
            self._notified_widgets.discard(widget)
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._notified_widgets:
            return

        tab_widget = self.parent()
        if not isinstance(tab_widget, QTabWidget):
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(_NOTIFY_COLOR)
        painter.setPen(Qt.PenStyle.NoPen)

        for index in range(self.count()):
            widget = tab_widget.widget(index)
            if widget not in self._notified_widgets:
                continue
            rect = self.tabRect(index)
            x = rect.right() - _DOT_MARGIN - _DOT_DIAMETER
            y = rect.top() + _DOT_MARGIN
            painter.drawEllipse(x, y, _DOT_DIAMETER, _DOT_DIAMETER)

        painter.end()


class NotifyingTabWidget(QTabWidget):
    """Tab widget with green unread indicators; cleared when the tab is opened."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notify_bar = NotifyingTabBar(self)
        self.setTabBar(self._notify_bar)
        self.currentChanged.connect(self._on_current_changed)

    def mark_tab_notified(self, widget: QWidget) -> None:
        self._notify_bar.mark_notified(widget)

    def clear_tab_notification(self, widget: QWidget) -> None:
        self._notify_bar.clear_notification(widget)

    def _on_current_changed(self, index: int) -> None:
        if index < 0:
            return
        widget = self.widget(index)
        if widget is not None:
            self._notify_bar.clear_notification(widget)
