from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QToolButton, QScrollArea, QVBoxLayout, QSizePolicy


class CollapsibleBox(QWidget):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.toggle_button = QToolButton(text=title, checkable=True, checked=True)
        self.toggle_button.setStyleSheet("QToolButton { border: none; }")
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.DownArrow)
        self.toggle_button.clicked.connect(self._on_toggled)

        self.content_area = QScrollArea()
        self.content_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.content_area.setFrameShape(QScrollArea.NoFrame)
        self.content_area.setWidgetResizable(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.toggle_button)
        lay.addWidget(self.content_area)

        self._content_layout = QVBoxLayout()
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(4)

        self._content_widget = QWidget()
        self._content_widget.setLayout(self._content_layout)
        self.content_area.setWidget(self._content_widget)

    def setContentLayout(self, layout):
        """Reemplaza el layout de contenido de forma segura.

        En PySide6, QScrollArea.setWidget(new_widget) elimina automáticamente
        el widget anterior. Por eso NO se debe llamar old_widget.deleteLater()
        después de setWidget(), porque el objeto C++ puede haber sido destruido
        ya por Qt/Shiboken.
        """
        new_widget = QWidget()
        new_widget.setLayout(layout)
        self.content_area.setWidget(new_widget)
        self._content_widget = new_widget
        self._content_layout = layout

    def contentLayout(self):
        return self._content_layout

    def _on_toggled(self, checked: bool):
        self.toggle_button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.content_area.setVisible(checked)
