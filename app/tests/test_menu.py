import unittest

from pycam.menu import FULL, QUICK, Item, Menu
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
        menu = Menu(Settings(), quick=False)
        menu.move(+1)  # past Applications: metering, centre, its first value
        self.assertEqual(menu.next_value(-1), "centre")
        self.assertEqual(menu.next_value(+10), "matrix")

    def test_lines_that_are_not_settings(self):
        ran = []
        items = (
            Item("Statut", kind="info", text=lambda ctx: f"{ctx} en attente"),
            Item("Lancer", kind="run", target=ran.append, text=lambda ctx: "›"),
            Item("Section"),
        )
        menu = Menu(Settings(), items=items, title="Appli", context=3)
        self.assertEqual(menu.item.label, "Lancer")  # an info line cannot be chosen
        self.assertEqual(menu.rows(), (("Statut", "3 en attente"), ("Lancer", "›"), ("Section", None)))
        menu.move(+1)
        self.assertEqual(menu.item.label, "Lancer")  # the only one

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
