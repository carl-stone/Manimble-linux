"""Missing-package banners and explicitly consented TeX repair flows."""
from html import escape
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy, QWidget

from . import prefs
from .icons import template_icon
from .rpc import EngineError
from .style import Theme, is_dark, watch_appearance
from .tasks import spawn
from .widgets import add_copy_button


def _package_alert(parent, title, text, actions=()):
    box = QMessageBox(QMessageBox.Icon.Warning, title, title, QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    box.setInformativeText(text)
    if actions:
        buttons = {label: box.addButton(label, QMessageBox.ButtonRole.AcceptRole) for label in actions}
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
    else:
        box.addButton(QMessageBox.StandardButton.Ok)
    add_copy_button(box)
    box.exec()
    clicked = box.clickedButton()
    return next((label for label, button in buttons.items() if button == clicked), None) if actions else None


class _BannerLabel(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self._message = ""
        self.setFont(Theme.font(12))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_message(self, text):
        self._message = text
        self.setAccessibleName(text)
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        text = self._message
        width = max(1, self.contentsRect().width())
        metrics = self.fontMetrics()
        if metrics.horizontalAdvance(text) <= width:
            self.setText(text)
            self.setToolTip("")
            return
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout = QTextLayout(text, self.font())
        layout.setTextOption(option)
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(width)
        layout.endLayout()
        # QTextLine positions are UTF-16 units, unlike Python's string offsets.
        encoded = text.encode("utf-16-le")
        cut = 2 * line.textLength()
        first = encoded[:cut].decode("utf-16-le").rstrip()
        rest = encoded[cut:].decode("utf-16-le").lstrip()
        last = metrics.elidedText(rest, Qt.TextElideMode.ElideRight, width)
        self.setText(first + ("\n" + last if last else ""))
        self.setToolTip(f"<qt>{escape(text)}</qt>" if last != rest else "")


class _Spinner(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._spin)

    def showEvent(self, event):
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()

    def _spin(self):
        self._angle = (self._angle - 30) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(Theme.secondary_ink(), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(self.rect()).adjusted(1, 1, -1, -1), self._angle * 16, 270 * 16)


class LatexBanner(QFrame):
    muted_key = "latexBannerMutedDecks"
    not_now = set()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(round(max(38, Theme.logical_size(38))))
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 1)
        row.setSpacing(8)
        self.icon = QLabel(self)
        self.icon.setAccessibleName("LaTeX packages")
        self.icon.setFixedSize(16, 16)
        self.label = _BannerLabel(self)
        self.install = QPushButton("Install")
        self.later = QPushButton("Not Now")
        self.never = QPushButton("Don't Ask Again")
        self.spinner = _Spinner(self)
        row.addWidget(self.icon)
        row.addWidget(self.label, 1)
        row.addWidget(self.spinner)
        for button in (self.install, self.later, self.never):
            button.setFont(Theme.font(11))
            button.setAutoDefault(False)
            row.addWidget(button)
        self.install.setStyleSheet(
            f"QPushButton:enabled {{ background: {Theme.accent.name()}; color: #181818; "
            "border-radius: 5px; padding: 3px 10px; }"
            "QPushButton:pressed, QPushButton:focus { border: 1px solid palette(text); padding: 2px 9px; }")
        self._recolor()
        watch_appearance(self._recolor)
        self.busy(False)
        self.hide()

    def _recolor(self):
        self._symbol = template_icon("shippingbox", self, tint=Theme.warning_ink())
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        yellow = QColor("#ffd60a" if is_dark() else "#ffcc00")
        yellow.setAlphaF(.16)
        painter.fillRect(self.rect(), yellow)
        painter.fillRect(QRect(0, self.height() - 1, self.width(), 1), Theme.separator())
        # Paint via the DPR-aware template engine on the banner's actual output device.
        self._symbol.paint(painter, self.icon.geometry())

    def show(self, text=None):
        if text is not None:
            self.label.set_message(text)
        self.busy(False)
        super().show()

    def busy(self, on):
        for button in (self.install, self.later, self.never):
            button.setEnabled(not on)
        self.spinner.setVisible(on)

    @staticmethod
    def names(files):
        return ", ".join(Path(name).stem for name in files)


class LatexMixin:
    def init_latex(self):
        self.latex_banner = LatexBanner(self.window)
        self.latex_banner.install.clicked.connect(self.latex_banner_install)
        self.latex_banner.later.clicked.connect(self.latex_banner_not_now)
        self.latex_banner.never.clicked.connect(self.latex_banner_never)

    def offer_latex_packages(self, src, deck):
        missing = yield self.call("latex_missing", {"src": str(src)})
        if missing.get("error"):
            raise RuntimeError(missing["error"])
        files = missing.get("missing", [])
        if not files:
            return True
        list_names = LatexBanner.names(files)
        if missing.get("tlmgr"):
            message = (
                f"Missing: {list_names}.\n\n"
                "Manimble can install them with TeX Live's package manager (tlmgr), which downloads them from CTAN. "
                "This requires a writable user TeX installation. For distro-managed TeX, use your distribution's "
                "package manager; Manimble will not request administrator privileges.\n\n"
                "Without them, pictures that need these packages are imported as placeholders. "
                "You can install them later from the deck.")
            actions = ("Install and Import", "Import Without Them")
        else:
            message = (
                f"Missing: {list_names}.\n\nNo TeX package manager (tlmgr) was found, so they can't be installed from Manimble. "
                "Pictures that need them are imported as placeholders.")
            actions = ("Import Without Them",)
        choice = _package_alert(self.window, f"{Path(src).name} uses LaTeX packages that aren't installed", message, actions)
        if choice is None:
            return False
        if choice == "Install and Import":
            self.busy_pill.show("Installing LaTeX packages…", spinning=True, cancellable=False)
            try:
                result = yield self.call("latex_install", {"files": files})
            finally:
                self.update_busy_pill()
            if result.get("ok") is not True:
                _package_alert(
                    self.window, "The LaTeX packages were not installed",
                    (result.get("error") or "tlmgr did not answer") +
                    "\n\nThe presentation is imported anyway; pictures that need them are placeholders until they are installed.")
                LatexBanner.not_now.add(str((Path(deck) / "deck").resolve()))
            else:
                self.toast("Installed " + ", ".join(result.get("installed", [])))
        else:
            LatexBanner.not_now.add(str((Path(deck) / "deck").resolve()))
        return True

    def check_latex_packages(self):
        banner = self.latex_banner
        workspace = self.workspace
        if not workspace:
            banner.hide()
            return
        try:
            status = yield self.call("latex_status", {})
        except EngineError:
            if workspace is self.workspace:
                banner.hide()
            return
        if workspace is not self.workspace:
            return
        if status.get("error"):
            banner.hide()
            return
        missing = status.get("missing", [])
        if not missing:
            banner.hide()
            if status.get("placeholders", 0):
                yield from self.retypeset_latex_pictures()
            return
        key = str(workspace.original)
        if key in prefs.get_json(LatexBanner.muted_key, []) or key in LatexBanner.not_now:
            banner.hide()
            return
        source = f" (from {status['source']})" if status.get("source") else ""
        count = status.get("placeholders", 0)
        pics = (" One picture is a placeholder until they are installed." if count == 1 else
                f" {count} pictures are placeholders until they are installed." if count > 1 else "")
        banner.show(f"This deck{source} needs LaTeX packages that aren't installed: {LatexBanner.names(missing)}." + pics)

    def latex_banner_install(self):
        banner = self.latex_banner
        workspace, rpc = self.workspace, self.rpc
        if not workspace or not banner.install.isEnabled():
            return
        banner.busy(True)

        def install():
            try:
                status = yield self.call("latex_status", {})
                if workspace is not self.workspace or rpc is not self.rpc:
                    return
                if status.get("error"):
                    raise EngineError(status["error"])
                missing = status.get("missing", [])
                if not missing:
                    yield from self.check_latex_packages()
                    return
                names = LatexBanner.names(missing)
                choice = _package_alert(
                    self.window, "Install LaTeX packages?",
                    f"Download from CTAN using a writable user tlmgr installation: {names}. "
                    "Protected distro TeX requires distro packages; no privileged changes will be made.", ("Install",))
                if choice != "Install" or workspace is not self.workspace or rpc is not self.rpc:
                    return
                banner.label.set_message(f"Installing {names} with tlmgr…")
                result = yield self.call("latex_install", {"files": missing})
                if result.get("ok") is not True:
                    raise EngineError(result.get("error") or "tlmgr did not answer")
                self.toast("Installed " + ", ".join(result.get("installed", [])))
            except EngineError as exc:
                _package_alert(self.window, "The LaTeX packages were not installed", str(exc))
            finally:
                banner.busy(False)
            if workspace is self.workspace:
                yield from self.check_latex_packages()
        spawn(install())

    def latex_banner_not_now(self):
        if self.workspace:
            LatexBanner.not_now.add(str(self.workspace.original))
        self.latex_banner.hide()

    def latex_banner_never(self):
        if self.workspace:
            muted = prefs.get_json(LatexBanner.muted_key, [])
            key = str(self.workspace.original)
            if key not in muted:
                muted.append(key)
            prefs.set(LatexBanner.muted_key, muted)
        self.latex_banner.hide()
        self.toast("This deck won't ask about LaTeX packages again")

    def retypeset_latex_pictures(self):
        self.busy_pill.show("Typesetting LaTeX pictures…", spinning=True, cancellable=False)
        try:
            result = yield self.call("latex_retypeset", {})
        except EngineError as exc:
            self.toast(str(exc), error=True)
            return
        finally:
            self.update_busy_pill()
        if result.get("ok") is not True:
            self.toast(result.get("error") or "could not typeset the LaTeX pictures", error=True)
            return
        failed, updated = result.get("failed", 0), result.get("updated", 0)
        if failed:
            failure = f"{failed} LaTeX picture{'s' if failed != 1 else ''} could not be typeset"
        if updated:
            message = f"Typeset {updated} LaTeX picture{'s' if updated != 1 else ''}"
            self.toast(f"{message} — {failure}" if failed else message, error=bool(failed))
            yield from self.reload_state()
            if self.scene_name:
                yield from self.load_scene(self.scene_name, keep_stop=True)
        elif failed:
            self.toast(failure, error=True)
