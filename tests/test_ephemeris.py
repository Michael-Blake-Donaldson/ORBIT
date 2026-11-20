import math
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

import numpy as np

from orbit_sim import (
    EphemerisPoint,
    Mission,
    SimulationError,
    State,
    ThirdBody,
    load_mission,
    propagate,
)
from orbit_sim.ephemeris import _position_interpolant, _relative_acceleration

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "lunar_perturbation.json"


def stationary_body(radius_km, duration_s=60, *, name="Example body", separation_km=1):
    points = tuple(EphemerisPoint(t, (radius_km, 0, 0), (0, 0, 0)) for t in (0, duration_s))
    return ThirdBody(name, 4902.8, points, separation_km)


class EphemerisTests(unittest.TestCase):
    def test_far_field_residual_matches_high_precision_differential_force(self):
        body = stationary_body(1e8)
        r = np.array([1e-6, 0, 0])
        positions = np.array(body.ephemeris[0].position_km)
        acceleration = _relative_acceleration(r, positions, body)
        with localcontext() as context:
            context.prec = 60
            distance = Decimal("1e8")
            offset = Decimal("1e-6")
            expected = Decimal("4902.8") * (1 / (distance - offset)**2 - 1 / distance**2)
        self.assertTrue(math.isclose(acceleration[0], float(expected), rel_tol=1e-13))
        np.testing.assert_array_equal(acceleration[1:], [0, 0])
        np.testing.assert_array_equal(_relative_acceleration(np.zeros(3), positions, body), [0, 0, 0])

    def test_near_field_matches_direct_and_indirect_terms(self):
        body = stationary_body(380_000)
        positions = np.array(body.ephemeris[0].position_km)
        spacecraft = np.array([365_000, 10_000, -5000])
        delta = positions - spacecraft
        reference = 4902.8 * (delta / np.linalg.norm(delta)**3 - positions / 380_000**3)
        np.testing.assert_allclose(_relative_acceleration(spacecraft, positions, body), reference, rtol=2e-15, atol=0)

    def test_lunar_ephemeris_matches_withheld_vector_and_independent_propagation(self):
        mission = load_mission(EXAMPLE)
        body = mission.third_bodies[0]
        position_at = _position_interpolant(body)
        midway = [158738.2722837145, -304926.9701192979, -165316.8786638905]
        self.assertLess(math.dist(position_at(7200), midway), 0.003)
        for point in body.ephemeris:
            np.testing.assert_allclose(position_at(point.time_s), point.position_km, rtol=0, atol=1e-9)
            np.testing.assert_allclose(position_at(point.time_s, 1), point.velocity_km_s, rtol=0, atol=1e-12)

        # Independent direct-minus-indirect force and 30-minute Horizons samples.
        # See README for the Horizons settings and reference integration tolerances.
        reference_position = [36147.21373624136, 21707.516347041983, 101.4859995237287]
        reference_velocity = [-1.5683562617459323, 2.609707548051826, 0.4278348808100815]
        samples = list(propagate(mission))
        self.assertEqual([sample.time_s for sample in samples], list(range(0, 86_401, 120)))
        final = samples[-1].state
        undisturbed = list(propagate(replace(mission, third_bodies=())))[-1].state
        self.assertLess(math.dist(final.position_km, reference_position), 1e-6)
        self.assertLess(math.dist(final.velocity_km_s, reference_velocity), 1e-10)
        self.assertGreater(math.dist(final.position_km, undisturbed.position_km), 12)

    def test_rejects_unordered_or_incomplete_ephemeris_and_duplicate_bodies(self):
        initial = State((7000, 0, 0), (0, 7.5, 0))
        body = stationary_body(380_000)
        with self.assertRaisesRegex(ValueError, "ordered"):
            replace(body, ephemeris=body.ephemeris[::-1])
        with self.assertRaisesRegex(ValueError, "entire mission"):
            Mission(initial, 61, 10, third_bodies=(body,))
        with self.assertRaisesRegex(ValueError, "unique"):
            Mission(initial, 60, 10, third_bodies=(body, replace(body, name=body.name.upper())))

    def test_force_rejects_close_approach_and_invalid_interpolation(self):
        body = stationary_body(8000, separation_km=5)
        close = State((7996, 0, 0), (0, 7.5, 0))
        with self.assertRaisesRegex(SimulationError, "exclusion radius"):
            list(propagate(Mission(close, 60, 20, third_bodies=(body,))))

        points = (
            EphemerisPoint(0, (1e4, 0, 0), (0, 0, 0)),
            EphemerisPoint(60, (-1e4, 0, 0), (0, 0, 0)),
        )
        invalid = replace(body, ephemeris=points)
        initial = State((0, 7000, 0), (7.5, 0, 0))
        with self.assertRaisesRegex(SimulationError, "intersects Earth"):
            list(propagate(Mission(initial, 60, 20, third_bodies=(invalid,))))
