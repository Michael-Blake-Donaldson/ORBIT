import math
import unittest
from pathlib import Path

import numpy as np

from orbit_sim import (
    Impulse,
    Integrator,
    Mission,
    SimulationError,
    Spacecraft,
    State,
    load_mission,
    orbital_elements,
    propagate,
)
from orbit_sim.mission import EARTH_J2
from orbit_sim.mission import EARTH_MU_KM3_S2 as MU
from orbit_sim.mission import EARTH_RADIUS_KM as RADIUS

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def circular_state(radius=7000.0, inclination_deg=0.0):
    inclination = math.radians(inclination_deg)
    speed = math.sqrt(MU / radius)
    return State((radius, 0, 0), (0, speed * math.cos(inclination), speed * math.sin(inclination)))


class PhysicsTests(unittest.TestCase):
    def test_circular_orbit_matches_analytic_positions_and_velocity(self):
        radius = 7000.0
        period = math.tau * math.sqrt(radius**3 / MU)
        samples = list(propagate(Mission(circular_state(radius), period, period / 4)))
        self.assertEqual(len(samples), 5)
        for index, sample in enumerate(samples):
            angle = index * math.pi / 2
            speed = math.sqrt(MU / radius)
            np.testing.assert_allclose(
                sample.state.position_km, [radius * math.cos(angle), radius * math.sin(angle), 0],
                rtol=0, atol=4e-7,
            )
            np.testing.assert_allclose(
                sample.state.velocity_km_s, [-speed * math.sin(angle), speed * math.cos(angle), 0],
                rtol=0, atol=5e-10,
            )
            self.assertAlmostEqual(sample.specific_energy_km2_s2, -MU / (2 * radius), places=9)
        self.assertIsNone(samples[0].elements.raan_deg)
        self.assertIsNone(samples[0].elements.true_anomaly_deg)

    def test_inclined_eccentric_orbit_reaches_analytic_apogee_and_converges(self):
        periapsis, apoapsis = 7000.0, 45_000.0
        axis = (periapsis + apoapsis) / 2
        inclination = math.radians(45)
        speed = math.sqrt(MU * (2 / periapsis - 1 / axis))
        state = State((periapsis, 0, 0), (0, speed * math.cos(inclination), speed * math.sin(inclination)))
        duration = math.pi * math.sqrt(axis**3 / MU)
        loose = Integrator(1e-6, 1e-2, 1e-5, 120)
        tight = Integrator(1e-13, 1e-11, 1e-14, 30)
        errors = []
        for integrator in (loose, tight):
            _, end = propagate(Mission(state, duration, duration, integrator=integrator))
            errors.append(math.dist(end.state.position_km, (-apoapsis, 0, 0)))
        self.assertLess(errors[1], 1e-6)
        self.assertLess(errors[1], errors[0])

    def test_hohmann_transfer_and_mass_match_closed_form(self):
        mission = load_mission(EXAMPLES / "hohmann.json")
        a, b = 7000, 12000
        transfer_axis = (a + b) / 2
        t = math.pi * math.sqrt(transfer_axis**3 / MU)
        dv1 = math.sqrt(MU / a) * (math.sqrt(2 * b / (a + b)) - 1)
        dv2 = math.sqrt(MU / b) * (1 - math.sqrt(2 * a / (a + b)))
        samples = list(propagate(mission))
        first, second = [sample for sample in samples if sample.kind == "post_burn"]
        self.assertAlmostEqual(first.elements.semi_major_axis_km, transfer_axis, places=7)
        self.assertAlmostEqual(second.time_s, t, places=9)
        self.assertAlmostEqual(second.state.position_km[0], -b, places=6)
        self.assertAlmostEqual(second.speed_km_s, math.sqrt(MU / b), places=9)
        self.assertLess(samples[-1].elements.eccentricity, 1e-9)
        reference_mass = 1000 * math.exp(-(dv1 + dv2) / (0.00980665 * 320))
        self.assertAlmostEqual(samples[-1].mass_kg, reference_mass, places=8)

    def test_j2_secular_nodal_regression_and_invariants(self):
        mission = load_mission(EXAMPLES / "j2_precession.json")
        samples = list(propagate(mission))
        time = np.array([sample.time_s for sample in samples])
        raan = np.unwrap(np.radians([sample.elements.raan_deg for sample in samples]))
        observed, _ = np.polyfit(time, raan, 1)
        a = 7000
        expected = -1.5 * EARTH_J2 * math.sqrt(MU / a**3) * (RADIUS / a)**2 * math.cos(math.radians(55))
        self.assertLess(abs(observed / expected - 1), 0.01)
        energies = [sample.specific_energy_km2_s2 for sample in samples]
        axial_momentum = [
            sample.state.position_km[0] * sample.state.velocity_km_s[1]
            - sample.state.position_km[1] * sample.state.velocity_km_s[0]
            for sample in samples
        ]
        self.assertLess(max(energies) - min(energies), 1e-8)
        self.assertLess(max(axial_momentum) - min(axial_momentum), 1e-5)

    def test_shallow_impact_and_first_crossing_from_elliptic_reference(self):
        apoapsis = 7400
        periapsis = RADIUS - 0.00005
        a = (periapsis + apoapsis) / 2
        e = (apoapsis - periapsis) / (apoapsis + periapsis)
        state = State((apoapsis, 0, 0), (0, math.sqrt(MU * (2 / apoapsis - 1 / a)), 0))
        anomaly = math.tau - math.acos((1 - RADIUS / a) / e)
        expected = (anomaly - e * math.sin(anomaly) - math.pi) * math.sqrt(a**3 / MU)
        last = list(propagate(Mission(state, 4000, 600)))[-1]
        self.assertEqual(last.kind, "impact")
        self.assertAlmostEqual(last.altitude_km, 0, places=7)
        self.assertLess(abs(last.time_s - expected), 0.01)

    def test_fast_radial_impact_stops_before_pending_maneuver(self):
        state = State((6400, 0, 0), (-1000, 0, 0))
        mission = Mission(state, 100, 1, impulses=(Impulse(1, (0, 1, 0)),))
        start, hit = propagate(mission)
        self.assertEqual((start.kind, hit.kind), ("sample", "impact"))
        self.assertAlmostEqual(hit.altitude_km, 0.0, places=6)
        self.assertAlmostEqual(hit.time_s, (6400 - RADIUS) / 1000, places=7)
        self.assertIsNone(hit.elements.inclination_deg)

    def test_burn_boundaries_and_rtn_rotation(self):
        mission = Mission(
            circular_state(inclination_deg=90), 0.5, 0.1,
            impulses=(Impulse(0, (1, 2, 3), "RTN"), Impulse(0.3, (0, 0, 0)), Impulse(0.5, (0, 0, 0))),
        )
        samples = list(propagate(mission))
        self.assertEqual(
            [(sample.time_s, sample.kind) for sample in samples],
            [(0, "pre_burn"), (0, "post_burn"), (0.1, "sample"), (0.2, "sample"),
             (0.3, "pre_burn"), (0.3, "post_burn"), (0.4, "sample"),
             (0.5, "pre_burn"), (0.5, "post_burn"), (0.5, "complete")],
        )
        pre, post = samples[:2]
        self.assertEqual(pre.state.position_km, post.state.position_km)
        delta = np.array(post.state.velocity_km_s) - pre.state.velocity_km_s
        np.testing.assert_allclose(delta, (1, -3, 2), rtol=0, atol=1e-12)

    def test_impossible_burns_are_rejected(self):
        cases = (
            Mission(State((7000, 0, 0), (0, 0, 0)), 10, 5, impulses=(Impulse(0, (0, 1, 0), "RTN"),)),
            Mission(circular_state(), 10, 5, impulses=(Impulse(0, (0, 3, 0)),), spacecraft=Spacecraft(100, 90, 300)),
        )
        for mission in cases:
            with self.subTest(mission=mission), self.assertRaises(SimulationError):
                list(propagate(mission))

    def test_parabolic_and_hyperbolic_elements(self):
        escape_speed = math.sqrt(2 * MU / 7000)
        parabolic = orbital_elements(State((7000, 0, 0), (0, escape_speed, 0)))
        self.assertIsNone(parabolic.semi_major_axis_km)
        self.assertAlmostEqual(parabolic.eccentricity, 1)
        escaping = Mission(State((7000, 0, 0), (0, escape_speed * 1.1, 0)), 20000, 20000)
        _, end = propagate(escaping)
        self.assertLess(end.elements.semi_major_axis_km, 0)
        self.assertGreater(end.elements.eccentricity, 1)
        self.assertGreater(end.altitude_km, 100_000)

    def test_retrograde_elements_recover_angles_in_the_correct_quadrants(self):
        def rotation_z(angle):
            c, s = math.cos(angle), math.sin(angle)
            return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])

        axis, eccentricity = 12_000, 0.25
        raan, inclination, periapsis, anomaly = np.radians([245, 130, 300, 225])
        c, s = math.cos(inclination), math.sin(inclination)
        rotation_x = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        rotation = rotation_z(raan) @ rotation_x @ rotation_z(periapsis)
        p = axis * (1 - eccentricity**2)
        c, s = math.cos(anomaly), math.sin(anomaly)
        position = rotation @ (p / (1 + eccentricity * c) * np.array([c, s, 0]))
        velocity = rotation @ (math.sqrt(MU / p) * np.array([-s, eccentricity + c, 0]))
        elements = orbital_elements(State(tuple(map(float, position)), tuple(map(float, velocity))))
        self.assertAlmostEqual(elements.semi_major_axis_km, axis, places=8)
        self.assertAlmostEqual(elements.eccentricity, eccentricity, places=10)
        np.testing.assert_allclose(
            [elements.raan_deg, elements.inclination_deg,
             elements.argument_of_periapsis_deg, elements.true_anomaly_deg],
            [245, 130, 300, 225], rtol=0, atol=1e-9,
        )
