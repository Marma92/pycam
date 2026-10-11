import unittest

from pycam.menu import FULL, QUICK, Menu
from pycam.settings import Settings


class MenuTest(unittest.TestCase):
    def test_every_line_shows_its_value(self):
        for quick in (True, False):
            rows = Menu(Settings(), quick).rows()
            self.assertNotIn("?", [value for _, value in rows], rows)

    def test_the_quick_menu_is_short_and_in_the_full_one(self):
        self.assertLessEqual(len(QUICK), 12)
        self.assertTrue(all(item in FULL for item in QUICK))

    def test_the_dial_stops_at_the_ends(self):
        menu = Menu(Settings())  # metering: centre, the first value
        self.assertEqual(menu.next_value(-1), "centre")
        self.assertEqual(menu.next_value(+10), "matrix")

    def test_peaking_line(self):
        settings = Settings()
        menu = Menu(settings)
        menu.set("peaking_mode", "green")
        self.assertEqual((settings.peaking, settings.peaking_color), (True, "green"))
        menu.set("peaking_mode", "off")
        self.assertEqual((settings.peaking, settings.peaking_color), (False, "green"))  # the colour is kept
        self.assertNotIn("peaking_mode", settings.to_dict())


if __name__ == "__main__":
    unittest.main()
