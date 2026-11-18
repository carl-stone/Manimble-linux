# How Manimble works

This page is for developers who want to understand Manimble's design or contribute to it.

## Two parts

Manimble is split into a platform-independent **engine**, written in Python, and a **front end**, the macOS app. They
talk over a small JSON protocol on standard input and output, described in [Engine protocol](protocol.md). Other front
ends, such as a planned Windows app, can share the same engine.

```
Manimble.app  (Swift, AppKit, Metal)                 engine/manimble  (Python)
 ├ navigator, canvas, inspector, source pane    ◄─►   rpc.py      JSON lines over stdio
 ├ Metal canvas: exact frame when idle,                core.py     scheduling, caching, edits, undo
 │   backdrop + per-object sprites while editing       srcedit.py  AST analysis → minimal source patches
 └ working copy of the deck (Workspace.swift)          worker.py   runs scenes in Manim, records what it sees
```

The Linux front end lives under `apps/linux/manimble_linux/` and uses PySide6 Qt Widgets. It shares the engine
and existing deck/project formats with macOS rather than translating Swift or adding another engine.
`RpcClient` starts the engine with `QProcess` and exchanges the same JSON-line requests, replies and events.
Mutations are serialized by the controller, and Qt signals return results to the GUI thread.

The Linux canvas paints exact frames when idle and composites sprites during edits. Its terminal uses local
xterm.js resources and a separate Python PTY helper, so the multithreaded Qt process never forks a shell itself.
Blocking file and HTTP work uses a bounded worker pool. Model inference, image fill and narrated movie composition
run in cancellable worker processes.

Linux working copies live under `$XDG_STATE_HOME/manimble`, with preferences in the XDG config directory and
renders and downloads in the XDG cache directory. The installer copies the app to `$XDG_DATA_HOME/manimble/app`
and creates its private environment at the stable path `$XDG_DATA_HOME/manimble/python`.
See the [Linux README](https://github.com/pachterlab/Manimble/blob/main/apps/linux/README.md) for installation
and tests. The macOS rendering and release paths below remain unchanged.


## Rendering

The **worker** runs a slide's real code in Manim, with animations fast-forwarded to their end states. As it runs, it
records, for every text, formula, picture, line, shape and table, the call stack that created it and every layout
call that touched it or its groups (`move_to`, `shift`, `next_to`, `set`, `arrange`, `.animate…`). It saves a full
frame per build, a backdrop, and a transparent picture of each object.

Simple animations (fades, creation, writing, growing, transforms, replacements) are fast-forwarded without Manim's
expensive per-frame setup; anything else goes through Manim's own pipeline. The fast path has been checked to give
frames identical to the real pipeline.

Renders run in separate processes, so edits never wait behind background pre-rendering, and the slide on screen
always renders first. Results are cached by a hash of everything a slide depends on: its own class, the shared modules
it uses, `manim.cfg` and the project's assets.

## Editing

An edit on the canvas becomes the smallest patch to the source that achieves it, located through the Python syntax
tree: rewrite a literal, change a `move_to`, `set` or `scale` argument, add a keyword argument, or append a call such
as `.shift(...)`. The patch is verified by rendering the slide again. If later code overrides it, so the object
doesn't end up where it should, the patch is reverted and the next strategy is tried. If none works, Manimble reports
the line that controls the position.

The canvas composites the backdrop and the object pictures on the GPU, so moving, resizing and retyping show
immediately while the verifying render runs in the background.

## The working copy

The app copies the deck into `~/Library/Application Support/Manimble/work` and opens the engine on the copy. Every
edit, from the canvas or the terminal, changes the copy; **Save to Original** writes the changed files back. The
engine fingerprints the deck's files every couple of seconds, so edits made outside the app re-render the affected
slides.

## Repository layout

```
engine/                  the Python engine (rpc, core, srcedit, worker, export, importer, themes)
docs/                    this documentation (MkDocs); docs/developers/protocol.md is the engine protocol
apps/macos/              the macOS app: Package.swift, Sources/Manimble (AppKit, Metal, SwiftTerm),
                         build_app.sh, make_dmg.sh, icons and tools
apps/macos/install_dependencies.sh       what the first-launch setup runs to install Python and LaTeX
apps/macos/tools/build_python_runtime.sh  builds the self-contained Python runtime attached to each release
apps/windows/            notes for a future Windows front end
apps/linux/              the Linux Qt Widgets front end, PTY helper, worker entry points and installer
install_manimble.sh      installs dependencies and builds ~/Applications/Manimble.app
setup_python_env.sh      installs the Python, Manim and TeX dependencies with Homebrew
manimbler                the command-line tool for .manimble packages
examples/hello/deck/     a small deck to try Manimble on
VERSION                  the app's version, checked against the release tag
.github/workflows/       CI builds, the tag-triggered signed release, and this documentation site
```

## Building and releasing

See [Build from source](../getting-started/installation.md#build-from-source). Pushing a tag such as `v0.4.2` runs
the release workflow, which builds the Python runtimes for both architectures, signs and notarizes the app, and
publishes the DMG and ZIP to GitHub Releases.
