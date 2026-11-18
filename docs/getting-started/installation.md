# Installation

Manimble runs on macOS 13 (Ventura) or later, on both Apple Silicon and Intel Macs. A Linux front end supports
Ubuntu 24.04 and Arch/Omarchy on x86_64. The sections below describe macOS unless marked Linux. Windows is planned.

## Install the app

1. Download [Manimble-macOS.dmg](https://github.com/pachterlab/Manimble/releases/latest/download/Manimble-macOS.dmg)
   from the latest release.
2. Open the disk image and drag **Manimble** to the **Applications** folder.
3. Open Manimble from Applications.

The app is signed and notarized by Apple, so it opens like any other app.

## First launch: Python and LaTeX

Manimble draws slides with Manim, which needs Python, and typesets formulas with LaTeX. On first launch Manimble
looks for both and offers to install whatever is missing. You don't need Homebrew or the Terminal.

| Component | What Manimble installs | Size | Where |
|---|---|---|---|
| Python with Manim | A self-contained Python runtime with Manim, Manim Slides and the other packages Manimble uses | about 150 MB download | `~/Library/Application Support/Manimble/python` |
| LaTeX | [TinyTeX](https://yihui.org/tinytex/) and the LaTeX packages Manim uses | about 80 MB download | `~/Library/TinyTeX` |

Both go into your user Library, so no administrator password is needed. Click **Install** in the setup window to
install them, or **Not Now** to skip and install later.

If your Mac already has what Manimble needs, it uses that instead:

- **Python:** any Python that can import Manim. Click **Use Another Python…** in the setup window, or choose
  **Manimble ▸ Choose Python…** later, to pick one.
- **LaTeX:** an existing TeX installation such as MacTeX. If it lacks some of the packages Manim uses, Manimble adds
  them. That step asks for your password, because a system-wide TeX lives in a system folder.

To open the setup window again, choose **Manimble ▸ Install Dependencies…**.

!!! tip "How Manimble finds Python"
    On launch Manimble checks, in order: the `MANIMBLE_PYTHON` environment variable, the Python you chose with
    **Choose Python…**, its own installed runtime, a `.venv` in a source checkout, and the Python on your login
    shell's `PATH`. It uses the first one that can import Manim and Pillow.

## Update Manimble

Download the latest DMG from the [Releases page](https://github.com/pachterlab/Manimble/releases) and drag Manimble
to Applications, replacing the old copy. Your Python and LaTeX installations are kept.

After some updates the first opening of a deck re-renders every slide, because the way slides are rendered changed.
This happens once per deck.

## Optional extras

- **Keynote** is needed only to import and export Keynote files.
- **An AI coding agent** such as [Claude Code](https://claude.com/claude-code), Codex or Gemini CLI, if you want to
  build slides with one. Manimble offers to install the agent you pick the first time you run it. See
  [The terminal](../ai/terminal.md).
- **Extra LaTeX packages** for decks that use them. Install one with `tlmgr install <package>`; use
  `sudo tlmgr install <package>` for a system-wide TeX such as MacTeX.

## Build from source

To build Manimble yourself you need Xcode or the Xcode command line tools, and Homebrew.

```bash
git clone https://github.com/pachterlab/Manimble.git && cd Manimble
./install_manimble.sh                   # installs dependencies and builds the app
open "$HOME/Applications/Manimble.app"  # then File ▸ Open Deck or Project… and try examples/hello/deck
```

The installer creates a Python environment in `~/Library/Application Support/Manimble/python`, installs BasicTeX and
the LaTeX packages Manim uses if no TeX is present, and builds `~/Applications/Manimble.app`. Run it again after
pulling changes.

## Remove Manimble

Drag Manimble from Applications to the Trash. To remove everything it installed, also delete:

- `~/Library/Application Support/Manimble` (the Python runtime, render caches and working copies)
- `~/Library/TinyTeX`, if Manimble installed TinyTeX and nothing else uses it

Your decks are never stored inside these folders.

## Linux

The Linux app uses Qt Widgets and supports Wayland and X11. It needs system Python 3.11 through 3.14.
Clone the repository and run the installer from its root:

```bash
git clone https://github.com/pachterlab/Manimble.git
cd Manimble
./install_manimble.sh
~/.local/bin/manimble examples/hello/deck
```

The installer prints the native package and Python package plans before requesting consent. Ubuntu 24.04 uses
`apt-get install`, and Arch uses `pacman -S --needed` without a full-system upgrade. Use `--no-system-deps` on a
preprovisioned machine, `--system-deps` to request native installation, or `--yes` to accept the printed plan.
Unsupported distributions receive package and missing-tool guidance instead of automatic system changes.

The app and its private Python environment are copied into `$XDG_DATA_HOME/manimble`, which defaults to
`~/.local/share/manimble`. Launchers are installed in `~/.local/bin`. The checkout can be removed after installation.
Preferences, caches and working copies use the XDG config, cache and state directories. Rerun the installer after
pulling updates. Desktop registration does not change the default handler for `.manimble` files.

The Linux bundle includes Comic Neue and Nunito Sans. When a Mac theme font is missing, the editor and renderer
resolve the same local substitute. Opening a deck does not rewrite its font names.

Ubuntu may need an AppArmor user namespace exception for the installed `QtWebEngineProcess`. The installer asks
for separate permission and scopes the profile to that exact executable. `--install-webengine-profile` explicitly
approves the exception, while `--yes` alone does not. Without the required permission, the terminal remains
unavailable. Chromium sandboxing stays enabled.

The optional extraction model is not installed by the main installer. Keynote and legacy `.ppt` conversions
require macOS, so use `.pptx` on Linux. Protected distro TeX installations need their distro packages rather than
privileged `tlmgr` changes.

See the [Linux README](https://github.com/pachterlab/Manimble/blob/main/apps/linux/README.md) for development,
testing and installed paths.

