import math
import unittest
from dataclasses import replace

from orbit_sim import (
    GroundStation,
    KeplerianOrbit,
    Mission,
    State,
    access_windows,
    propagate,
    state_from_keplerian,
)
from orbit_sim.intervals import _append_interval, _positive_intervals

_RADIUS_KM = 7000
_EARTH_RADIUS_KM = 6378.137
_RELATIVE_RATE = math.sqrt(398600.4418 / _RADIUS_KM**3) - 7.292115e-5


def half_pass_duration(elevation_deg):
    mask = math.radians(elevation_deg)
    central_angle = math.acos(_EARTH_RADIUS_KM / _RADIUS_KM * math.cos(mask)) - mask
    return central_angle / _RELATIVE_RATE


class AccessTests(unittest.TestCase):
    def test_endpoint_contact_does_not_hide_an_interior_crossing(self):
        # The mask is met exactly at one endpoint, then missed until a later crossing.
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                def margin(time, reverse=reverse):
                    offset = 1 - time if reverse else time
                    return offset * (offset - 0.5)

                windows = []
                for window in _positive_intervals(margin, 0, 1, margin(0), margin(1)):
                    _append_interval(windows, window)
                (window,) = windows
                self.assertAlmostEqual(window.start_s, 0 if reverse else 0.5, places=7)
                self.assertAlmostEqual(window.end_s, 0.5 if reverse else 1, places=7)

    def test_equatorial_pass_times_match_closed_form_with_boundary_clipping(self):
        period = math.tau / _RELATIVE_RATE
        mission = Mission(state_from_keplerian(KeplerianOrbit(_RADIUS_KM, 0)), period, period)
        station = GroundStation(0, 0, minimum_elevation_deg=15)
        windows = access_windows(mission, station, 0)
        self.assertEqual(len(windows), 2)
        half = half_pass_duration(15)
        first, second = windows
        self.assertEqual(first.start_s, 0)
        self.assertAlmostEqual(first.end_s, half, places=5)
        self.assertAlmostEqual(second.start_s, period - half, places=5)
        self.assertEqual(second.end_s, period)
        self.assertEqual(windows, access_windows(replace(mission, sample_interval_s=10), station, 0))

    def test_detects_window_shorter_than_a_solver_step_and_telemetry_interval(self):
        center_s = 315
        state = state_from_keplerian(
            KeplerianOrbit(_RADIUS_KM, 0, true_anomaly_deg=-math.degrees(center_s * _RELATIVE_RATE)),
        )
        mission = Mission(state, 800, 800)
        station = GroundStation(0, 0, minimum_elevation_deg=89.5)
        (window,) = access_windows(mission, station, 0)
        half = half_pass_duration(89.5)
        self.assertAlmostEqual(window.start_s, center_s - half, places=5)
        self.assertAlmostEqual(window.end_s, center_s + half, places=5)
        self.assertLess(window.duration_s, 2)

    def test_clips_analysis_at_impact_and_omits_no_pass_scenarios(self):
        radial = Mission(State((6400, 0, 0), (-1, 0, 0)), 100, 100)
        impact = list(propagate(radial))[-1]
        (interval,) = access_windows(radial, GroundStation(0, 0, height_km=-0.1), 0)
        self.assertEqual(interval.start_s, 0)
        self.assertAlmostEqual(interval.end_s, impact.time_s, places=5)
        self.assertEqual(access_windows(radial, GroundStation(0, 180), 0), ())
