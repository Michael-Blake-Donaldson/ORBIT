# ORBIT

ORBIT is an Earth-orbit mission simulator. The Python library propagates
Cartesian trajectories, applies scheduled burns, and streams telemetry.
The Java mission controller runs the Python worker in an isolated interpreter,
enforces a timeout, and publishes the completed CSV.

## Open the browser mission workspace

```powershell
uv run --locked --extra web orbit-web
```

Open [the loopback workspace](http://127.0.0.1:8765/). It starts by
running the Hohmann example. Import a version 1 mission JSON or choose
another example. Edit a Cartesian or Keplerian initial orbit, elapsed
duration, sampling cadence, gravity, propellant settings, and impulses
or finite burns. Each initial-orbit representation maintains its own
draft. Imported advanced settings remain in the plan. Use **Validate**
to check the plan with the same Python validator used by the CLI.

**Run simulation** updates the flight summary, ECI trajectory, and
telemetry plots. Pick a maneuver from the event menu or step one row
at a time to inspect coincident before- and after-burn states. **Save
JSON** downloads the current plan, and **Export telemetry** downloads
the last completed run in canonical ORBIT CSV format. Editing a plan
marks its prior result accordingly.

This preview listens on loopback only. The web adapter caps plans
at 256 KiB and results at 4,000 rows, allows two simulations in flight,
and sets a 15-second worker deadline. Use `--port` to choose another
local port. If the project directory is synced and its existing virtual
environment is locked, set `UV_PROJECT_ENVIRONMENT` to a dedicated
directory outside the synced tree.

### Run the browser workspace with queued missions

Build the controller and start its private loopback service. Generate a
unique `ORBIT_JOB_TOKEN` of at least 32 URL-safe characters, and supply the
same token through the environment of both processes. Keep the token out
of the command line and source control.

```powershell
mvn -f controller/pom.xml package
uv sync --locked --extra web
$orbitPython = (uv run --locked --extra web python -c "import sys; print(sys.executable)").Trim()
$orbitSpool = Join-Path $env:LOCALAPPDATA 'ORBIT/MissionJobSpool'
New-Item -ItemType Directory -Force $orbitSpool | Out-Null
java -jar controller/target/orbit-controller-0.1.0.jar --serve $orbitPython 8766 $orbitSpool
```

In a second terminal with the same token in its environment, run:

```powershell
uv run --locked --extra web orbit-web --job-port 8766
```

The browser now submits a job explicitly when you select **Run
simulation**. It displays queued, running, and final states and offers
**Cancel run** until the job finishes. Completed jobs return the same
canonical CSV as direct runs. Submissions that repeat the same
idempotency key and exact plan reuse the existing job. Reusing that key
with a different plan returns a conflict.
If a response is lost, retry from the same browser tab with the same
plan to resume monitoring or retrieve the original result.

The Java process runs at most two simulations and queues eight more.
Each owner can have up to four queued or running jobs and 16 retained
records. Repeating a submission key does not consume another slot.
It retains up to 64 job records in `orbit-job-store-v1` under the spool
directory. Use a service-owned directory on a local filesystem that
supports exclusive locks and atomic renames. Keep it outside web asset
directories and file-sync tools. Only one controller can open the store
at a time.

Completed results and idempotency keys survive a controller restart.
Interrupted jobs become failed, and a recorded cancellation becomes
cancelled. The controller checks completed telemetry against its stored
SHA-256 digest when recovering a job and when serving an export.
Terminal records and artifacts expire after 30 minutes.
The internal API scopes status, cancellation, telemetry, and
idempotency to the owner supplied by the authenticated gateway. The
loopback browser preview supplies a single fixed owner.

### Authenticate the workspace behind an AWS load balancer

Hosted mode expects a commercial AWS Application Load Balancer (ALB)
with an HTTPS listener and an authenticate-Cognito or authenticate-OIDC
action before it forwards traffic. Register the public callback
`https://your-domain/oauth2/idpresponse` in the identity provider.
Set these environment variables in the web process from the actual
listener and identity provider.

| Setting | Value |
| --- | --- |
| `ORBIT_PUBLIC_ORIGIN` | The canonical browser origin, such as `https://orbit.example.com`. |
| `ORBIT_ALB_ARN` | The ARN of the ALB signing user claims. |
| `ORBIT_OIDC_ISSUER` | The exact HTTPS issuer configured on the listener. |
| `ORBIT_OIDC_CLIENT_ID` | The Cognito app client or equivalent OIDC client ID. |
| `ORBIT_JOB_TOKEN` | The existing private credential shared with the Java controller. |

```powershell
uv run --locked --extra web --extra hosted orbit-web --job-port 8766 --auth-alb
```

The workspace remains bound to loopback. Put an HTTPS reverse proxy on
the same host, restrict its inbound security group to the ALB, and
preserve the public Host header when forwarding to ORBIT. Configure
the ALB target group with HTTPS and use `GET /healthz` to check web
liveness. Allow outbound HTTPS from the web process to the regional
AWS public-key endpoint for signing key rotation.

ORBIT verifies the signature, signer ARN, issuer, client, expiry, and
subject in the ALB-signed claims before accepting a request. The Java
controller receives a stable hash derived from the signed subject.
Unsigned identity headers and caller-supplied job owners are ignored.
Browser submissions and cancellations require the configured HTTPS
origin. [AWS ALB authentication and networking requirements](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html)

### Package the web deployment with Docker

The [deployment guide](deploy/README.md) covers a loopback preview and
an HTTPS target behind an authenticated AWS load balancer. Both use
the same image and persist completed jobs in a dedicated volume.

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
| [Finite prograde burn](examples/finite_burn.json) | A 1,500 N transverse burn from 600 to 720 s raises the orbit and consumes about 57.36 kg. |
| [Orbital decay](examples/orbital_decay.json) | The configured drag model lowers the semi-major axis of a 400 km orbit by about 1.13 km in one day. |
| [Tracked ellipse](examples/apsides.json) | Periapsis and apoapsis appear as event rows between 300 s cadence samples. |
| [Lunar perturbation](examples/lunar_perturbation.json) | The Moon shifts the final position by about 12.14 km versus the same one-day high-orbit case with Earth gravity alone. |
| [Solar eclipses](examples/solar_eclipses.json) | Two orbits through Earth's shadow, using a DE441 Sun ephemeris. |

## Replay telemetry in JavaFX

From the repository root, launch the optional desktop viewer:

```powershell
mvn -f viewer/pom.xml javafx:run
```

Select **Open CSV…** and choose a telemetry file produced by either CLI.
Switch between the XY, XZ, and YZ projections to inspect the orbit around
Earth. The altitude chart and sample inspector follow playback and scrubbing.
Select an impulse in **Mission events** to inspect the post-burn state, then
step back one row to compare the pre-burn state at the same timestamp.
Finite burns show separate ignition and cutoff markers.
When enabled, coast-phase periapsis and apoapsis also appear in **Mission events**.

You can also open a file at launch. The Maven plugin resolves relative paths
from the `viewer` directory:

```powershell
mvn -f viewer/pom.xml javafx:run '-Djavafx.args=../output/hohmann.csv'
```

Playback displays recorded samples and connects them with straight segments
in the selected projection. Long trajectories use fewer points in the
plots, while the timeline and inspector retain access to every row.

## Export a ground track and station access

The ground-track command takes the eastward angle of Greenwich from the
mission's inertial x-axis at elapsed time zero. It rotates Earth about the
shared north axis at the WGS 84 nominal rate. Use an angle that matches your
initial state and epoch. The examples below use zero to place the J₂
spacecraft over longitude zero at the start. [NGA WGS 84 reference](https://earth-info.nga.mil/?action=wgs84&dir=wgs84)

```powershell
uv run --locked python -m orbit_sim.groundtrack examples/j2_precession.json output/j2-ground-track.csv --greenwich-angle-deg 0 --station 0 0 --min-elevation-deg 10
uv run --locked python -m orbit_sim.groundtrack examples/j2_precession.json output/j2-ground-track.geojson --greenwich-angle-deg 0 --station 0 0 --format geojson
uv run --locked python -m orbit_sim.groundtrack examples/j2_precession.json output/j2-station-access.csv --greenwich-angle-deg 0 --station 0 0 --min-elevation-deg 10 --format access
```

- **CSV** includes WGS 84 geodetic latitude, east longitude, and ellipsoidal
  height at each telemetry time. With a station configured, it also includes
  azimuth clockwise from true north, elevation, slant range, and a
  visibility flag. Undefined angles are blank. [ESA topocentric coordinate reference](https://gssc.esa.int/navipedia/index.php/Transformations_between_ECEF_and_ENU_coordinates)
- **GeoJSON** includes the sampled subpoint track and an optional station
  marker. It cuts lines at the antimeridian and breaks tracks where longitude
  is undefined at a pole. Coordinates are longitude first. [GeoJSON specification](https://www.rfc-editor.org/rfc/rfc7946#section-3.1.9)
- **Access CSV** reports the start, end, and duration of intervals above the
  station elevation mask. The search brackets crossings and checks for
  short passes within dense integrator steps, so access does not depend on
  the telemetry cadence. Windows end at mission completion or surface contact.

Supply station coordinates as geodetic latitude and east longitude in
degrees, with optional ellipsoidal height in kilometres. The fixed-spin
geometry covers mission comparison and planning. Precision tracking requires
Earth orientation data, and radio coverage requires additional terrain,
refraction, and link-budget models.

## Analyze solar eclipses

Use a mission with a Sun ephemeris under `third_bodies`. The eclipse command
locates Earth's shadow boundaries independently of the telemetry cadence.
It reports partial eclipse as `penumbra`, total eclipse as `umbra`, and
annular eclipse as `antumbra`. The rows contain elapsed start, end, and
duration in seconds. [NASA eclipse geometry](https://ntrs.nasa.gov/api/citations/20250006484/downloads/GEONSMSR3_1.pdf)

```powershell
uv run --locked python -m orbit_sim.eclipses examples/solar_eclipses.json output/solar-eclipse-windows.csv
uv run --locked python -m orbit_sim.eclipses examples/solar_eclipses.json output/solar-visibility.csv --format samples
```

The sample export reports the visible fraction of the Sun's apparent
solid angle, from 0 in total eclipse to 1 in full sunlight. It preserves
the mission's telemetry timestamps and row kinds. The model uses
spherical bodies, geometric vectors, and the IAU nominal solar radius of
695,700 km. The visible fraction measures geometry. Electrical power
also depends on orientation, irradiance, and array properties.
[IAU Resolution B3](https://www.iau.org/common/Uploaded%20files/IAUGA2015-Resolution-B3-recommended-nominal-conversion.pdf)

The example uses one-hour geometric Sun vectors from JPL Horizons target
`10` relative to Earth's center, in ICRF, starting January 1, 2025 at
00:00 TDB. Import additional epochs with the same workflow as the lunar
example. `--sun-name` selects a differently named ephemeris.

## Estimate solar generation and battery charge

```powershell
uv run --locked python -m orbit_sim.power_budget examples/solar_eclipses.json output/solar-power-budget.csv
```

The optional `power_system` block specifies array output at 1 AU
(`array_power_w_at_1_au`), initial bus demand (`load_w`),
`battery_capacity_wh`, and `initial_charge_wh`. Optional
`charge_efficiency` and `discharge_efficiency` default to 1. The array
tracks the Sun and uses the inverse-square distance law. During partial
eclipse, it weights the visible part of the Sun by the incidence angle
of its rays. [NASA spacecraft power reference](https://www.nasa.gov/smallsat-institute/sst-soa/power-subsystems/)

Add `load_changes` with ordered `time_s` and `load_w` entries
to model operating modes. Each change takes effect at its timestamp
and is emitted as a CSV row even between solar observations.
`max_charge_w` and `max_discharge_w` optionally cap battery bus
power before charge and discharge efficiency are applied.

The power analysis samples the dense trajectory independently of the
flight telemetry interval. It includes exact eclipse boundaries and
uses at most one-second spacing in penumbra. Between observations, it
integrates linearly interpolated power. `analysis_interval_s` defaults
to 60 seconds. Reduce it for convergence checks. The CSV reports
instantaneous power, stored charge, and cumulative generated, demanded,
curtailed, unserved, and lost energy. The model uses constant conversion
efficiencies for the battery.

## Compare two trajectories

```powershell
uv run --locked python -m orbit_sim.compare examples/lunar_baseline.json examples/lunar_perturbation.json output/lunar-comparison.csv
```

Both plans must use the same inertial axes, duration, and time-zero epoch.
The command resamples each trajectory on the same grid and reports
candidate minus reference in inertial axes and in the reference orbit's
radial, transverse, and normal (RTN) directions. Velocity differences
are inertial velocities resolved in those axes. The comparison uses the
state after all maneuvers at a shared timestamp and reports samples
through the common portion of both trajectories. [NASA orbit-frame reference](https://ntrs.nasa.gov/api/citations/20240004259/downloads/GEONSMS_R3_0_NASA-TP-20240004259.pdf)

Use `--interval-s` to change the cadence. By default it uses the coarser
input cadence. The one-day lunar comparison peaks at about 13.20 km
sampled separation and ends at about 12.14 km.

## Propagate initial-state uncertainty

```powershell
uv run --locked python -m orbit_sim.ensemble examples/hohmann.json examples/initial_dispersion.json output/hohmann-dispersion.json
uv run --locked python -m orbit_sim.ensemble examples/hohmann.json examples/initial_dispersion.json output/hohmann-trials.csv --format csv
```

The dispersion configuration has a `covariance_eci` matrix, an integer
`seed`, and a `trials` count up to 256. Use the state order
`x, y, z, vx, vy, vz` in km and km/s. Covariance terms use the products
of their corresponding units. ORBIT validates symmetry and positive
semidefiniteness after scaling into correlation space, then draws
Gaussian perturbations using the configured seed. [NASA predictive covariance reference](https://ntrs.nasa.gov/archive/nasa/casi.ntrs.nasa.gov/20180003439.pdf)

The JSON report includes the number of completed and impacted trials.
Final-state statistics use the trials that reached mission end, with
sample covariance available from two or more completions. The CSV
includes all outcomes and records the actual elapsed time of each
impact. A draw that creates invalid initial conditions fails the analysis
and identifies the trial. Use the locked environment to reproduce a run.
[NumPy random generator documentation](https://numpy.org/doc/stable/reference/random/generator)

## Propagate a state transition matrix

```python
from pathlib import Path
from orbit_sim import load_mission, propagate_state_transition

mission = load_mission(Path("examples/lunar_perturbation.json"))
for row in propagate_state_transition(mission):
    print(row.sample.time_s, row.matrix_eci)
```

Each matrix maps a small perturbation of the initial Cartesian state to a
perturbation at the sample's fixed elapsed time. Both sides use the state
order `x, y, z, vx, vy, vz` in km and km/s. The calculation integrates
the variational equations along ORBIT's dense nominal trajectory for
point-mass Earth gravity, J₂, and prescribed third-body ephemerides.
Fixed inertial impulses keep the matrix continuous across the velocity
jump. RTN impulses differentiate the local orbital frame using the
pre-burn state. Finite thrust follows the nominal mass history and
includes rotation of an RTN thrust vector. The drag gradient includes
atmospheric co-rotation and the exponential density variation. At the
reference altitude it uses the derivative of the constant-density
branch. Vehicle mass and force model parameters are prescribed.
At a surface impact or apsis, the matrix describes the
state at the nominal event time. The derivative of the event time is a
separate quantity.
[NASA state and covariance propagation reference](https://ntrs.nasa.gov/api/citations/20240004259/downloads/GEONSMS_R3_0_NASA-TP-20240004259.pdf)

Call `propagate_covariance(mission, initial_covariance_eci)` to obtain the
linearized covariance at each of those samples. It transports the initial
covariance factor, including rank-deficient inputs, and reconstructs the
covariance as a product with its transpose. This models small initial
errors under the prescribed dynamics with zero process noise.

```powershell
uv run --locked python -m orbit_sim.covariance_timeline examples/hohmann.json examples/initial_dispersion.json output/hohmann-covariance.csv
```

The covariance timeline uses the matrix in the dispersion configuration.
The seed and trial count apply to the Monte Carlo command. The CSV
preserves before- and after-burn samples and reports the lower triangle
in Cartesian state order. Each covariance column uses the product of its
named coordinate units. The 3D RMS columns take the square root of the
sum of the position or velocity variances.

## Export a CCSDS orbit ephemeris

```powershell
uv run --locked python -m orbit_sim.oem examples/lunar_perturbation.json output/lunar.oem --frame ICRF
```

The exporter writes a CCSDS OEM 3.0 in plain-text keyword-value notation.
The plan must specify `epoch_jd_tdb`. The spacecraft states use TDB
calendar timestamps, Earth-centered Cartesian kilometers, and kilometers
per second. Choose `ICRF` or `EME2000` to identify the axes already used
by the mission. The export performs no frame or time-scale conversion
on the trajectory. Optional `--object-name`, `--object-id`, and
`--originator` values populate the message metadata.

An impulsive burn starts a new ephemeris segment with the post-burn
state at the burn epoch. Where the preceding solver step has room for a
distinct time tag, its segment ends with a dense state evaluated about
one nanosecond before the burn. An impulse at mission completion can
produce a final segment containing a single state. Continuous thrust
boundaries remain within a segment. Coverage ends at mission completion
or surface impact.
[CCSDS Orbit Data Messages standard](https://ccsds.org/publications/allpubs/entry/3073/)

## Mission plan

Plans use UTF-8 JSON. The required fields are:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `initial_state` or `initial_orbit` | Exactly one Cartesian or Keplerian description at elapsed time zero. |
| `duration_s` | End time in seconds, from 0.001 s to 30 days. |
| `sample_interval_s` | Regular telemetry spacing in seconds, from 0.001 s to 30 days. |

Optional fields:

| Field | Meaning |
| --- | --- |
| `gravity` | `point_mass` (default) or `j2`. |
| `impulses` | Objects with `time_s`, `delta_v_km_s`, and optional `frame` (`ECI` by default or `RTN`). Times must increase strictly and may include both endpoints. |
| `finite_burns` | Objects with `start_s`, `end_s`, `thrust_n`, and optional `frame` (`ECI` by default or `RTN`). Each burn lasts at least 1 ms. Windows must be ordered, nonoverlapping, and within the mission. |
| `spacecraft` | `initial_mass_kg`, `dry_mass_kg`, and `isp_s`. Required for finite burns and optional for impulsive propellant accounting. |
| `drag` | Optional local atmosphere with `reference_altitude_km`, `reference_density_kg_m3`, `scale_height_km`, `area_m2`, and `coefficient` (default `2.2`). Requires spacecraft mass. |
| `third_bodies` | Up to eight objects with a unique `name`, `mu_km3_s2`, `ephemeris`, and optional `minimum_separation_km` (default `1`). |
| `epoch_jd_tdb` | Optional Julian date in TDB for elapsed time zero. The Horizons importer uses it to align successive imports. |
| `power_system` | Optional solar generation and battery model. Requires a Sun ephemeris under `third_bodies`. |
| `track_apsides` | Boolean, default `false`. Detect distinct radial turning points during coasts and add `periapsis` and `apoapsis` rows. |
| `integrator` | Optional overrides for `relative_tolerance` (default `1e-11`), `position_tolerance_km` (`1e-8`), `velocity_tolerance_km_s` (`1e-11`), and `max_step_s` (`120`). |

`initial_state` uses three-component ECI arrays `position_km` and
`velocity_km_s`. `initial_orbit` uses `periapsis_radius_km` and
`eccentricity`, plus `inclination_deg`, `raan_deg`,
`argument_of_periapsis_deg`, and `true_anomaly_deg`, whose defaults are
zero. Periapsis radius is measured from Earth's center. Elliptic, parabolic,
and hyperbolic initial states use the same elements. For a circular
equatorial orbit, set RAAN and argument of periapsis to zero to measure
true anomaly from the inertial x-axis for a prograde orbit.

ECI is an Earth-centered inertial frame with a fixed north-pole axis and a
user-defined equatorial x-axis. Specify all inertial vectors in that same
frame. RTN components are radial outward, transverse along `N × R`, and
normal along pre-burn angular momentum `r × v`. A burn changes velocity
instantaneously. It retains position and, when a spacecraft is configured,
updates mass by the ideal rocket equation. A burn that would violate dry
mass fails the mission. [NASA ideal rocket equation](https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/ideal-rocket-equation/)

For finite burns, `thrust_n` is a three-component force in newtons. ORBIT
uses its constant magnitude to compute mass flow `|F| / (g₀ Isp)` and
integrates acceleration `F / m(t)` alongside gravity. An RTN burn recomputes
its direction as the spacecraft moves. The full scheduled burn must fit
the available propellant at ignition. If contact occurs during a burn,
impact telemetry reports the mass at contact. The mission supports up to
1,000 maneuvers, and impulses can share a burn endpoint. [NASA astrodynamics modeling reference](https://ntrs.nasa.gov/citations/20220014814)

Atmospheric drag uses a rigidly co-rotating atmosphere and a constant
effective area. Density falls exponentially above the reference altitude
and stays at the reference density below it. The force opposes the
air-relative velocity and uses the spacecraft mass at each integration
time. This local density model is intended for sensitivity studies around
the chosen altitude. Space weather, winds, and reentry aerodynamics require
a higher-fidelity atmosphere. [NASA orbital drag model](https://ilrs.gsfc.nasa.gov/docs/ANDE_AAS_07-265.pdf)

Each third-body ephemeris contains two to 1,024 rows with `time_s`,
`position_km`, and `velocity_km_s`. Use the same Earth-centered inertial
axes as the spacecraft, and cover the mission from zero through its end.
ORBIT uses cubic Hermite interpolation and restarts the integrator at
ephemeris knots. It subtracts the body's acceleration of Earth from its
acceleration of the spacecraft. A force evaluation within
`minimum_separation_km` stops the run with an error. [NASA third-body force formulation](https://ntrs.nasa.gov/api/citations/20240016341/downloads/Nate_Steffen_AAS_Final_Manuscript_with_copyright.pdf)

### Import JPL Horizons vectors

Export a text **Vectors** table from JPL Horizons with these settings:

- Center `500@399` for Earth's center.
- Reference system **ICRF** and reference plane **Frame**.
- Vector correction **None** for geometric states, output units **KM-S**,
  and vector table **2** for position and velocity.
- CSV enabled, time scale **TDB**, and an interval spanning the mission.

Use a starting epoch that matches the spacecraft initial conditions.
The first import stores that epoch in the new plan. Further imports
require the same epoch. [JPL Horizons settings](https://ssd-api.jpl.nasa.gov/doc/horizons.html)

```powershell
uv run --locked python -m orbit_sim.horizons moon-vectors.txt base-mission.json output/with-moon.json --name Moon --mu 4902.8001184575496 --minimum-separation-km 1737.4
```

The importer validates the table and publishes a new plan atomically.
Run the resulting plan with either CLI. The checked-in lunar example
uses Moon target `301`, DE441 Horizons states from January 1 to 2, 2025
TDB, and four-hour spacing. It uses the lunar gravitational parameter
from the JPL DE440 constants file. [JPL gravitational constants](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/pck/gm_de440.tpc)

## Telemetry and model

- The CSV includes elapsed time, row kind, ECI Cartesian state, spherical
  altitude, speed, specific mechanical energy, optional mass, and osculating
  Keplerian elements. Blank fields represent unavailable mass or undefined
  elements, such as RAAN for an equatorial orbit.
- Rows are ordered by elapsed time. A burn emits `pre_burn` and `post_burn`
  at the same timestamp for an impulse. Finite burns emit `burn_start` and
  `burn_end`. Shared timestamps are ordered cutoff, impulse, ignition,
  then mission completion where applicable. Event rows replace a cadence
  row at the same timestamp. Earth contact terminates the simulation with
  an `impact` row. Both `complete` and `impact` are successful simulation outcomes.
- With apsis tracking enabled, the solver locates radial turning points
  between samples. Effectively circular motion and roots at segment
  endpoints are omitted. The telemetry row limit also applies to events.
- The model uses WGS 84 nominal Earth constants. J₂ mode applies only the
  leading zonal harmonic and includes its potential in the energy telemetry.
  Energy includes only Earth potential and inertial kinetic energy. The
  elements are instantaneous two-body elements in either gravity mode.
  Surface contact uses a sphere of radius 6,378.137 km.
  [NGA WGS 84](https://earth-info.nga.mil/?action=wgs84&dir=wgs84)
- SciPy's adaptive DOP853 integrator advances the state between maneuver boundaries. Its
  dense interpolation supplies telemetry and brackets surface contact.
  Tolerances control local integration error. See the tests for measured
  trajectory error on the reference cases. [SciPy DOP853](https://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.DOP853.html)
- The force model includes point-mass and J₂ Earth gravity, optional
  third-body gravity and exponential drag, and impulsive and
  constant-magnitude finite thrust.

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
node --test tests/test_browser_workspace.mjs
uv run --locked python tools/profile_mission.py examples/j2_precession.json --runs 7 --profile
```

The tests compare circular states, eccentric-orbit apoapsis, Hohmann transfer,
and propellant use against analytic calculations. Finite-thrust tests also
check velocity and displacement against the closed-form free-space solution.
Stored JPL CSPICE reference vectors check position and velocity for elliptic,
parabolic, and hyperbolic conics at initialization and after 9,000 s.
[JPL CSPICE conic reference](https://naif.jpl.nasa.gov/pub/naif/toolkit_docs/C/cspice/conics_c.html)

The J₂ test compares
fitted RAAN drift to NASA's first-order secular model and checks conservation
of energy and axial angular momentum. The shallow-impact test uses the
analytic Kepler time of the first surface crossing. The drag test compares
one-day circular decay against the first-order work-energy estimate. Java integration tests run
when the controller JAR and Java are available. [NASA trajectories](https://science.nasa.gov/learn/basics-of-space-flight/chapter4-1/)
[NASA nodal precession](https://science.nasa.gov/wp-content/uploads/2023/05/GDC_OrbitPrimer.pdf)

The lunar regression test compares against a separate propagation using
30-minute Horizons vectors, a position-only cubic spline, direct-minus-indirect
lunar acceleration, and DOP853 with relative tolerance `3e-14`, absolute
tolerance `1e-13`, and a maximum step of 30 seconds. A withheld Horizons
vector also checks the four-hour interpolant between samples.

Ground geometry tests use PROJ WGS 84 Cartesian and topocentric reference
vectors. Station access tests compare timing to a closed-form circular orbit
and cover a sub-two-second window between telemetry rows. [PROJ topocentric conversion](https://proj.org/en/stable/operations/conversions/topocentric.html)

Eclipse tests compare ingress and egress against closed-form common
tangents, exercise a grazing penumbra and annular eclipse, and check
visible solar solid angle against a spherical-lens calculation.
Power tests check inverse-square scaling, separate efficiencies, battery
limits, and conservation of energy at each output row.

The profiling command reports median runtime and rows per second after a
warm-up. It measures the numerical engine and telemetry construction,
excluding imports, file I/O, and Java process startup. Add `--memory` to
measure peak Python-tracked allocations in a separate run.

The [GitHub Actions workflow](.github/workflows/verify.yml) builds both Java
modules and runs the tests on Ubuntu with Python 3.11 and Java 17, and on
Windows with Python 3.12 and Java 25.

On the Windows development machine with Python 3.12, the one-day J₂ example
produced 2,881 rows. Across 11 runs, the median dropped from 0.755 s to
0.206 s after specializing three-component cross products and computing
dense interpolation only when needed.

A separate capacity check streamed 100,000 rows from a 0.12-eccentricity
orbit sampled at 1 s. Propagation took 3.09 s in one run and peaked at
36.9 KiB in Python-tracked allocations in a separate run. The Java
controller published the full CSV, and the viewer read all 100,000 rows.
