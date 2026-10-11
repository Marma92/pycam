"""The HUD drawn over the viewfinder (docs/architecture.md §4, docs/protocol.md "HUD").

Pillow draws the whole screen into the engine's shared memory, then the camera sends
"hud_commit". The margins around the image stay opaque black: they hide the console.
"""

import mmap
import os

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WHITE = (255, 255, 255, 255)
AUTO = (150, 205, 255, 255)  # values chosen by the camera
AMBER = (255, 190, 60, 255)
RED = (255, 90, 70, 255)
BAR = (0, 0, 0, 150)
GRID = (255, 255, 255, 80)
TRANSPARENT = (0, 0, 0, 0)
OPAQUE_BLACK = (0, 0, 0, 255)
PEAKING_COLOURS = {
    "red": (255, 70, 60, 255),
    "yellow": (255, 230, 0, 255),
    "green": (70, 255, 70, 255),
    "blue": (100, 150, 255, 255),
    "white": WHITE,
}


class Hud:
    """Draws a view.HudView into the engine's HUD shared memory."""

    def __init__(self, info):
        hud = info["hud"]
        if hud.get("format") != "RGBA":
            raise ValueError(f"unsupported HUD format {hud.get('format')}")
        self.size = (hud["width"], hud["height"])
        self.rect = tuple(info["image_rect"])  # x, y, w, h of the image on the screen
        fd = os.open(hud["path"], os.O_RDWR)
        try:
            self.shm = mmap.mmap(fd, hud["stride"] * hud["height"])
        finally:
            os.close(fd)
        self.s = self.rect[3] / 1080  # sizes below are for a 1080 pixel high image
        self.font = ImageFont.truetype(FONT, max(11, round(32 * self.s)))
        self.font_bold = ImageFont.truetype(FONT_BOLD, max(12, round(40 * self.s)))
        self.font_message = ImageFont.truetype(FONT_BOLD, max(14, round(52 * self.s)))
        self._backgrounds = {}

    def close(self):
        self.shm.close()

    def px(self, value):
        return max(1, round(value * self.s))

    def draw(self, view):
        img = self._background(view.grid and view.display != "clean").copy()
        d = ImageDraw.Draw(img)
        if view.display != "clean":
            self._info_bar(d, view)
            self._status(d, view)
        if view.display == "detailed":
            self._histogram(d, view.histogram)
        if view.loupe > 1:  # whatever the display: you must know the image is magnified
            self._loupe(d, view)
        if view.message:
            self._message(d, view.message, view.warning)
        self.shm[:] = img.tobytes()

    def draw_default(self):
        """What the engine shows when no application runs: black margins, nothing else."""
        self.shm[:] = self._background(False).tobytes()

    def _background(self, grid):
        """Black margins, transparent image, maybe the rule of thirds: drawn once, then copied."""
        if grid not in self._backgrounds:
            img = Image.new("RGBA", self.size, OPAQUE_BLACK)
            d = ImageDraw.Draw(img)
            x, y, w, h = self.rect
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=TRANSPARENT)
            if grid:
                for i in (1, 2):
                    d.line([(x + w * i // 3, y), (x + w * i // 3, y + h)], fill=GRID, width=self.px(2))
                    d.line([(x, y + h * i // 3), (x + w, y + h * i // 3)], fill=GRID, width=self.px(2))
            self._backgrounds[grid] = img
        return self._backgrounds[grid]

    def _info_bar(self, d, view):
        x, y, w, h = self.rect
        bar_h = self.px(68)
        top = y + h - bar_h
        d.rectangle([x, top, x + w, y + h], fill=BAR)
        cx = x + self.px(24)
        gap = self.px(36)

        def text(value, font, colour):
            nonlocal cx
            ty = top + (bar_h - font.size) // 2 - self.px(4)
            d.text((cx, ty), value, font=font, fill=colour)
            cx += d.textlength(value, font=font) + gap

        # Mode letter in a box.
        box = self.px(48)
        by = top + (bar_h - box) // 2
        d.rounded_rectangle([cx, by, cx + box, by + box], radius=self.px(8), outline=WHITE, width=self.px(3))
        d.text((cx + box / 2, by + box / 2), view.mode, font=self.font_bold, fill=WHITE, anchor="mm")
        cx += box + gap

        text(view.shutter, self.font_bold, AUTO if view.shutter_auto else WHITE)
        text(view.iso, self.font_bold, AUTO if view.iso_auto else WHITE)
        if view.ev:
            text(view.ev, self.font, WHITE)
        text(view.awb, self.font, WHITE)
        text(view.formats, self.font, WHITE)

        shots = str(view.shots)
        d.text((x + w - self.px(24), top + bar_h // 2), shots, font=self.font_bold, fill=WHITE, anchor="rm")

    def _status(self, d, view):
        """Top left: what is switched on (ZSL, peaking, zebras) and the photos being written."""
        x, y, w, h = self.rect
        cx, cy = x + self.px(24), y + self.px(24)
        badge_h = self.font.size + self.px(14)

        def badge(label, colour):
            nonlocal cx
            tw = d.textlength(label, font=self.font)
            d.rounded_rectangle([cx, cy, cx + tw + self.px(20), cy + badge_h], radius=self.px(6), fill=BAR)
            d.text((cx + self.px(10), cy + self.px(5)), label, font=self.font, fill=colour)
            cx += tw + self.px(32)

        if view.zsl:
            badge("ZSL", AMBER)
        if view.peaking:
            badge("PEAK", PEAKING_COLOURS.get(view.peaking, WHITE))
        if view.zebra:
            badge("ZÉBRAS", WHITE)
        if view.writing:
            r = self.px(10)
            cy_mid = cy + badge_h // 2
            d.ellipse([cx, cy_mid - r, cx + 2 * r, cy_mid + r], fill=RED)
            d.text((cx + 3 * r, cy_mid), str(view.writing), font=self.font, fill=WHITE, anchor="lm")

    def _histogram(self, d, histogram):
        if not histogram:
            return
        x, y, w, h = self.rect
        hw, hh = self.px(300), self.px(140)
        hx, hy = x + w - hw - self.px(24), y + self.px(24)
        d.rectangle([hx, hy, hx + hw, hy + hh], fill=BAR)
        peak = max(histogram) or 1
        bw = hw / len(histogram)
        for i, v in enumerate(histogram):
            bh = round(hh * v / peak)
            if bh:
                d.rectangle([hx + i * bw, hy + hh - bh, hx + (i + 1) * bw - 1, hy + hh], fill=(255, 255, 255, 200))

    def _loupe(self, d, view):
        """Bottom right, above the info bar: the magnification and a map of where we look."""
        x, y, w, h = self.rect
        mw, mh = self.px(160), self.px(120)
        mx = x + w - mw - self.px(24)
        my = y + h - self.px(68) - mh - self.px(24)
        d.rectangle([mx, my, mx + mw, my + mh], fill=BAR, outline=WHITE, width=self.px(2))
        cx, cy = view.loupe_center
        lw, lh = mw / view.loupe, mh / view.loupe
        lx = mx + min(max(cx * mw - lw / 2, 0), mw - lw)
        ly = my + min(max(cy * mh - lh / 2, 0), mh - lh)
        d.rectangle([lx, ly, lx + lw, ly + lh], fill=AMBER)
        d.text((mx - self.px(16), my + mh / 2), f"×{view.loupe:g}", font=self.font_bold, fill=AMBER, anchor="rm")

    def _message(self, d, message, warning):
        x, y, w, h = self.rect
        tw = d.textlength(message, font=self.font_message)
        pad = self.px(24)
        bw, bh = tw + 2 * pad, self.font_message.size + 2 * pad
        bx, by = x + (w - bw) / 2, y + h * 0.42 - bh / 2
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=self.px(12), fill=(0, 0, 0, 170))
        d.text((x + w / 2, by + bh / 2), message, font=self.font_message, fill=AMBER if warning else WHITE, anchor="mm")
