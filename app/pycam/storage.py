"""Where photos go: <dcim>/100PYCAM/PYC_0001.JPG and .DNG (docs/architecture.md §7).

The next number is kept in the settings, so it survives a restart; names already taken
(photos copied back, another tool) are skipped.
"""

import os
import shutil

# Per photo, with a margin: JPEG ~2-3 MB, DNG 24.8 MB, or ~12 MB compressed (measured 2026-10-11).
BYTES_PER_SHOT = {"jpeg": 4 * 1024**2, "dng": 25 * 1024**2, "dng_compressed": 14 * 1024**2}
EXTENSIONS = (".JPG", ".DNG")


def _after(folder, number):
    if number < 9999:
        return folder, number + 1
    return (folder + 1 if folder < 999 else 100), 1


class Storage:
    def __init__(self, settings):
        self.settings = settings

    @property
    def root(self):
        return os.path.expanduser(self.settings.dcim)

    def next_photo(self):
        """(base path without extension, folder, number) of the next free name."""
        folder, number = self.settings.folder_number, self.settings.next_number
        for _ in range(10000):
            base = os.path.join(self.root, f"{folder}PYCAM", f"PYC_{number:04d}")
            if not any(os.path.exists(base + ext) for ext in EXTENSIONS):
                return base, folder, number
            folder, number = _after(folder, number)
        raise RuntimeError(f"no free photo name in {self.root}")

    @staticmethod
    def base_copy(base):
        """Where the JPEG without the look goes: PYB_0001 next to PYC_0001."""
        folder, name = os.path.split(base)
        return os.path.join(folder, "PYB_" + name[4:])

    def commit(self, folder, number):
        """The photo (folder, number) was taken: the next one comes after."""
        self.settings.folder_number, self.settings.next_number = _after(folder, number)

    def shots_remaining(self, formats, dng_compressed=False, base_jpeg=False):
        path = self.root
        while not os.path.exists(path):  # the card may not have a DCIM folder yet
            parent = os.path.dirname(path)
            if parent == path:
                return 0
            path = parent
        kinds = ["dng_compressed" if f == "dng" and dng_compressed else f for f in formats]
        if base_jpeg and "jpeg" in formats:
            kinds.append("jpeg")
        per_shot = sum(BYTES_PER_SHOT[k] for k in kinds) or BYTES_PER_SHOT["jpeg"]
        return shutil.disk_usage(path).free // per_shot
