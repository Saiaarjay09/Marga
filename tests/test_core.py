"""Run with:  .venv/bin/python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.physics import SegmentConditions, segment_energy_parts, segment_energy_wh
from core.simulator import simulate_trip
from data.vehicles import VEHICLE_DB

NEXON = VEHICLE_DB["Tata Nexon EV (30 kWh)"]


def straight_route(n=350):
    return [[77.5 + i * 0.01, 13.0 + i * 0.005] for i in range(n)]


def stations(route, down_at=None):
    out = []
    for km in range(20, 340, 40):
        idx = int(km / 350 * len(route))
        lng, lat = route[idx]
        out.append({"ID": f"a{km}", "AddressInfo": {"Title": f"A{km}", "Latitude": lat + 0.01, "Longitude": lng + 0.01},
                    "max_ccs2_power": 60, "is_working": km != down_at})
        out.append({"ID": f"b{km}", "AddressInfo": {"Title": f"B{km}", "Latitude": lat - 0.01, "Longitude": lng - 0.01},
                    "max_ccs2_power": 50, "is_working": True})
    return out


class PhysicsTests(unittest.TestCase):
    def test_parts_sum_to_total(self):
        for elev in (0, 40, -400):
            for style in ("Eco", "Normal", "Aggressive"):
                cond = SegmentConditions(5, 80, elev, 900, 12, 32)
                parts = segment_energy_parts(NEXON, cond, style)
                self.assertAlmostEqual(sum(parts.values()), segment_energy_wh(NEXON, cond, style), places=6)

    def test_headwind_costs_tailwind_helps(self):
        base = SegmentConditions(5, 80, 0, 0, 0, 25)
        head = SegmentConditions(5, 80, 0, 0, 20, 25)
        tail = SegmentConditions(5, 80, 0, 0, -20, 25)
        self.assertGreater(segment_energy_wh(NEXON, head), segment_energy_wh(NEXON, base))
        self.assertLess(segment_energy_wh(NEXON, tail), segment_energy_wh(NEXON, base))

    def test_climbing_costs_descending_returns_energy(self):
        flat = segment_energy_wh(NEXON, SegmentConditions(5, 80, 0, 0, 0, 25))
        up = segment_energy_wh(NEXON, SegmentConditions(5, 80, 60, 0, 0, 25))
        down = segment_energy_wh(NEXON, SegmentConditions(5, 80, -300, 0, 0, 25))
        self.assertGreater(up, flat)
        self.assertLess(down, 0)

    def test_driving_style_ordering(self):
        cond = SegmentConditions(5, 90, 0, 0, 0, 25)
        eco, normal, hard = (segment_energy_wh(NEXON, cond, s) for s in ("Eco", "Normal", "Aggressive"))
        self.assertLess(eco, normal)
        self.assertLess(normal, hard)

    def test_cold_and_hot_cost_more_than_mild(self):
        mild = segment_energy_wh(NEXON, SegmentConditions(5, 80, 0, 0, 0, 22))
        self.assertGreater(segment_energy_wh(NEXON, SegmentConditions(5, 80, 0, 0, 0, 2)), mild)
        self.assertGreater(segment_energy_wh(NEXON, SegmentConditions(5, 80, 0, 0, 0, 42)), mild)


class SimulatorTests(unittest.TestCase):
    def run_trip(self, **kw):
        route = straight_route()
        args = dict(route_geometry=route, vehicle=NEXON, safety_buffer_pct=15, reliability_toggle=True,
                    pre_fetched_chargers=stations(route, kw.pop("down_at", None)), avg_speed_kmh=80,
                    elevation_profile=[800] * len(route), weather_samples=None)
        args.update(kw)
        return simulate_trip(**args)

    def test_plans_stops_with_backups_and_times(self):
        r = self.run_trip()
        self.assertEqual(r["status"], "success")
        self.assertGreater(len(r["stops"]), 0)
        for s in r["stops"]:
            self.assertGreater(s["charge_minutes"], 0)
            self.assertLessEqual(s["arrival_wh"], s["depart_wh"])
        self.assertTrue(any(s["backup_charger"] for s in r["stops"]))
        self.assertGreater(r["arrival_soc_pct"], 0)

    def test_avoids_stations_flagged_down(self):
        for down in range(60, 340, 40):
            r = self.run_trip(down_at=down)
            self.assertTrue(all(s["is_working"] for s in r["stops"]))

    def test_factor_totals_match_trip_energy(self):
        r = self.run_trip()
        self.assertAlmostEqual(sum(r["factor_wh"].values()), r["total_energy_wh"], delta=1.0)

    def test_failed_trip_still_has_complete_stops(self):
        # No chargers at all after the first stretch: forces the "stranded" path.
        route = straight_route()
        few = [c for c in stations(route) if c["ID"] in ("a20", "b20")]
        r = simulate_trip(route, NEXON, 15, True, pre_fetched_chargers=few, avg_speed_kmh=80, start_soc_pct=40)
        self.assertEqual(r["status"], "failed")
        for s in r["stops"]:
            self.assertIn("charge_minutes", s)
        self.assertIn("km_by_point", r)

    def test_lower_start_charge_needs_more_charging(self):
        full = self.run_trip(start_soc_pct=100)
        low = self.run_trip(start_soc_pct=40)
        self.assertGreaterEqual(len(low["stops"]), len(full["stops"]))


if __name__ == "__main__":
    unittest.main()
