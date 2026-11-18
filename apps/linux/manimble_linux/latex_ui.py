"""Missing-package banners and explicitly consented TeX repair flows."""
from pathlib import Path

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton

from . import prefs
from .project_controller import ask
from .tasks import spawn


class LatexBanner(QFrame):
    muted_key = "latexBannerMutedDecks"
    not_now = set()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.install = QPushButton("Install")
        self.later = QPushButton("Not Now")
        self.never = QPushButton("Don't Ask Again")
        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setMaximumWidth(60)
        row.addWidget(self.label, 1)
        for widget in (self.spinner, self.install, self.later, self.never):
            row.addWidget(widget)
        self.hide()

    def show(self, text=None):
        if text is not None:
            self.label.setText(text)
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
        message = ("Missing: " + LatexBanner.names(files) + ".\n\nA writable user TeX installation can download packages from CTAN with tlmgr. "
                   "Protected/distro TeX must be updated using your distro packages; Manimble will not request administrator privileges. "
                   "Without packages, affected pictures are imported as placeholders.")
        choices = ["Install and Import", "Import Without Them", "Cancel"] if missing.get("tlmgr") else ["Import Without Them", "Cancel"]
        choice = ask(self.window, f"{Path(src).name} uses missing LaTeX packages", message, choices)
        if choice == "Cancel":
            return False
        if choice == "Install and Import":
            result = yield self.call("latex_install", {"files": files})
            if result.get("ok") is not True:
                self.alert("Packages were not installed", result.get("error", "tlmgr did not answer") + "\n\nImport continues with placeholders.")
                LatexBanner.not_now.add(str((Path(deck) / "deck").resolve()))
            else:
                self.toast("Installed " + ", ".join(result.get("installed", [])))
        else:
            LatexBanner.not_now.add(str((Path(deck) / "deck").resolve()))
        return True

    def check_latex_packages(self):
        banner = self.latex_banner
        if not self.workspace:
            banner.hide()
            return
        status = yield self.call("latex_status", {})
        if status.get("error"):
            banner.show(status["error"])
            return
        missing = status.get("missing", [])
        if not missing:
            banner.hide()
            if status.get("placeholders", 0):
                yield from self.retypeset_latex_pictures()
            return
        key = str(self.workspace.original)
        if key in prefs.get_json(LatexBanner.muted_key, []) or key in LatexBanner.not_now:
            banner.hide()
            return
        source = f" (from {status['source']})" if status.get("source") else ""
        count = status.get("placeholders", 0)
        banner.show(f"This deck{source} needs missing LaTeX packages: {LatexBanner.names(missing)}." +
                    (f" {count} picture(s) are placeholders until installed." if count else ""))

    def latex_banner_install(self):
        def install():
            try:
                status = yield self.call("latex_status", {})
                missing = status.get("missing", [])
                if not missing:
                    yield from self.check_latex_packages()
                    return
                if ask(self.window, "Install LaTeX packages?", "Download from CTAN using a writable user tlmgr installation: " + LatexBanner.names(missing) + ". Protected distro TeX requires distro packages; no privileged changes will be made.", ["Install", "Cancel"]) != "Install":
                    return
                self.latex_banner.busy(True)
                result = yield self.call("latex_install", {"files": missing})
                if result.get("ok") is not True:
                    raise RuntimeError(result.get("error", "tlmgr did not answer"))
                self.toast("Installed " + ", ".join(result.get("installed", [])))
                yield from self.check_latex_packages()
            except Exception as exc:
                self.alert("The LaTeX packages were not installed", str(exc))
            finally:
                self.latex_banner.busy(False)
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

    def retypeset_latex_pictures(self):
        result = yield self.call("latex_retypeset", {})
        if result.get("ok") is not True:
            self.toast(result.get("error", "Could not typeset LaTeX pictures"), error=True)
            return
        if result.get("failed"):
            self.toast(f"{result['failed']} LaTeX pictures could not be typeset", error=True)
        if result.get("updated", 0):
            self.toast(f"Typeset {result['updated']} LaTeX pictures")
            yield from self.reload_state()
            if self.scene_name:
                yield from self.load_scene(self.scene_name, keep_stop=True)
