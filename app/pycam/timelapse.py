"""Timelapse videos: the frames of a timelapse (drive.py) assembled into an MP4
(docs/architecture.md §5).

ffmpeg does it, after the shooting: the JPEGs (look included) cropped to the video's
format, scaled to 1080 lines, smoothed against flicker (its "deflicker" filter: each
frame's brightness brought to the average of its neighbours, which hides the jumps of the
automatic exposure), and encoded in H.264 by the VideoCore's encoder (h264_v4l2m2m), or
by libx264 if it fails. Measured on a Pi 3B: ~6.6 frames a second from 3 Mpx JPEGs, so
~40 s for 250 frames.
"""

import asyncio
import logging
import os
import shutil
import tempfile

log = logging.getLogger(__name__)

ASPECTS = {"16:9": (1920, 1080), "4:3": (1440, 1080)}
DEFLICKER_FRAMES = 9  # the window the brightness is averaged over
HARDWARE_BITRATE = "20M"  # 1080p: generous, the frames hardly move between two
ENCODERS = (
    ["-c:v", "h264_v4l2m2m", "-b:v", HARDWARE_BITRATE],
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],
)


def video_filters(aspect, deflicker):
    width, height = ASPECTS[aspect]
    crop = "crop=iw:iw*9/16" if aspect == "16:9" else "crop=ih*4/3:ih"
    filters = [crop, f"scale={width}:{height}"]
    if deflicker:
        filters.append(f"deflicker=size={DEFLICKER_FRAMES}:mode=pm")
    filters.append("format=yuv420p")
    return ",".join(filters)


def ffmpeg_command(pattern, out, fps, aspect, deflicker, encoder):
    """The command line; `pattern`: numbered frames (frame_%06d.jpg), `out`: written as MP4
    whatever its extension."""
    return [
        "nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-framerate", str(fps), "-start_number", "0", "-i", pattern,
        "-vf", video_filters(aspect, deflicker),
        *encoder,
        "-r", str(fps), "-movflags", "+faststart", "-f", "mp4",
        "-progress", "pipe:1",
        out,
    ]  # fmt: skip


def video_name(first_frame):
    """PYT_0042.MP4 for a timelapse whose first frame is PYC_0042.JPG."""
    return "PYT_" + os.path.basename(first_frame)[4:8] + ".MP4"


async def assemble(frames, out, fps=25, aspect="16:9", deflicker=True, progress=None):
    """Assembles `frames` (JPEG paths, in order) into the MP4 `out`; True if done.
    progress(fraction) as it goes. The file appears complete or not at all."""
    if len(frames) < 2:
        return False
    if shutil.which("ffmpeg") is None:
        log.error("timelapse: no ffmpeg")
        return False
    with tempfile.TemporaryDirectory(prefix="pycam-timelapse-") as tmp:
        # Numbered links: ffmpeg wants a sequence without gaps.
        for i, frame in enumerate(frames):
            os.symlink(os.path.abspath(frame), os.path.join(tmp, f"frame_{i:06d}.jpg"))
        part = out + ".part"
        for encoder in ENCODERS:
            command = ffmpeg_command(os.path.join(tmp, "frame_%06d.jpg"), part, fps, aspect, deflicker, encoder)
            if await _run(command, len(frames), progress):
                _sync(part)
                os.replace(part, out)
                _sync(os.path.dirname(out) or ".")
                return True
            log.warning("timelapse: %s failed, %s", encoder[1], "trying another" if encoder is not ENCODERS[-1] else "giving up")
        try:
            os.remove(part)
        except FileNotFoundError:
            pass
    return False


async def _run(command, total, progress):
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async for line in proc.stdout:
            key, _, value = line.decode(errors="replace").strip().partition("=")
            if key == "frame" and progress and value.isdigit():
                progress(min(1.0, int(value) / total))
        _, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        log.warning("ffmpeg: %s", err.decode(errors="replace").strip()[-500:])
    return proc.returncode == 0


def _sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
