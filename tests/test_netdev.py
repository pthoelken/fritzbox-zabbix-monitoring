import unittest
from types import SimpleNamespace

from src import fritzbox_monitor as monitor


class NetdevSpeedTests(unittest.TestCase):
    def collect(self, device):
        lua = SimpleNamespace(
            data_lua=lambda page: {"data": {"active": [{"name": "test", **device}]}}
        )
        return monitor.collect_lua_netdev(lua)

    def test_wlan_speed_and_band_with_fritzos_word_joiners(self):
        for text, speed, band in [
            ("2,4 GHz, 65 / 65 Mbit/s", 65, "2.4 GHz"),
            ("2,4 GHz, 1 / 72 Mbit\u2060/\u2060s", 72, "2.4 GHz"),
            ("5 GHz, 1201 / 866 Mbit\u2060/\u2060s", 1201, "5 GHz"),
        ]:
            with self.subTest(text=text):
                result = self.collect({"properties": [{"txt": text}]})
                self.assertEqual(result.get("fritzbox.netdev[test,speed]"), speed)
                self.assertEqual(result.get("fritzbox.netdev[test,band]"), band)

    def test_lan_speed_unit_conversion_with_fritzos_word_joiners(self):
        for device, speed in [
            ({"port": "LAN 1 mit 1 Gbit/s "}, 1000),
            ({"port": "LAN 2 mit 2,5 Gbit\u2060/\u2060s"}, 2500),
            ({"properties": [{"txt": "100 Mbit\u2060/\u2060s"}]}, 100),
        ]:
            with self.subTest(device=device):
                result = self.collect(device)
                self.assertEqual(result.get("fritzbox.netdev[test,speed]"), speed)


if __name__ == "__main__":
    unittest.main()
