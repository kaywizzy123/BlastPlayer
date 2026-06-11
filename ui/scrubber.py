from PyQt5.QtWidgets import QSlider, QStyle, QStyleOptionSlider, QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPainter, QPen, QColor


class ScrubberSlider(QSlider):
    """
    QSlider that jumps to the exact clicked position on the groove.

    Qt's default behaviour moves by a page step when clicking the groove, and
    calling super() afterwards overrides our setValue with that page step.
    We intercept groove clicks entirely: bypass super() for press/move/release,
    manually emit the standard signals, and let super() handle handle-drag as
    normal so no existing behaviour is regressed.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._groove_pressed = False
        self._in_frame:  int | None = None
        self._out_frame: int | None = None
        self._mc_clips:  list = []
        self._ann_frames: set = set()

    # -- Mouse handling -------------------------------------------------- #

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle_rect = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle_rect.contains(event.pos()):
                # Groove click: jump directly, don't let super() page-step on top.
                self._groove_pressed = True
                self.setValue(self._value_from_pos(event.pos()))
                self.sliderPressed.emit()
                event.accept()
                return
        self._groove_pressed = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._groove_pressed:
            val = max(self.minimum(),
                      min(self._value_from_pos(event.pos()), self.maximum()))
            self.setValue(val)
            self.sliderMoved.emit(val)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._groove_pressed:
            self._groove_pressed = False
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- Public setters -------------------------------------------------- #

    def set_in_out(self, in_frame, out_frame):
        self._in_frame  = in_frame
        self._out_frame = out_frame
        self.update()

    def set_mc_clips(self, clips: list):
        self._mc_clips = clips
        self.update()

    def set_annotation_frames(self, frames: set) -> None:
        self._ann_frames = frames
        self.update()

    # -- Paint ----------------------------------------------------------- #

    def paintEvent(self, event):
        super().paintEvent(event)
        if (self._in_frame is None and self._out_frame is None
                and not self._mc_clips and not self._ann_frames):
            return

        opt    = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        span   = groove.width() - handle.width()
        offset = groove.x() + handle.width() // 2
        gy     = groove.center().y()
        gh     = groove.height()

        def x_for(val):
            return offset + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), val, span)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        # Multi-clip bands: alternate a light tint on odd-indexed clips
        if len(self._mc_clips) > 1:
            band_color = QColor(255, 255, 255, 70)
            div_color  = QColor(255, 255, 255, 100)
            bh = max(gh + 4, 8)
            clips = self._mc_clips
            for i, clip in enumerate(clips):
                x_start = x_for(clip['offset'])
                x_end   = (x_for(clips[i + 1]['offset'])
                           if i < len(clips) - 1
                           else x_for(self.maximum()) + 1)
                if i % 2 == 1 and x_end > x_start:
                    painter.fillRect(x_start, gy - bh // 2,
                                     x_end - x_start, bh, band_color)
                if i > 0:
                    painter.setPen(QPen(div_color, 1))
                    painter.drawLine(x_start, gy - bh // 2, x_start, gy + bh // 2)

        # Tinted range between in and out
        x_in  = x_for(self._in_frame  if self._in_frame  is not None else self.minimum())
        x_out = x_for(self._out_frame if self._out_frame is not None else self.maximum())
        if self._in_frame is not None or self._out_frame is not None:
            if x_out > x_in:
                painter.fillRect(x_in, gy - 3, x_out - x_in, 6, QColor(255, 170, 0, 90))

        # In-point marker (green)
        if self._in_frame is not None:
            painter.setPen(QPen(QColor("#4CAF50"), 2))
            x = x_for(self._in_frame)
            painter.drawLine(x, gy - 7, x, gy + 7)

        # Out-point marker (orange-red)
        if self._out_frame is not None:
            painter.setPen(QPen(QColor("#FF6B35"), 2))
            x = x_for(self._out_frame)
            painter.drawLine(x, gy - 7, x, gy + 7)

        # Annotation frame markers — gold ticks, width scaled to frame density
        if self._ann_frames:
            total_frames = max(1, self.maximum() - self.minimum())
            frame_px = span / total_frames
            w = max(1, min(round(frame_px) - 1, 4))  # leave 1px gap; cap at 4px
            ann_color = QColor("#FFD700")
            for frame in self._ann_frames:
                if self.minimum() <= frame <= self.maximum():
                    cx = x_for(frame)
                    painter.fillRect(cx - w // 2, gy - 5, w, 10, ann_color)

        painter.end()

    # -- Private --------------------------------------------------------- #

    def _value_from_pos(self, pos) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        if self.orientation() == Qt.Horizontal:
            span    = groove.width() - handle.width()
            rel_pos = pos.x() - groove.x() - handle.width() // 2
        else:
            span    = groove.height() - handle.height()
            rel_pos = pos.y() - groove.y() - handle.height() // 2
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), rel_pos, span,
            self.invertedAppearance())
