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
# RGB histogram: the colour of each overlap of the channels (0 red, 1 green, 2 blue).
HISTOGRAM_COLOURS = {
    frozenset({0, 1, 2}): (255, 255, 255, 210),
    frozenset({0, 1}): (255, 235, 60, 210),
    frozenset({0, 2}): (240, 80, 255, 210),
    frozenset({1, 2}): (60, 230, 255, 210),
    frozenset({0}): (255, 70, 60, 210),
    frozenset({1}): (70, 230, 70, 210),
    frozenset({2}): (70, 120, 255, 210),
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
        self.font_countdown = ImageFont.truetype(FONT_BOLD, max(40, round(300 * self.s)))
        self.font_timer = ImageFont.truetype(FONT_BOLD, max(30, round(200 * self.s)))
        self._backgrounds = {}
        self._review = (None, None)  # (path, image fitted to the image area)
        self._full = (None, None)  # (path, full resolution image), for the playback loupe

    def close(self):
        self.shm.close()

    def px(self, value):
        return max(1, round(value * self.s))

    def load_review(self, path):
        """Decodes a photo for the review, fitted to the image area; returns its histograms,
        luminance and (red, green, blue), of 64 classes. Slow (~0.25 s): to run off the event
        loop."""
        x, y, w, h = self.rect
        with Image.open(path) as im:
            # The JPEG decoder shrinks by 2, 4 or 8 for almost nothing: a 12 MP photo
            # decodes in ~0.2 s at 1/4 on a Pi 3B, against ~0.75 s at 1/2.
            im.draft("RGB", (w * 2 // 3, h * 2 // 3))
            im = im.convert("RGB")
            scale = min(w / im.width, h / im.height)
            photo = im.resize((round(im.width * scale), round(im.height * scale)), Image.BILINEAR)
        self._review = (path, photo)

        def bins(levels):
            return [sum(levels[i : i + 4]) for i in range(0, 256, 4)]

        rgb = photo.histogram()  # 3 x 256
        return bins(photo.convert("L").histogram()), [bins(rgb[c * 256 : (c + 1) * 256]) for c in range(3)]

    def draw(self, view):
        if view.video_playing:
            self._draw_video(view)
            return
        if view.review:
            self._draw_review(view)
            return
        img = self._background(view.grid and view.display != "clean", view.video_mode).copy()
        d = ImageDraw.Draw(img)
        if view.display != "clean":
            self._info_bar(d, view)
            self._status(d, view)
            if view.metering == "spot" and view.loupe == 1 and not view.menu:
                self._spot_area(d)
        if view.display == "detailed":
            self._histogram(d, view)
        if view.loupe > 1:  # whatever the display: you must know the image is magnified
            self._loupe(d, view)
        if view.countdown:  # whatever the display: the self-timer is running
            self._countdown(d, view.countdown)
        if view.exposure_timer:  # and the exposure
            self._exposure_timer(d, view.exposure_timer, view.exposure_label)
        if view.menu:
            self._menu(d, view)
        if view.message:
            self._message(d, view.message, view.warning)
        self.shm[:] = img.tobytes()

    def draw_default(self):
        """What the engine shows when no application runs: black margins, nothing else."""
        self.shm[:] = self._background(False).tobytes()

    def _draw_review(self, view):
        """A photo (just taken, or in playback), opaque over the viewfinder, with what it was
        shot at. In playback, maybe magnified (view.loupe: against the fitted photo)."""
        img = self._background(False).copy()
        path, photo = self._review
        x, y, w, h = self.rect
        if path == view.review and view.loupe > 1:
            photo = self._magnified(path, view.loupe, view.loupe_center)
        if path == view.review and photo is not None:
            img.paste(photo, (x + (w - photo.width) // 2, y + (h - photo.height) // 2))
        d = ImageDraw.Draw(img)
        if view.display != "clean":
            bar_h = self.px(68)
            d.rectangle([x, y + h - bar_h, x + w, y + h], fill=BAR)
            d.text((x + self.px(24), y + h - bar_h // 2), view.review_info, font=self.font_bold, fill=WHITE, anchor="lm")
            if view.playback:
                d.text((x + w - self.px(24), y + h - bar_h // 2), view.playback, font=self.font_bold, fill=AMBER, anchor="rm")
        if view.video_hint and path == view.review and photo is not None:
            self._play_button(d, x + w / 2, y + h / 2, view.video_hint)
        if view.display == "detailed":
            self._histogram(d, view)
        if view.loupe > 1:
            self._loupe(d, view)
        if view.menu:  # the photo's menu, in playback
            self._menu(d, view)
        if view.message:
            self._message(d, view.message, view.warning)
        self.shm[:] = img.tobytes()

    def _play_button(self, d, cx, cy, hint):
        """A video's first frame: a play sign in the middle, and how to play it."""
        r = self.px(70)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 150), outline=WHITE, width=self.px(4))
        t = r * 0.45
        d.polygon([(cx - t * 0.7, cy - t), (cx - t * 0.7, cy + t), (cx + t, cy)], fill=WHITE)
        d.text((cx, cy + r + self.px(16)), hint, font=self.font, fill=WHITE, anchor="ma",
               stroke_width=self.px(3), stroke_fill=(0, 0, 0, 160))  # fmt: skip

    def _draw_video(self, view):
        """A video played by the engine on the video plane, in the image area, under this
        HUD: transparent there but for a bar at the bottom (name, time, progress) and the
        pause sign; black around, as for the photos."""
        img = self._background(False).copy()
        d = ImageDraw.Draw(img)
        x0, y0, sw, sh = self.rect
        if view.video_paused:
            cx, cy, r = x0 + sw / 2, y0 + sh / 2, self.px(70)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 150), outline=WHITE, width=self.px(4))
            bw, bh = r * 0.22, r * 0.9
            for side in (-1, 1):
                bx = cx + side * r * 0.25
                d.rectangle([bx - bw / 2, cy - bh / 2, bx + bw / 2, cy + bh / 2], fill=WHITE)
        if view.display != "clean":
            bar_h = self.px(68)
            top = y0 + sh - bar_h
            d.rectangle([x0, top, x0 + sw, y0 + sh], fill=BAR)
            line = self.px(6)
            d.rectangle([x0, top - line, x0 + sw, top], fill=(255, 255, 255, 70))
            d.rectangle([x0, top - line, x0 + round(sw * view.video_progress), top], fill=AMBER)
            d.text((x0 + self.px(24), top + bar_h // 2), view.review_info, font=self.font_bold, fill=WHITE, anchor="lm")
            right = x0 + sw - self.px(24)
            if view.playback:
                d.text((right, top + bar_h // 2), view.playback, font=self.font_bold, fill=AMBER, anchor="rm")
                right -= d.textlength(view.playback, font=self.font_bold) + self.px(36)
            d.text((right, top + bar_h // 2), view.video_time, font=self.font_bold, fill=WHITE, anchor="rm")
        if view.message:
            self._message(d, view.message, view.warning)
        self.shm[:] = img.tobytes()

    def _magnified(self, path, zoom, center):
        """The part of the photo shown at `zoom` times the fitted size, around `center`. The
        full resolution photo is decoded once (~1 s for 12 MP), then only cropped."""
        if self._full[0] != path:
            with Image.open(path) as im:
                self._full = (path, im.convert("RGB"))
        full = self._full[1]
        x, y, w, h = self.rect
        scale = min(w / full.width, h / full.height) * zoom  # screen pixels per photo pixel
        cw, ch = min(full.width, w / scale), min(full.height, h / scale)
        left = min(max(center[0] * full.width - cw / 2, 0), full.width - cw)
        top = min(max(center[1] * full.height - ch / 2, 0), full.height - ch)
        crop = full.crop((round(left), round(top), round(left + cw), round(top + ch)))
        # Beyond 100 %, the pixels as squares: that is what one looks for.
        return crop.resize((round(cw * scale), round(ch * scale)), Image.NEAREST if scale > 1 else Image.BILINEAR)

    def video_rect(self):
        """The 16:9 image of the video mode, in the image area (letterboxed as the engine
        shows it)."""
        x, y, w, h = self.rect
        vh = min(h, round(w * 9 / 16)) & ~1
        return x, y + (h - vh) // 2, w, vh

    def _background(self, grid, video=False):
        """Black margins, transparent image, maybe the rule of thirds: drawn once, then copied.
        video: the 16:9 image of the video mode, black above and below it."""
        key = (grid, video)
        if key not in self._backgrounds:
            img = Image.new("RGBA", self.size, OPAQUE_BLACK)
            d = ImageDraw.Draw(img)
            x, y, w, h = self.video_rect() if video else self.rect
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=TRANSPARENT)
            if grid:
                for i in (1, 2):
                    d.line([(x + w * i // 3, y), (x + w * i // 3, y + h)], fill=GRID, width=self.px(2))
                    d.line([(x, y + h * i // 3), (x + w, y + h * i // 3)], fill=GRID, width=self.px(2))
            self._backgrounds[key] = img
        return self._backgrounds[key]

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
        cx += box + self.px(14)
        self._metering_icon(d, cx, by, box, view.metering)
        cx += box + gap

        text(view.shutter, self.font_bold, AUTO if view.shutter_auto else WHITE)
        text(view.iso, self.font_bold, AUTO if view.iso_auto else WHITE)
        if view.meter is not None:
            cx += self._meter(d, cx, top, bar_h, view.meter, view.meter_warning) + gap
        if view.ev:
            text(view.ev, self.font, WHITE)
        text(view.awb, self.font, WHITE)
        text(view.formats, self.font, WHITE)
        if view.look:
            text(view.look, self.font_bold, WHITE)
        if view.lens:
            text(view.lens, self.font, WHITE)

        shots = str(view.shots)
        d.text((x + w - self.px(24), top + bar_h // 2), shots, font=self.font_bold, fill=WHITE, anchor="rm")

    def _metering_icon(self, d, x, y, size, metering):
        """A frame with what the metering looks at: a ring (centre), a dot (spot), a grid (matrix)."""
        w = self.px(2)
        d.rectangle([x, y + size * 0.15, x + size, y + size * 0.85], outline=WHITE, width=w)
        cx, cy = x + size / 2, y + size / 2
        if metering == "spot":
            r = size * 0.09
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
        elif metering == "matrix":
            for gx in (0.25, 0.5, 0.75):
                for gy in (0.32, 0.5, 0.68):
                    r = size * 0.05
                    px, py = x + size * gx, y + size * gy
                    d.ellipse([px - r, py - r, px + r, py + r], fill=WHITE)
        else:
            r = size * 0.2
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=w)
            r = size * 0.06
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)

    def _spot_area(self, d):
        """Where spot metering looks: the central zone of the ISP statistics (approximate)."""
        x, y, w, h = self.rect
        r = h * 0.09
        cx, cy = x + w / 2, y + h / 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 160), width=self.px(2))

    def _meter(self, d, x, top, bar_h, stops, warning):
        """The exposure scale, -3 to +3 stops in thirds, with a bar from 0 to the measure.
        Returns its width."""
        width = self.px(190)
        unit = width / 6  # one stop
        mid_x, mid_y = x + width / 2, top + bar_h / 2
        colour = AMBER if warning or abs(stops) > 3 else WHITE
        for third in range(-9, 10):
            tx = mid_x + third * unit / 3
            tick = self.px(14) if third == 0 else self.px(9) if third % 3 == 0 else self.px(4)
            d.line([(tx, mid_y - tick), (tx, mid_y - 1)], fill=WHITE, width=self.px(2))
        end = mid_x + max(-3.0, min(3.0, stops)) * unit
        d.rectangle([min(mid_x, end), mid_y + self.px(3), max(mid_x, end) + 1, mid_y + self.px(11)], fill=colour)
        if abs(stops) > 3:  # beyond the scale: an arrow at its end
            tip = x + (width + self.px(14) if stops > 0 else -self.px(14))
            base = x + (width if stops > 0 else 0)
            d.polygon([(base, mid_y + self.px(1)), (base, mid_y + self.px(13)), (tip, mid_y + self.px(7))], fill=colour)
        return width

    def _status(self, d, view):
        """Top left: the drive mode (amber while its sequence runs), what is switched on (ZSL,
        peaking, zebras) and the photos being written."""
        x, y, w, h = self.rect
        cx, cy = x + self.px(24), y + self.px(24)
        badge_h = self.font.size + self.px(14)

        def badge(label, colour):
            nonlocal cx
            tw = d.textlength(label, font=self.font)
            d.rounded_rectangle([cx, cy, cx + tw + self.px(20), cy + badge_h], radius=self.px(6), fill=BAR)
            d.text((cx + self.px(10), cy + self.px(5)), label, font=self.font, fill=colour)
            cx += tw + self.px(32)

        if view.recording:
            badge(f"● REC {view.recording}", RED)
        elif view.video_mode:
            badge("VIDÉO", WHITE)
        if view.drive:
            badge(view.drive, AMBER if view.drive_running else WHITE)
        if view.viewfinder_auto:  # the exposure is too long to be shown
            badge("VISÉE AUTO", AMBER)
        if view.task:
            badge(view.task, AUTO)
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

    def _histogram(self, d, view):
        """Top right. The RGB one is additive: where the three channels overlap it is white,
        where two do it is their mix (yellow, magenta, cyan), above it the highest channel."""
        channels = view.histogram_rgb or ((view.histogram,) if view.histogram else ())
        if not channels:
            return
        x, y, w, h = self.rect
        hw, hh = self.px(300), self.px(140)
        hx, hy = x + w - hw - self.px(24), y + self.px(24)
        d.rectangle([hx, hy, hx + hw, hy + hh], fill=BAR)
        # Scaled on the inner classes: a clipped end (black or white) shoots to the top
        # instead of flattening the rest.
        peak = max((max(c[1:-1]) for c in channels), default=0) or max(max(c) for c in channels) or 1
        n = len(channels[0])
        bw = hw / n
        for i in range(n):
            heights = sorted(
                ((min(hh, round(hh * c[i] / peak)), k) for k, c in enumerate(channels)), reverse=True
            )  # highest first
            bottom = 0
            for level, (bh, _) in enumerate(reversed(heights)):  # lowest first
                if bh > bottom:
                    on = frozenset(k for _, k in heights[: len(heights) - level])
                    colour = HISTOGRAM_COLOURS[on] if len(channels) == 3 else (255, 255, 255, 200)
                    d.rectangle([hx + i * bw, hy + hh - bh, hx + (i + 1) * bw - 1, hy + hh - bottom], fill=colour)
                    bottom = bh

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
        label = view.loupe_label or f"×{view.loupe:g}"
        d.text((mx - self.px(16), my + mh / 2), label, font=self.font_bold, fill=AMBER, anchor="rm")

    def _menu(self, d, view):
        """A menu, over the left part of the image; the chosen line in amber, section titles
        in blue. A long menu scrolls to keep the chosen line in view."""
        rows, index = view.menu, view.menu_index
        x, y, w, h = self.rect
        row_h = self.px(58)
        title_h = self.px(64)
        px, py = x + self.px(40), y + self.px(40)
        # Wide enough for the longest line with its arrows, up to the image width.
        longest = max(
            d.textlength(label, font=self.font) + (d.textlength(f"◀  {value}  ▶", font=self.font) if value else 0)
            for label, value in rows
        )
        width = min(w - self.px(80), max(self.px(720), round(longest) + self.px(100)))
        visible = max(3, (h - self.px(40) - self.px(110) - title_h - self.px(16)) // row_h)  # above the info bar
        first = 0
        if len(rows) > visible:
            first = max(0, min(len(rows) - visible, index - visible // 2))
        shown_rows = rows[first : first + visible]
        height = title_h + row_h * len(shown_rows) + self.px(16)
        d.rounded_rectangle([px, py, px + width, py + height], radius=self.px(14), fill=(0, 0, 0, 190))
        d.text((px + self.px(24), py + title_h // 2), view.menu_title, font=self.font_bold, fill=WHITE, anchor="lm")
        if len(rows) > visible:  # where we are in the list
            track = row_h * visible
            top = py + title_h
            bar_x = px + width - self.px(8)
            d.rectangle([bar_x, top, bar_x + self.px(3), top + track], fill=(80, 80, 80, 230))  # nearly opaque, like the chosen line
            thumb = track * visible / len(rows)
            ty = top + track * first / len(rows)
            d.rectangle([bar_x, ty, bar_x + self.px(3), ty + thumb], fill=WHITE)
        for i, (label, value) in enumerate(shown_rows, start=first):
            ry = py + title_h + (i - first) * row_h
            if value is None:  # section title
                d.text((px + self.px(24), ry + row_h // 2), label.upper(), font=self.font, fill=AUTO, anchor="lm")
                continue
            chosen = i == index
            if chosen:
                d.rounded_rectangle(
                    [px + self.px(10), ry + self.px(4), px + width - self.px(10), ry + row_h - self.px(4)],
                    radius=self.px(8),
                    # Drawing replaces the pixels, alpha included: a light, transparent amber
                    # would let a bright scene wash the line out. Dark and nearly opaque.
                    fill=(95, 65, 15, 230),
                )
            colour = AMBER if chosen else WHITE
            d.text((px + self.px(28), ry + row_h // 2), label, font=self.font, fill=colour, anchor="lm")
            shown = f"◀  {value}  ▶" if chosen else value
            d.text((px + width - self.px(28), ry + row_h // 2), shown, font=self.font, fill=colour, anchor="rm")

    def _countdown(self, d, seconds):
        """The self-timer's seconds left, big in the middle, outlined to read on any scene."""
        x, y, w, h = self.rect
        d.text(
            (x + w / 2, y + h / 2),
            str(seconds),
            font=self.font_countdown,
            fill=(255, 255, 255, 230),
            anchor="mm",
            stroke_width=self.px(6),
            stroke_fill=(0, 0, 0, 160),
        )

    def _exposure_timer(self, d, timer, label):
        """A long exposure's time left (a bulb's so far), big, over the last image shown."""
        x, y, w, h = self.rect
        cx, cy = x + w / 2, y + h * 0.45
        stroke = {"stroke_width": self.px(5), "stroke_fill": (0, 0, 0, 160)}
        d.text((cx, cy), timer, font=self.font_timer, fill=(255, 255, 255, 235), anchor="mm", **stroke)
        top = cy - self.font_timer.size * 0.75
        d.text((cx, top), label, font=self.font_bold, fill=AMBER, anchor="md", **stroke)

    def _message(self, d, message, warning):
        x, y, w, h = self.rect
        tw = d.textlength(message, font=self.font_message)
        pad = self.px(24)
        bw, bh = tw + 2 * pad, self.font_message.size + 2 * pad
        bx, by = x + (w - bw) / 2, y + h * 0.42 - bh / 2
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=self.px(12), fill=(0, 0, 0, 170))
        d.text((x + w / 2, by + bh / 2), message, font=self.font_message, fill=AMBER if warning else WHITE, anchor="mm")
