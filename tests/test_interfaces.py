import csv
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orbit_sim import load_mission

ROOT = Path(__file__).resolve().parents[1]
JAR = ROOT / "controller" / "target" / "orbit-controller-0.1.0.jar"
JAVA = shutil.which("java")

BASE_PLAN = {
    "schema_version": 1,
    "initial_state": {"position_km": [7000, 0, 0], "velocity_km_s": [0, 7.546053290107541, 0]},
    "duration_s": 30,
    "sample_interval_s": 10,
}


class FileTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orbit test & ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.plan = self.directory / "mission & test.json"
        self.output = self.directory / "telemetry & test.csv"
        self.write_plan(BASE_PLAN)

    def write_plan(self, values):
        self.plan.write_text(json.dumps(values), encoding="utf-8")

    def python_command(self):
        return [sys.executable, "-I", "-m", "orbit_sim", str(self.plan), str(self.output)]

    def java_command(self, python=None, timeout="30"):
        return [JAVA, "-jar", str(JAR), python or sys.executable, str(self.plan), str(self.output), timeout]

    def launch(self, command):
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=45, cwd=self.directory)

    def rows(self):
        with self.output.open(encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file))


class PlanTests(FileTestCase):
    def test_rejects_unknown_versions_unknown_fields_and_invalid_numbers(self):
        cases = (
            {"schema_version": True}, {"schema_version": 2}, {"executable": "unexpected"},
            {"duration_s": float("nan")}, {"duration_s": True},
            {"sample_interval_s": 0.001, "duration_s": 3600},
            {"integrator": {"max_step_s": 0.25}, "duration_s": 86400},
            {"initial_state": {"position_km": [6378, 0, 0], "velocity_km_s": [0, 1, 0]}},
            {"impulses": [{"time_s": 10, "delta_v_km_s": [0, 0, 0]}] * 2},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.write_plan(BASE_PLAN | changes)
                with self.assertRaises(ValueError):
                    load_mission(self.plan)

    def test_rejects_duplicate_properties_oversize_and_excessive_nesting(self):
        cases = (
            '{"schema_version":1,"schema_version":1}',
            " " * (256 * 1024 + 1),
            "[" * 2000 + "]" * 2000,
        )
        for raw in cases:
            self.plan.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_mission(self.plan)


class PythonCliTests(FileTestCase):
    def test_csv_contract_and_existing_output(self):
        result = self.launch(self.python_command())
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.rows()
        self.assertEqual([float(row["time_s"]) for row in rows], [0, 10, 20, 30])
        self.assertEqual([row["kind"] for row in rows], ["sample"] * 3 + ["complete"])
        self.assertEqual(rows[-1]["mass_kg"], "")
        self.assertEqual(rows[-1]["raan_deg"], "")
        self.assertTrue(math.isfinite(float(rows[-1]["specific_energy_km2_s2"])))
        before = self.output.read_bytes()
        result = self.launch(self.python_command())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.output.read_bytes(), before)

    def test_failed_late_burn_does_not_publish_partial_csv(self):
        self.write_plan(BASE_PLAN | {
            "spacecraft": {"initial_mass_kg": 100, "dry_mass_kg": 99, "isp_s": 300},
            "impulses": [{"time_s": 20, "delta_v_km_s": [0, 1, 0]}],
        })
        result = self.launch(self.python_command())
        self.assertEqual(result.returncode, 1)
        self.assertIn("below dry mass", result.stderr)
        self.assertEqual(list(self.directory.iterdir()), [self.plan])


@unittest.skipUnless(JAVA and JAR.is_file(), "build the Java controller and install Java to run integration tests")
class ControllerTests(FileTestCase):
    def test_concurrent_runs_publish_exactly_one_complete_output(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.launch, [self.java_command(), self.java_command()]))
        self.assertEqual(sorted(result.returncode for result in results), [0, 1], results)
        self.assertEqual(self.rows()[-1]["kind"], "complete")
        self.assertEqual(set(self.directory.iterdir()), {self.plan, self.output})

    def test_impact_is_a_successful_terminal_outcome(self):
        self.write_plan(BASE_PLAN | {
            "initial_state": {"position_km": [6400, 0, 0], "velocity_km_s": [-1, 0, 0]},
        })
        result = self.launch(self.java_command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ORBIT impact", result.stdout)
        self.assertEqual(self.rows()[-1]["kind"], "impact")

    def test_invalid_plan_is_not_published(self):
        self.write_plan(BASE_PLAN | {"schema_version": 99})
        result = self.launch(self.java_command())
        self.assertEqual(result.returncode, 1)
        self.assertIn("simulation worker exited", result.stderr)
        self.assertEqual(list(self.directory.iterdir()), [self.plan])

    def test_timeout_discards_worker_output(self):
        builder = venv.EnvBuilder(with_pip=False)
        environment = self.directory / "worker-venv"
        builder.create(environment)
        python = builder.ensure_directories(environment).env_exe
        location = subprocess.check_output(
            [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            encoding="utf-8",
        ).strip()
        Path(location, "orbit_sim.py").write_text(
            "import sys, time\n"
            "from pathlib import Path\n"
            "Path(sys.argv[-1]).write_text('time_s,kind,x_km\\n0,complete,0\\n')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        result = self.launch(self.java_command(python=python, timeout="1"))
        self.assertEqual(result.returncode, 124, result)
        self.assertFalse(self.output.exists())
        self.assertEqual(set(self.directory.iterdir()), {self.plan, environment})
