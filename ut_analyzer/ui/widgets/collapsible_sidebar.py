"""Left-side panel that collapses to a floating vertical 'Project' tab."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_DOCK_TAB_WIDTH = 22
_DOCK_TAB_HEIGHT = 80
_DRAG_THRESHOLD = 5

_CLOSE_BTN_STYLE = """
QPushButton {
    border: 1px solid #94a3b8;
    background-color: #f1f5f9;
    color: #1e293b;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 6px;
}
QPushButton:hover {
    color: #0f172a;
    background-color: #e2e8f0;
    border-color: #64748b;
}
QPushButton:pressed {
    background-color: #cbd5e1;
}
"""

_HEADER_STYLE = """
QFrame {
    background-color: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
}
"""


class VerticalDockTab(QPushButton):
    """Narrow left-edge tab with title rotated 90 degrees; draggable vertically."""

    moved = Signal(int)
    activate = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self.setFixedSize(_DOCK_TAB_WIDTH, _DOCK_TAB_HEIGHT)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip(f"Drag vertically or click to open {title}")
        self.setFlat(True)
        self._press_global: QPoint | None = None
        self._start_y = 0
        self._dragging = False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._start_y = self.y()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_global is None or not (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            return
        delta_y = event.globalPosition().toPoint().y() - self._press_global.y()
        if not self._dragging and abs(delta_y) < _DRAG_THRESHOLD:
            return
        self._dragging = True
        self.move(0, self._clamp_y(self._start_y + delta_y))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._press_global is not None:
            if self._dragging:
                self.moved.emit(self.y())
            else:
                self.activate.emit()
            self._press_global = None
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _clamp_y(self, y: int) -> int:
        host = self.parentWidget()
        if host is None:
            return max(y, 0)
        max_y = max(host.height() - self.height(), 0)
        return max(0, min(y, max_y))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QColor("#cbd5e1"))
        painter.setBrush(QColor("#f8fafc"))
        painter.drawRoundedRect(rect, 5, 5)

        painter.setPen(QColor("#1e293b"))
        font = self.font()
        font.setBold(True)
        font.setPointSize(10)
        painter.setFont(font)
        painter.translate(self.width(), 0)
        painter.rotate(90)
        painter.drawText(
            0,
            0,
            self.height(),
            self.width(),
            int(Qt.AlignmentFlag.AlignCenter),
            self._title,
        )

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.update()


class CollapsibleSidebar(QWidget):
    """Sidebar with 'Close tab'; collapsed state uses a floating edge tab."""

    expanded_changed = Signal(bool)

    COLLAPSED_WIDTH = 0
    DEFAULT_EXPANDED_WIDTH = 420

    def __init__(
        self,
        content: QWidget,
        *,
        dock_title: str = "Project",
        host: QWidget | None = None,
        expanded: bool = True,
        expanded_width: int = DEFAULT_EXPANDED_WIDTH,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._expanded = expanded
        self._saved_width = expanded_width
        self._host = host
        self._dock_y: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._expanded_wrap = QFrame()
        self._expanded_wrap.setFrameShape(QFrame.Shape.NoFrame)
        expanded_layout = QVBoxLayout(self._expanded_wrap)
        expanded_layout.setContentsMargins(0, 0, 0, 0)
        expanded_layout.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(_HEADER_STYLE)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 6)
        header_layout.addStretch()
        self._close_btn = QPushButton("Close tab")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("Hide sidebar")
        self._close_btn.setStyleSheet(_CLOSE_BTN_STYLE)
        self._close_btn.clicked.connect(lambda: self.set_expanded(False))
        header_layout.addWidget(self._close_btn)
        expanded_layout.addWidget(header)
        expanded_layout.addWidget(content, 1)
        layout.addWidget(self._expanded_wrap)

        self._dock_tab = VerticalDockTab(dock_title)
        self._dock_tab.moved.connect(self._on_dock_moved)
        self._dock_tab.activate.connect(lambda: self.set_expanded(True))
        self._dock_tab.hide()

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        if self._host is not None:
            self._host.installEventFilter(self)
        self.set_expanded(expanded)

    def set_host(self, host: QWidget) -> None:
        if self._host is not None:
            self._host.removeEventFilter(self)
        self._host = host
        if self._host is not None:
            self._host.installEventFilter(self)
            if not self._expanded:
                self._position_dock_tab()

    def saved_width(self) -> int:
        return self._saved_width

    def set_saved_width(self, width: int) -> None:
        self._saved_width = max(width, 280)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        if not expanded and self._expanded:
            self._saved_width = max(self.width(), 280)
        self._expanded = expanded
        if expanded:
            self._dock_tab.hide()
            self.show()
            self.setMinimumWidth(280)
            self.setMaximumWidth(16777215)
        else:
            self.hide()
            self._position_dock_tab()
            self._dock_tab.show()
            self._dock_tab.raise_()
        self.expanded_changed.emit(expanded)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            watched is self._host
            and event.type() == QEvent.Type.Resize
            and not self._expanded
        ):
            self._position_dock_tab()
        return super().eventFilter(watched, event)

    def _on_dock_moved(self, y: int) -> None:
        self._dock_y = y

    def _clamp_dock_y(self, y: int, host_height: int) -> int:
        max_y = max(host_height - _DOCK_TAB_HEIGHT, 0)
        return max(0, min(y, max_y))

    def _position_dock_tab(self) -> None:
        host = self._host or self.parentWidget()
        if host is None:
            return
        if self._dock_tab.parent() is not host:
            self._dock_tab.setParent(host)
        if self._dock_y is None:
            y = self._clamp_dock_y((host.height() - _DOCK_TAB_HEIGHT) // 2, host.height())
        else:
            y = self._clamp_dock_y(self._dock_y, host.height())
        self._dock_y = y
        self._dock_tab.move(0, y)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)
