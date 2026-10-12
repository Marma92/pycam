"""Applications: what the camera does besides taking pictures, in MENU › Applications
(docs/architecture.md §5).

An application is an object made with the camera, with:
- title: its name in the Applications menu;
- summary(camera): one line about it, on the right of its name;
- menu(camera): its own menu (menu.Menu with items of any kind);
- run(): a coroutine started with the camera (its background work);
- wake(): new files on the card (a shot, a video), or one of its settings changed;
- hud(): a few characters for the HUD while it works, or "".
"""


def make_apps(camera):
    from .cloudsync import CloudSync

    return [CloudSync(camera)]
