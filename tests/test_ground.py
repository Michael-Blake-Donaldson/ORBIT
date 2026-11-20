import math
import unittest
from dataclasses import replace

from orbit_sim import GroundStation, ecef_to_geodetic, eci_to_ecef, look_angles

# WGS 84 position vectors generated with PROJ EPSG:4979 -> EPSG:4978.
# Position is in km. Angles are geodetic latitude and east longitude.
PROJ_POSITIONS = (
    ((-843.2813352422764, -5324.268807683117, 4495.797518496627), (40, -99, 650)),
    ((-2055.980109628565, 748.3155621481616, -5971.0418865038055), (-70, 160, 0.002)),
    ((-0.11343913602744629, -0.00019798884346587705, 6456.752313255234), (89.999, -179.9, 100)),
    ((39838.30895113929, 10674.642711279339, 8757.730298833936), (12, 15, 35786)),
    ((3146.6587389939955, 2640.360187330626, -4862.559224373497), (-50, 40, -0.3)),
)


class GroundGeometryTests(unittest.TestCase):
    def test_wgs84_inverse_agrees_with_proj_forward_vectors(self):
        for vector, (latitude, longitude, height) in PROJ_POSITIONS:
            with self.subTest(latitude=latitude, height=height):
                observed = ecef_to_geodetic(vector)
                self.assertAlmostEqual(observed.latitude_deg, latitude, places=10)
                self.assertAlmostEqual(observed.longitude_deg, longitude, places=10)
                self.assertAlmostEqual(observed.height_km, height, places=9)

    def test_ground_station_matches_proj_enu_reference(self):
        # PROJ reports ENU (703619.254434, 613098.865025, 586079.902377) metres.
        station = GroundStation(35.2, -106.5, 1.65, 15)
        observed = look_angles(PROJ_POSITIONS[0][0], station)
        self.assertAlmostEqual(observed.azimuth_deg, 48.93272384440727, places=10)
        self.assertAlmostEqual(observed.elevation_deg, 32.128554707352215, places=10)
        self.assertAlmostEqual(observed.slant_range_km, 1102.0253742432162, places=9)
        self.assertTrue(observed.visible)
        self.assertFalse(look_angles(PROJ_POSITIONS[0][0], replace(station, minimum_elevation_deg=35)).visible)

    def test_earth_rotation_turns_stationary_inertial_longitude_westward(self):
        quarter_turn_s = math.pi / (2 * 7.292115e-5)
        rotated = eci_to_ecef((7000, 0, 0), quarter_turn_s, 0)
        point = ecef_to_geodetic(rotated)
        self.assertAlmostEqual(point.longitude_deg, -90, places=12)
        self.assertAlmostEqual(point.latitude_deg, 0, places=12)
        self.assertAlmostEqual(point.height_km, 7000 - 6378.137, places=10)
        shifted = ecef_to_geodetic(eci_to_ecef((7000, 0, 0), 0, -90))
        self.assertAlmostEqual(shifted.longitude_deg, 90, places=12)

    def test_singular_poles_and_zenith_report_blank_angles(self):
        pole = ecef_to_geodetic((0, 0, -7000))
        self.assertEqual(pole.latitude_deg, -90)
        self.assertIsNone(pole.longitude_deg)
        self.assertAlmostEqual(pole.height_km, 7000 - 6356.752314245179, places=9)
        station = GroundStation(0, 0)
        zenith = look_angles((7000, 0, 0), station)
        self.assertIsNone(zenith.azimuth_deg)
        self.assertEqual(zenith.elevation_deg, 90)
        self.assertTrue(zenith.visible)
        opposite = look_angles((-7000, 0, 0), station)
        self.assertFalse(opposite.visible)
        self.assertEqual(opposite.elevation_deg, -90)
        coincident = look_angles((6378.137, 0, 0), station)
        self.assertIsNone(coincident.elevation_deg)
        self.assertFalse(coincident.visible)

    def test_validates_station_rotation_and_position_inputs(self):
        with self.assertRaises(ValueError):
            GroundStation(110, 0)
        with self.assertRaises(ValueError):
            GroundStation(0, 0, minimum_elevation_deg=-1)
        with self.assertRaises(ValueError):
            eci_to_ecef((7000, 0, 0), 0, float("nan"))
        with self.assertRaises(ValueError):
            ecef_to_geodetic((0, 0, 0))
