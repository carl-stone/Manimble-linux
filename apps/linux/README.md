# Manimble for Linux

The Linux front end uses Python and PySide6 Qt Widgets with the shared Manimble engine. It supports Linux x86_64 on Ubuntu 24.04 and Arch/Omarchy, with Wayland or X11 and system Python 3.11 through 3.14.

## Install

From the repository root, run:

```bash
./install_manimble.sh
```

The installer prints its native package and Python package plans before asking for permission. Native installation uses `apt-get install` on Ubuntu 24.04 or `pacman -S --needed` on Arch. It does not upgrade the whole system, install distro Qt packages, download Python, or install an extraction model. On Arch, synchronize the package databases and system through your usual maintenance process first.

Use `--no-system-deps` if the native dependencies are already installed. Use `--system-deps` to request native installation explicitly. `--yes` accepts the printed package and payload plan without prompts.

```bash
./install_manimble.sh --no-system-deps --yes
```

Unsupported distributions can use `--no-system-deps` after installing equivalents of the package lists printed by the installer. Missing tools are reported by name. TeX font extras supply `dsfont` and `doublestroke`, and eSpeak NG supplies local speech. Missing optional tools leave their affected features unavailable.

Ubuntu's AppArmor configuration may prevent QtWebEngine from creating its sandbox. If needed, the installer separately offers a profile for the exact installed `QtWebEngineProcess` executable. `--install-webengine-profile` explicitly approves that exception. `--yes` alone does not approve it. Refusing the exception leaves the terminal unavailable until the restriction is addressed. Never disable Chromium sandboxing or change global user namespace settings to work around the restriction.

Installation copies the application rather than linking to the checkout, so the checkout can be moved or removed afterward. Rerun the installer after updating the source. The private virtual environment stays at its final path because its executable scripts contain absolute paths.

| Location | Contents |
|---|---|
| `$XDG_DATA_HOME/manimble/app/` | Copied engine, Linux front end, CLI, version, license, icons and bundled fonts |
| `$XDG_DATA_HOME/manimble/python/` | Private GUI and engine Python environment |
| `$XDG_CONFIG_HOME/manimble/` | Nonsecret preferences |
| `$XDG_CACHE_HOME/manimble/` | Renders, voice audio, the extraction model and downloads |
| `$XDG_STATE_HOME/manimble/` | Working copies and logs |
| `$HOME/.local/bin/` | `manimble` and `manimbler` launchers |

Unset or relative XDG variables use `~/.local/share`, `~/.config`, `~/.cache` and `~/.local/state`. Desktop and MIME registrations use the XDG data directory. Installation does not select a default file handler or change `mimeapps.list`.

The desktop entry uses the Manimble application icon. The `.manimble` MIME type has its own document icon.

## Run

Open Manimble from the application launcher, or use:

```bash
~/.local/bin/manimble
~/.local/bin/manimble /path/to/project/deck /path/to/project.manimble
~/.local/bin/manimble --help
~/.local/bin/manimbler --help
```

The launchers work from any current directory, including when installation paths contain spaces. `--help` does not start Qt. Add `~/.local/bin` to your shell's `PATH` if needed.

Manimble edits a working copy and writes changed files back only on Save to Original. Authored symlinks and terminal commands operating on external assets can still change external files, so the working copy is not a sandbox. Save Project Copy preserves unsaved changes without changing the original association.

On Linux, Manimble uses a private Fontconfig configuration for deck rendering and theme selection. It keeps your installed fonts and preferences, and substitutes locally available faces when a Mac theme face isn't installed. Comic Neue and Nunito Sans are bundled; nothing is downloaded at launch. Opening a deck doesn't change its font names. The terminal keeps its own font preference; for larger interface text, launch with e.g. `MANIMBLE_TEXT_SCALE=1.15`.

Keynote `.key` and legacy `.ppt` conversions require macOS. Use `.pptx` on Linux. Extract Elements uses the IS-Net model (179 MB), downloaded on first use with its own consent and run on the CPU by the main private Python. Manual extraction and background fill do not need the model.

## Develop

Create a development environment at the checkout root after provisioning native dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r apps/linux/requirements.txt
PYTHONPATH="$PWD/apps/linux:$PWD/engine" QT_API=pyside6 \
  .venv/bin/python -m manimble_linux examples/hello/deck
```

The package is flat under `apps/linux/manimble_linux/`. `rpc.py` talks to the shared engine over JSON lines through `QProcess`. `paths.py` resolves application and XDG paths independently of the current directory. GUI continuations use Qt signals and generator tasks, not an asyncio loop. Blocking file and network work uses the bounded worker pool, while CPU-heavy jobs run in cancellable processes.

## Test

On Ubuntu 24.04, install `xvfb` and `dbus-x11` in addition to the native package plan. Run the following as a non-root user from the checkout root:

```bash
./install_manimble.sh --no-system-deps --yes
PY="${XDG_DATA_HOME:-$HOME/.local/share}/manimble/python/bin/python"
export PATH="$(dirname "$PY"):$HOME/.local/bin:$PATH"
"$PY" -m pip check
"$PY" -m unittest discover -s tests -p 'test_manimbler.py' -v
PYTHONPATH="$PWD/apps/linux:$PWD/engine" QT_API=pyside6 \
  dbus-run-session -- xvfb-run -a "$PY" -m unittest discover -s apps/linux/tests -p 'test_*.py' -v
~/.local/bin/manimble --help
~/.local/bin/manimbler --help
```

If AppArmor requires the exact-path WebEngine profile, approve it separately during installation or add `--install-webengine-profile`. CI adds the flag only when the runner's AppArmor user namespace restriction is enabled. The tests do not download the optional model, call paid voice providers or submit public BioCartoon assets.

Automated tests do not establish desktop parity on their own. Check actual editor rendering, terminal input and resizing, working-copy Save/Revert/conflicts, portable project relocation, audio/video playback, extraction, plots and BioCartoon on both Wayland and X11. Check audience/presenter separation and monitor removal on a desktop with two displays. Exchange a saved project with macOS before claiming format parity.

For an installation relocation check, copy the checkout to a disposable path containing spaces and use temporary HOME/XDG directories. Install there, remove only that disposable source copy, and run both launchers from another directory. Validate `manimble.desktop` with `desktop-file-validate` and confirm that no `mimeapps.list` was created.

For the macOS side of the project exchange check, set `MACPY` to the existing absolute engine interpreter shown
by Choose Python and run from the checkout root:

```bash
"$MACPY" -m unittest discover -s tests -p 'test_manimbler.py' -v
PYTHONPATH="$PWD/engine:$PWD/apps/linux" "$MACPY" -m unittest discover -s apps/linux/tests -p 'test_portability.py' -v
apps/macos/build_app.sh
```

Check text, notes, plots and scripts after a macOS to Linux to macOS exchange. The macOS checks and desktop
observations must be run on their respective systems. A successful Linux headless run does not substitute for them.
