# ORBIT

ORBIT is an Earth-orbit mission simulator. The Python library propagates
Cartesian trajectories, applies scheduled burns, and streams telemetry.
The Java mission controller runs the Python worker in an isolated interpreter,
enforces a timeout, and publishes the completed CSV.

## Run a mission

From the repository root, install [uv](https://docs.astral.sh/uv/), Python 3.11
or newer, JDK 17 or newer, and Maven 3.9 or newer. The examples below use
PowerShell.

```powershell
uv sync --locked --managed-python --python 3.12
mvn -f controller/pom.xml package
New-Item -ItemType Directory -Force output | Out-Null
java -jar controller/target/orbit-controller-0.1.0.jar .venv/Scripts/python.exe examples/hohmann.json output/hohmann.csv
```

On macOS and Linux, use `.venv/bin/python` and create the output directory with
`mkdir -p output`. The output directory must support hard links, as NTFS, APFS,
and ext4 do. Choose a new output filename for each run. The controller's last
argument optionally sets the timeout in seconds, from 1 to 3600, with a default of 120.

To use the Python engine without the controller:

```powershell
uv run --locked python -m orbit_sim examples/j2_precession.json output/j2.csv
```

| Example | What to look for |
| --- | --- |
| [Hohmann transfer](examples/hohmann.json) | Two prograde burns raise a circular orbit from 7,000 km to 12,000 km geocentric radius. Final mass is about 572.35 kg. |
| [J₂ nodal regression](examples/j2_precession.json) | A 55° inclined orbit regresses by about 4.1° per day. RAAN wraps through 360°. |

## Replay telemetry in JavaFX

From the repository root, launch the optional desktop viewer:

```powershell
mvn -f viewer/pom.xml javafx:run
```

Select **Open CSV…** and choose a telemetry file produced by either CLI.
Switch between the XY, XZ, and YZ projections to inspect the orbit around
Earth. The altitude chart and sample inspector follow playback and scrubbing.
Select a burn in **Mission events** to inspect the post-burn state, then step
back one row to compare the pre-burn state at the same timestamp.

You can also open a file at launch. The Maven plugin resolves relative paths
from the `viewer` directory:

```powershell
mvn -f viewer/pom.xml javafx:run '-Djavafx.args=../output/hohmann.csv'
```

Playback displays recorded samples and connects them with straight segments
in the selected projection. Long trajectories use fewer points in the
plots, while the timeline and inspector retain access to every row.

## Mission plan

Plans use UTF-8 JSON. The required fields are:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `initial_state` | `position_km` and `velocity_km_s` as three-component ECI arrays at elapsed time zero. |
| `duration_s` | End time in seconds, from 0.001 s to 30 days. |
| `sample_interval_s` | Regular telemetry spacing in seconds, from 0.001 s to 30 days. |

Optional fields:

| Field | Meaning |
| --- | --- |
| `gravity` | `point_mass` (default) or `j2`. |
| `impulses` | Up to 1,000 objects with `time_s`, `delta_v_km_s`, and optional `frame` (`ECI` by default or `RTN`). Times must increase strictly and may include both endpoints. |
| `spacecraft` | `initial_mass_kg`, `dry_mass_kg`, and `isp_s`. Providing this object enables propellant accounting for every applied impulse. |
| `integrator` | Optional overrides for `relative_tolerance` (default `1e-11`), `position_tolerance_km` (`1e-8`), `velocity_tolerance_km_s` (`1e-11`), and `max_step_s` (`120`). |

ECI is an Earth-centered inertial frame with a fixed north-pole axis and a
user-defined equatorial x-axis. Specify all inertial vectors in that same
frame. RTN components are radial outward, transverse along `N × R`, and
normal along pre-burn angular momentum `r × v`. A burn changes velocity
instantaneously. It retains position and, when a spacecraft is configured,
updates mass by the ideal rocket equation. A burn that would violate dry
mass fails the mission. [NASA ideal rocket equation](https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/ideal-rocket-equation/)

## Telemetry and model

- The CSV includes elapsed time, row kind, ECI Cartesian state, spherical
  altitude, speed, specific mechanical energy, optional mass, and osculating
  Keplerian elements. Blank fields represent unavailable mass or undefined
  elements, such as RAAN for an equatorial orbit.
- Rows are ordered by elapsed time. A burn emits `pre_burn` and `post_burn`
  at the same timestamp and replaces any regular sample there. At the end
  time, a scheduled burn is followed by a `complete` row at that same time.
  Earth contact terminates the simulation with an `impact` row. Both
  `complete` and `impact` are successful simulation outcomes.
- The model uses WGS 84 nominal Earth constants. J₂ mode applies only the
  leading zonal harmonic and reports energy including its contribution to
  the potential. The elements are instantaneous two-body elements in either
  gravity mode. Surface contact uses a sphere of radius 6,378.137 km.
  [NGA WGS 84](https://earth-info.nga.mil/?action=wgs84&dir=wgs84)
- SciPy's adaptive DOP853 integrator advances the state between impulses. Its
  dense interpolation supplies telemetry and brackets surface contact.
  Tolerances control local integration error. See the tests for measured
  trajectory error on the reference cases. [SciPy DOP853](https://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.DOP853.html)
- This model covers point-mass and J₂ gravity with instantaneous thrust.
  Atmospheric drag, third-body gravity, higher harmonics, Earth orientation
  data, and finite burns are outside its fidelity.

The Java controller launches a caller-selected, trusted Python executable
without a shell. It accepts valid terminal telemetry from a successful worker
before publishing to the requested path. Both CLIs use exclusive, atomic
publication. Plans are capped at 256 KiB and missions at 100,000 telemetry
rows. Exit codes are `0` for a successful outcome, `1` for failure, `2` for
command-line usage errors, and `124` for a Java worker timeout.

## Python API

```python
from orbit_sim import Mission, State, propagate

mission = Mission(
    initial_state=State((7000, 0, 0), (0, 7.546053290107541, 0)),
    duration_s=3600,
    sample_interval_s=30,
    gravity="j2",
)
for sample in propagate(mission):
    if sample.kind in {"complete", "impact"}:
        print(sample.kind, sample.time_s, sample.altitude_km)
```

Propagation is lazy. Code consuming the iterator can receive samples before
an integration or maneuver error. CLI output is staged until propagation
finishes.

## Verify and profile

```powershell
mvn -f controller/pom.xml package
mvn -f viewer/pom.xml test
uv run --locked python -m unittest discover -s tests -v
uv run --locked python tools/profile_mission.py examples/j2_precession.json --runs 7 --profile
```

The tests compare circular states, eccentric-orbit apoapsis, Hohmann transfer,
and ideal propellant use against analytic calculations. The J₂ test compares
fitted RAAN drift to NASA's first-order secular model and checks conservation
of energy and axial angular momentum. The shallow-impact test uses the
analytic Kepler time of the first surface crossing. Java integration tests run
when the controller JAR and Java are available. [NASA trajectories](https://science.nasa.gov/learn/basics-of-space-flight/chapter4-1/)
[NASA nodal precession](https://science.nasa.gov/wp-content/uploads/2023/05/GDC_OrbitPrimer.pdf)

The profiling command reports median runtime and rows per second after a
warm-up. It measures the numerical engine and telemetry construction,
excluding imports, file I/O, and Java process startup.

On the Windows development machine with Python 3.12, the one-day J₂ example
produced 2,881 rows. Across 11 runs, the median dropped from 0.755 s to
0.206 s after specializing three-component cross products and computing
dense interpolation only when needed.
