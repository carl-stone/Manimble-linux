#!/usr/bin/env bash
# Install a copied Linux front end and a private, stable-path Python environment.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
DEPS=ask
YES=0
PROFILE=0
usage() {
    cat <<'EOF'
Usage: install_manimble.sh [OPTIONS]
  --no-system-deps             Skip native package installation
  --system-deps                Install the printed native package set
  --yes                        Accept the printed package and payload plan
  --install-webengine-profile  Separately approve an exact-path AppArmor userns profile
  --help                       Show this help

Requires system Python 3.11 through 3.14. No Python runtime is downloaded.
--yes alone never approves the AppArmor exception. The extraction model is downloaded
on first use, not here. Existing decks, preferences and MIME defaults are kept.
EOF
}
for arg in "$@"; do
    case "$arg" in
        --no-system-deps) [[ "$DEPS" != install ]] || { echo 'Conflicting dependency flags' >&2; exit 2; }; DEPS=skip ;;
        --system-deps) [[ "$DEPS" != skip ]] || { echo 'Conflicting dependency flags' >&2; exit 2; }; DEPS=install ;;
        --yes) YES=1 ;;
        --install-webengine-profile) PROFILE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
    esac
done
[[ "$(uname -s)" == Linux ]] || { echo 'This installer requires Linux.' >&2; exit 1; }
[[ "$(uname -m)" == x86_64 ]] || { echo 'Supported Linux architecture: x86_64.' >&2; exit 1; }
[[ "$HOME" == /* ]] || { echo 'HOME must be an absolute path.' >&2; exit 1; }
xdg_dir() {
    local value="${!1:-}"
    if [[ "$value" == /* ]]; then printf '%s\n' "$value"; else printf '%s/%s\n' "$HOME" "$2"; fi
}
DATA="$(xdg_dir XDG_DATA_HOME .local/share)"
CONFIG="$(xdg_dir XDG_CONFIG_HOME .config)"
CACHE="$(xdg_dir XDG_CACHE_HOME .cache)"
STATE="$(xdg_dir XDG_STATE_HOME .local/state)"
APP="$DATA/manimble/app"
VENV="$DATA/manimble/python"
BIN="$HOME/.local/bin"

UBUNTU=(
    build-essential pkg-config python3.12 python3.12-dev python3.12-venv
    libcairo2-dev libpango1.0-dev ffmpeg poppler-utils ghostscript espeak-ng
    texlive-latex-extra texlive-fonts-recommended texlive-fonts-extra
    texlive-science texlive-lang-english texlive-xetex dvisvgm
    fontconfig fonts-dejavu-core libegl1 libgl1 libopengl0 libgl1-mesa-dri libgbm1
    libdbus-1-3 libnss3 libnspr4 libasound2t64 libpulse0 libx11-xcb1
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0
    libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxcomposite1 libxdamage1
    libxrandr2 libxtst6 libxkbfile1 libwayland-client0 libwayland-cursor0 libwayland-egl1
    shared-mime-info desktop-file-utils xdg-utils
)
ARCH=(
    base-devel pkgconf python python-pip cairo pango ffmpeg poppler ghostscript espeak-ng
    texlive-latexextra texlive-fontsrecommended texlive-fontsextra texlive-mathscience
    texlive-langenglish texlive-xetex dvisvgm fontconfig ttf-dejavu mesa libglvnd
    libpulse alsa-lib dbus nss nspr libx11 libxcb libxrender libxext libsm libice
    libxkbcommon-x11 libxcomposite libxdamage libxrandr libxtst libxkbfile
    xcb-util xcb-util-cursor xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util-wm
    wayland shared-mime-info desktop-file-utils xdg-utils
)
ID=unknown
VERSION_ID=
if [[ -f /etc/os-release ]]; then
    # os-release is a system-owned shell assignment file.
    source /etc/os-release
fi
PACKAGES=()
MANAGER=
if [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]; then
    PACKAGES=("${UBUNTU[@]}"); MANAGER=apt
elif [[ "$ID" == arch || "$ID" == omarchy ]]; then
    PACKAGES=("${ARCH[@]}"); MANAGER=pacman
fi
printf 'Install payload: %s\nPrivate Python environment: %s\nLaunchers: %s\n' "$APP" "$VENV" "$BIN"
printf 'Preferences: %s/manimble\nCache: %s/manimble\nWorkspaces/logs: %s/manimble\n' "$CONFIG" "$CACHE" "$STATE"
echo 'Python packages will be downloaded from the configured pip index using apps/linux/requirements.txt:'
cat -- "$ROOT/apps/linux/requirements.txt"
if [[ -n "$MANAGER" ]]; then
    printf 'Native package plan (%s, no full-system upgrade):\n' "$MANAGER"
    printf '  %s\n' "${PACKAGES[*]}"
else
    printf 'No automatic native installation for %s %s. Preinstall the following equivalents.\n' "$ID" "$VERSION_ID"
    printf 'Ubuntu 24.04: %s\nArch: %s\n' "${UBUNTU[*]}" "${ARCH[*]}"
    [[ "$DEPS" != install ]] || { echo 'Use --no-system-deps after provisioning these dependencies.' >&2; exit 1; }
    DEPS=skip
fi
confirm() {
    local answer
    [[ -t 0 ]] || return 1
    read -r -p "$1 [y/N] " answer
    [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]]
}
if [[ "$DEPS" == ask ]]; then
    if [[ "$YES" == 1 ]] || confirm 'Install the listed native dependencies (may use sudo)?'; then DEPS=install; else DEPS=skip; fi
fi
if [[ "$YES" != 1 ]] && ! confirm 'Install/update the copied payload and download its Python packages?'; then
    echo 'Installation cancelled.'; exit 1
fi
if [[ "$DEPS" == install ]]; then
    SUDO=()
    if [[ "$EUID" != 0 ]]; then
        command -v sudo >/dev/null || { echo 'sudo is required for the approved native package installation.' >&2; exit 1; }
        SUDO=(sudo)
    fi
    case "$MANAGER" in
        apt) "${SUDO[@]}" apt-get install -y -- "${PACKAGES[@]}" ;;
        pacman) "${SUDO[@]}" pacman -S --needed --noconfirm -- "${PACKAGES[@]}" ;;
    esac
fi

PYTHON=
for candidate in /usr/bin/python3 /usr/bin/python3.14 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/local/bin/python3; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] <= (3, 14)))'; then
        PYTHON="$candidate"; break
    fi
done
[[ -n "$PYTHON" ]] || { echo 'Install system Python 3.11 through 3.14 with venv support, then rerun this installer.' >&2; exit 1; }
MISSING=()
for tool in cc pkg-config ffmpeg ffprobe pdftoppm gs espeak-ng latex xelatex dvisvgm fc-match update-mime-database update-desktop-database; do
    command -v "$tool" >/dev/null || MISSING+=("$tool")
done
if [[ "${#MISSING[@]}" -gt 0 ]]; then
    printf 'Missing native tools: %s\n' "${MISSING[*]}" >&2
    echo 'Install their packages from the native package plan above. Affected features remain unavailable until then.' >&2
fi
for library in cairo pangocairo; do
    if ! command -v pkg-config >/dev/null || ! pkg-config --exists "$library"; then
        printf 'Missing build dependency: %s (Ubuntu libcairo2-dev/libpango1.0-dev; Arch cairo/pango).\n' "$library" >&2
    fi
done

mkdir -p -- "$DATA/manimble" "$CONFIG/manimble" "$CACHE/manimble" "$STATE/manimble" "$BIN"
# The payload is staged, but the venv always lives at its final path because scripts embed it.
"$PYTHON" - "$ROOT" "$APP" <<'PY'
import os
from pathlib import Path
import shutil
import sys
import tempfile

root, app = map(Path, sys.argv[1:])
if app.resolve() == root.resolve():
    raise SystemExit('Refusing to replace the source checkout with the installed payload.')
stage = Path(tempfile.mkdtemp(prefix='.app-', dir=app.parent))
backup = None
try:
    for name in ('VERSION', 'LICENSE', 'manimbler'):
        shutil.copy2(root / name, stage / name)
    for name in ('engine', 'apps/linux'):
        shutil.copytree(root / name, stage / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.venv', '.pytest_cache'))
    if app.exists():
        backup = Path(tempfile.mkdtemp(prefix='.app-old-', dir=app.parent)) / 'app'
        os.replace(app, backup)
    try:
        os.replace(stage, app)
    except BaseException:
        if backup is not None:
            os.replace(backup, app)
        raise
finally:
    if stage.exists():
        shutil.rmtree(stage)
    if backup is not None:
        shutil.rmtree(backup.parent)
PY
unset PYTHONHOME PYTHONPATH QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH QML_IMPORT_PATH
export QT_API=pyside6
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] <= (3, 14)))' || {
    echo "The existing private environment at $VENV uses unsupported Python. Remove only that environment and rerun." >&2; exit 1;
}
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$APP/apps/linux/requirements.txt"
"$VENV/bin/python" - "$APP" "$VENV" "$BIN" "$DATA" "$HOME" "$ROOT" <<'PY'
from pathlib import Path
import os
import shlex
import sys
from PIL import Image

app, venv, bin_dir, data, home, root = map(Path, sys.argv[1:])

def atomic(path, text, mode=0o644):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.chmod(mode)
    os.replace(temporary, path)

# Quote shell tokens, not a joined command line. Preserve an explicit interpreter choice.
prefix = '\n'.join((
    '#!/usr/bin/env bash', 'set -euo pipefail',
    'unset PYTHONHOME QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH QML_IMPORT_PATH',
    'export QT_API=pyside6',
    'export PYTHONPATH=' + shlex.quote(str(app / 'apps/linux') + ':' + str(app / 'engine')),
    'if [[ -z "${MANIMBLE_PYTHON:-}" ]]; then export MANIMBLE_PYTHON=' + shlex.quote(str(venv / 'bin/python')) + '; fi',
    # The bounded list also prevents an inherited development venv from taking precedence.
    'export PATH=' + shlex.quote(':'.join(dict.fromkeys(map(str, (venv / 'bin', home / '.local/bin', Path('/usr/local/bin'), Path('/usr/bin'), Path('/bin')))))),
)) + '\n'
for name, tail in (('manimble', ['-m', 'manimble_linux']), ('manimbler', [str(app / 'manimbler')])):
    command = ' '.join(map(shlex.quote, [str(venv / 'bin/python'), *tail]))
    atomic(bin_dir / name, prefix + 'exec ' + command + ' "$@"\n', 0o755)

applications = data / 'applications'
applications.mkdir(parents=True, exist_ok=True)
launcher = str(bin_dir / 'manimble')
# Exec has its own quoting layer, followed by Desktop Entry string escaping.
exec_token = '"' + ''.join(('\\' + c) if c in '\\"`$' else ('%%' if c == '%' else c) for c in launcher) + '"'
exec_token = exec_token.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
template = (app / 'apps/linux/resources/manimble.desktop').read_text(encoding='utf-8')
atomic(applications / 'manimble.desktop', template.replace('@MANIMBLE_EXEC@', exec_token))
mime = data / 'mime/packages'
mime.mkdir(parents=True, exist_ok=True)
atomic(mime / 'manimble-mime.xml', (app / 'apps/linux/resources/manimble-mime.xml').read_text(encoding='utf-8'))
icon = data / 'icons/hicolor/256x256/apps/manimble.png'
icon.parent.mkdir(parents=True, exist_ok=True)
with Image.open(root / 'apps/macos/assets/Manimble.icns') as source:
    image = source.convert('RGBA').resize((256, 256), Image.Resampling.LANCZOS)
    temporary = icon.with_suffix('.tmp')
    image.save(temporary, format='PNG')
    os.replace(temporary, icon)
PY

# Ubuntu may forbid Chromium's user namespace sandbox. The exception is separate
# from package consent, names one installed executable, and never disables sandboxing.
RESTRICTED=0
if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]] && [[ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" == 1 ]]; then
    RESTRICTED=1
fi
if [[ "$RESTRICTED" == 1 || "$PROFILE" == 1 ]]; then
    WEBENGINE="$("$VENV/bin/python" -c 'from pathlib import Path; import PySide6; print(Path(PySide6.__file__).parent / "Qt/libexec/QtWebEngineProcess")')"
    [[ -x "$WEBENGINE" ]] || { printf 'QtWebEngineProcess not found: %s\n' "$WEBENGINE" >&2; exit 1; }
    printf 'AppArmor exception target: %s\n' "$WEBENGINE"
    echo 'This grants user namespaces to only that QtWebEngineProcess executable. Chromium sandboxing stays enabled.'
    if [[ "$PROFILE" == 1 ]] || confirm 'Install and load this exact-path AppArmor profile with administrator privileges?'; then
        command -v apparmor_parser >/dev/null || { echo 'Install apparmor (apparmor_parser) to load the approved profile.' >&2; exit 1; }
        PROFILE_TMP="$(mktemp)"
        trap 'rm -f -- "$PROFILE_TMP"' EXIT
        "$VENV/bin/python" - "$WEBENGINE" "$PROFILE_TMP" <<'PY'
import hashlib
from pathlib import Path
import sys
path, dest = sys.argv[1:]
# AppArmor attachment paths are glob patterns even inside quotes.
escaped = ''.join('\\' + c if c in '\\"*?[]{}' else c for c in path)
name = 'manimble-webengine-' + hashlib.sha256(path.encode()).hexdigest()[:16]
Path(dest).write_text('# Exact installed QtWebEngineProcess only.\nprofile ' + name + ' "' + escaped + '" flags=(unconfined) {\n  userns,\n}\n')
print(name)
PY
        PROFILE_NAME="manimble-webengine-$(printf '%s' "$WEBENGINE" | sha256sum | cut -c1-16)"
        SUDO=()
        if [[ "$EUID" != 0 ]]; then SUDO=(sudo); fi
        "${SUDO[@]}" install -m 644 -- "$PROFILE_TMP" "/etc/apparmor.d/$PROFILE_NAME"
        "${SUDO[@]}" apparmor_parser -r "/etc/apparmor.d/$PROFILE_NAME"
        rm -f -- "$PROFILE_TMP"
        trap - EXIT
    else
        echo 'Terminal unavailable while AppArmor denies user namespaces. Rerun with --install-webengine-profile to approve the exact-path exception.' >&2
    fi
fi
if command -v update-mime-database >/dev/null; then update-mime-database "$DATA/mime"; fi
if command -v update-desktop-database >/dev/null; then update-desktop-database "$DATA/applications"; fi
if command -v gtk-update-icon-cache >/dev/null; then gtk-update-icon-cache --force --ignore-theme-index "$DATA/icons/hicolor"; fi
printf 'Installed Manimble. Run %s or open Manimble from your application launcher.\n' "$BIN/manimble"
echo 'No default file handler was changed. The extraction model is downloaded on first use.'
