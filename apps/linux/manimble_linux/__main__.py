"""``python -m manimble_linux``; help does not load Qt or initialize a display."""
from __future__ import annotations
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog='manimble', description='Manimble — visual editor for Manim Slides on Linux')
    parser.add_argument('paths', nargs='*', metavar='PATH', help='deck folder, Python file, project folder, or .manimble archive')
    args = parser.parse_args(argv)
    from .app import ManimbleApp
    app = ManimbleApp([sys.argv[0]])
    controller = app.new_window()
    for path in args.paths:
        controller.open_project(path)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
