import {decimal, events} from "./mission.mjs";

const header = "time_s,reference_kind,candidate_kind,position_difference_km,velocity_difference_km_s," +
  "delta_x_km,delta_y_km,delta_z_km,delta_vx_km_s,delta_vy_km_s,delta_vz_km_s," +
  "delta_r_km,delta_t_km,delta_n_km,delta_vr_km_s,delta_vt_km_s,delta_vn_km_s";
const engineVersions = ["orbit_version", "source_sha256", "python_version", "numpy_version", "scipy_version"];
const hash = /^[0-9a-f]{64}$/;
const version = /^[0-9][A-Za-z0-9._+-]{0,39}$/;

function validEngine(engine) {
  return engineVersions.every(name => typeof engine?.[name] === "string" &&
    (name === "source_sha256" ? hash : version).test(engine[name]));
}

export function parseComparison(data, baselineId, candidateId) {
  const {baseline, candidate, report} = data ?? {};
  if (!baselineId || !candidateId || baselineId === candidateId || baseline?.id !== baselineId || candidate?.id !== candidateId ||
      ![baseline, candidate].every(run => hash.test(run.plan_sha256)) || typeof report?.csv !== "string" ||
      !Number.isFinite(report.interval_s) || report.interval_s <= 0 ||
      !Number.isFinite(report.duration_s) || report.duration_s <= 0 || typeof report.impact_limited !== "boolean" ||
      !validEngine(report.engine) || [baseline, candidate].some(run => run.engine && !validEngine(run.engine))) {
    throw new Error("The comparison response has invalid metadata.");
  }
  const [columns, ...lines] = report.csv.replace(/\r?\n$/, "").split(/\r?\n/);
  if (columns !== header || lines.length !== report.rows || lines.length < 1 || lines.length > 4000) {
    throw new Error("The comparison response has an unexpected CSV format.");
  }
  let previous = -1;
  const rows = lines.map(line => {
    const values = line.split(",");
    const [time, baselineKind, candidateKind, position, velocity] = values;
    if (values.length !== 17 || !Object.hasOwn(events, baselineKind) || !Object.hasOwn(events, candidateKind) ||
        values.some((value, index) => index !== 1 && index !== 2 && !(index >= 11 && value === "") &&
          (!decimal.test(value) || !Number.isFinite(Number(value)))) ||
        Number(time) <= previous || Number(time) < 0 || Number(position) < 0 || Number(velocity) < 0) {
      throw new Error("The comparison response contains an invalid sample.");
    }
    previous = Number(time);
    return {time: previous, baselineKind, candidateKind, positionDifference: Number(position), velocityDifference: Number(velocity)};
  });
  const final = rows.at(-1);
  if (rows[0].time !== 0 || final.time > report.duration_s || (!report.impact_limited && final.time !== report.duration_s)) {
    throw new Error("The comparison response contains an incomplete timeline.");
  }
  return {baseline, candidate, report, rows};
}

export function comparisonCsv({baseline, candidate, report}) {
  const names = ["baseline_run_id", "candidate_run_id", "baseline_plan_sha256", "candidate_plan_sha256",
    "comparison_interval_s", "impact_limited", ...engineVersions.map(name => `comparison_${name}`),
    ...["baseline", "candidate"].flatMap(side => engineVersions.map(name => `${side}_${name}`))];
  const metadata = [baseline.id, candidate.id, baseline.plan_sha256, candidate.plan_sha256, report.interval_s,
    report.impact_limited, ...engineVersions.map(name => report.engine[name]),
    ...[baseline, candidate].flatMap(run => engineVersions.map(name => run.engine?.[name] ?? ""))];
  const suffix = metadata.map(value => `"${String(value).replaceAll('"', '""')}"`).join(",");
  const [, ...lines] = report.csv.trimEnd().split(/\r?\n/);
  return `${header},${names.join(",")}\n${lines.map(line => `${line},${suffix}`).join("\n")}\n`;
}

export function engineMatches(run, current) {
  return Boolean(run.engine && engineVersions.every(name => run.engine[name] === current[name]));
}
