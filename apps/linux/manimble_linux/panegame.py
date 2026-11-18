"""The source pane's keyboard-local falling-block game."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from . import prefs
from .tasks import spawn


@dataclass
class Piece:
    kind: int
    rot: int = 0
    x: int = 0
    y: int = 0


class AuxPaneView(QWidget):
    cols, rows = 10, 20
    kinds = [
        ([(0, 1), (1, 1), (2, 1), (3, 1)], 4, "#4dd1f2"),
        ([(0, 0), (1, 0), (0, 1), (1, 1)], 2, "#ffcc40"),
        ([(1, 0), (0, 1), (1, 1), (2, 1)], 3, "#9e66ff"),
        ([(1, 0), (2, 0), (0, 1), (1, 1)], 3, "#66d97a"),
        ([(0, 0), (1, 0), (1, 1), (2, 1)], 3, "#ff6173"),
        ([(0, 0), (0, 1), (1, 1), (2, 1)], 3, "#598cff"),
        ([(2, 0), (0, 1), (1, 1), (2, 1)], 3, "#ff9f43"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.on_exit = None
        self.best = prefs.get_int("auxPaneBest")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.new_game()
        self.installEventFilter(self)
        QApplication.instance().applicationStateChanged.connect(self._application_state)

    def _application_state(self, state):
        if state != Qt.ApplicationState.ApplicationActive:
            self.pause()

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride:
            # While focused, deck accelerators must not steal local game keys.
            if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                               Qt.Key.Key_X, Qt.Key.Key_Z, Qt.Key.Key_P, Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Escape) and not event.modifiers():
                event.accept()
                return True
        if event.type() == QEvent.Type.WindowDeactivate:
            self.pause()
        return super().event(event)

    def focusOutEvent(self, event):
        self.pause()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self.pause()
        super().hideEvent(event)

    def stop(self):
        self.timer.stop()
        self.save_best()

    @property
    def level(self):
        return self.lines // 10 + 1

    def new_game(self):
        self.board = [[None]*self.cols for _ in range(self.rows)]
        self.score = self.lines = 0
        self.over = self.paused = False
        self.bag = []
        self.next = self.draw()
        self.spawn()
        self.schedule()
        self.update()

    def draw(self):
        if not self.bag:
            self.bag = list(range(len(self.kinds)))
            random.shuffle(self.bag)
        return self.bag.pop()

    def spawn(self):
        box = self.kinds[self.next][1]
        self.piece = Piece(self.next, x=(self.cols-box)//2, y=-1 if box == 4 else 0)
        self.next = self.draw()
        if not self.fits(self.piece):
            self.over = True
            self.stop()

    def cells(self, piece):
        cells, box, _ = self.kinds[piece.kind]
        out = []
        for x, y in cells:
            if box > 2:
                for _ in range(piece.rot % 4):
                    x, y = box-1-y, x
            out.append((x+piece.x, y+piece.y))
        return out

    def fits(self, piece):
        return all(0 <= x < self.cols and y < self.rows and (y < 0 or self.board[y][x] is None) for x, y in self.cells(piece))

    def shift(self, dx, dy):
        piece = replace(self.piece, x=self.piece.x+dx, y=self.piece.y+dy)
        if not self.fits(piece):
            return False
        self.piece = piece
        return True

    def rotate(self, direction):
        piece = replace(self.piece, rot=(self.piece.rot+direction)%4)
        for dx, dy in [(0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1)]:
            candidate = replace(piece, x=piece.x+dx, y=piece.y+dy)
            if self.fits(candidate):
                self.piece = candidate
                return

    def ghost(self):
        piece = replace(self.piece)
        while self.fits(replace(piece, y=piece.y+1)):
            piece.y += 1
        return piece

    def lock(self):
        for x, y in self.cells(self.piece):
            if y >= 0:
                self.board[y][x] = self.piece.kind
        kept = [row for row in self.board if None in row]
        cleared = self.rows-len(kept)
        self.board = [[None]*self.cols for _ in range(cleared)] + kept
        if cleared:
            before = self.level
            self.score += [0, 100, 300, 500, 800][cleared]*self.level
            self.lines += cleared
            self.save_best()
            if before != self.level:
                self.schedule()
        self.spawn()

    def tick(self):
        if not self.paused and not self.over:
            if not self.shift(0, 1):
                self.lock()
            self.update()

    def schedule(self):
        self.timer.stop()
        if not self.over and not self.paused:
            self.timer.start(round(max(.06, .8*.85**(self.level-1))*1000))

    def pause(self):
        if not self.over and not self.paused:
            self.paused = True
            self.timer.stop()
            self.save_best()
            self.update()

    def resume(self):
        if not self.over and self.paused:
            self.paused = False
            self.schedule()
            self.update()

    def save_best(self):
        if self.score > self.best:
            self.best = self.score
            prefs.set("auxPaneBest", self.best)

    def mousePressEvent(self, event):
        self.setFocus()
        self.resume()
        event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if event.modifiers():
            return super().keyPressEvent(event)
        if key == Qt.Key.Key_Escape:
            if self.on_exit:
                self.on_exit()
            event.accept()
            return
        if self.over:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                self.new_game()
            event.accept()
            return
        if key == Qt.Key.Key_P:
            self.resume() if self.paused else self.pause()
        elif self.paused:
            self.resume()
        elif key == Qt.Key.Key_Left:
            self.shift(-1, 0)
        elif key == Qt.Key.Key_Right:
            self.shift(1, 0)
        elif key == Qt.Key.Key_Down:
            if self.shift(0, 1):
                self.score += 1
            else:
                self.lock()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_X):
            self.rotate(1)
        elif key == Qt.Key.Key_Z:
            self.rotate(-1)
        elif key == Qt.Key.Key_Space:
            ghost = self.ghost()
            self.score += 2*(ghost.y-self.piece.y)
            self.piece = ghost
            self.lock()
        else:
            return super().keyPressEvent(event)
        self.update()
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#1b1b1b"))
        cell = math.floor(min((self.width()-42)/16, (self.height()-28)/20))
        if cell < 4:
            return
        bw, bh, panel = cell*10, cell*20, cell*5
        ox, oy = math.floor((self.width()-bw-14-panel)/2), math.floor((self.height()-bh)/2)
        well = QRectF(ox, oy, bw, bh)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#131313"))
        painter.drawRoundedRect(well.adjusted(-3, -3, 3, 3), 6, 6)
        painter.setPen(QColor(255, 255, 255, 10))
        for x in range(1, self.cols):
            painter.drawLine(QPointF(ox+x*cell, oy), QPointF(ox+x*cell, oy+bh))
        for y in range(1, self.rows):
            painter.drawLine(QPointF(ox, oy+y*cell), QPointF(ox+bw, oy+y*cell))
        def block(x, y, kind, origin=(ox, oy), size=cell, ghost=False):
            rect = QRectF(origin[0]+x*size+1, origin[1]+y*size+1, size-2, size-2)
            color = QColor(self.kinds[kind][2])
            if ghost:
                color.setAlphaF(.35)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(color, 1.5))
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
            painter.drawRoundedRect(rect, size*.18, size*.18)
            if not ghost:
                painter.fillRect(QRectF(rect.x()+2, rect.y()+2, max(0, rect.width()-4), max(1, size*.12)), QColor(255, 255, 255, 46))
        for y, row in enumerate(self.board):
            for x, kind in enumerate(row):
                if kind is not None:
                    block(x, y, kind)
        if not self.over:
            for x, y in self.cells(self.ghost()):
                if y >= 0:
                    block(x, y, self.piece.kind, ghost=True)
            for x, y in self.cells(self.piece):
                if y >= 0:
                    block(x, y, self.piece.kind)
        px, y = ox+bw+14, oy
        small, big = max(9, cell*.42), max(11, cell*.7)
        def text(value, size=small, alpha=.85):
            nonlocal y
            font = QFont()
            font.setPixelSize(round(size))
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, round(255*alpha)))
            painter.drawText(QPointF(px, y+size), value)
            y += size*1.4
        text("NEXT", alpha=.5)
        cells = self.kinds[self.next][0]
        size = cell*.8
        nx, ny = min(x for x, _ in cells), min(y for _, y in cells)
        nw = (max(x for x, _ in cells)-nx+1)*size
        for x, yy in cells:
            block(x, yy, self.next, (px+(panel-nw)/2-nx*size, y-ny*size), size)
        y += size*2+cell
        for label, value in [("SCORE", self.score), ("LINES", self.lines), ("LEVEL", self.level), ("BEST", max(self.best, self.score))]:
            text(label, alpha=.5)
            text(str(value), big)
            y += cell*.4
        y = max(y, oy+bh-small*1.4*7)
        for line in ["← → move", "↑ X rotate", "Z rotate back", "↓ drop", "space slam", "P pause", "esc back to code"]:
            text(line, alpha=.4)
        if self.over or self.paused:
            painter.fillRect(well, QColor(0, 0, 0, 140))
            painter.setPen(QColor("white"))
            painter.drawText(well.adjusted(0, -20, 0, -20), Qt.AlignmentFlag.AlignCenter, "GAME OVER" if self.over else "PAUSED")
            painter.drawText(well.adjusted(0, 20, 0, 20), Qt.AlignmentFlag.AlignCenter, "Return to play again" if self.over else "P or click to resume")


class PaneOverlayMixin:
    def toggle_pane_overlay(self, *_):
        current = getattr(self, "pane_overlay", None)
        source = self.source_box
        if current:
            current.stop()
            current.hide()
            current.deleteLater()
            self.pane_overlay = None
            source.show()
            self.canvas.setFocus()
            return
        def open_game():
            yield from self.finish_edits()
            if getattr(self, "pane_overlay", None):
                return
            card = source
            game = AuxPaneView(card)
            self.pane_overlay = game
            game.on_exit = self.toggle_pane_overlay
            # Match the source's actual layout rectangle, including later splitter resizes.
            game.setGeometry(card.rect())
            class ResizeFollower(QWidget):
                def eventFilter(follower, watched, event):
                    if event.type() == QEvent.Type.Resize:
                        game.setGeometry(card.rect())
                    return False
            game._resize_follower = ResizeFollower(game)
            card.installEventFilter(game._resize_follower)
            game.show()
            game.raise_()
            game.setFocus()
        return spawn(open_game()).future
