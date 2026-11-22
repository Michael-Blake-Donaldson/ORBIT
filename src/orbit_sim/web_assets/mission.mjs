export const cartesian = [
  ["position_km.0", "Position X", "km", 7000], ["position_km.1", "Position Y", "km", 0],
  ["position_km.2", "Position Z", "km", 0], ["velocity_km_s.0", "Velocity X", "km/s", 0],
  ["velocity_km_s.1", "Velocity Y", "km/s", 7.546053290107541], ["velocity_km_s.2", "Velocity Z", "km/s", 0],
];
export const keplerian = [
  ["periapsis_radius_km", "Periapsis radius", "km", 7000], ["eccentricity", "Eccentricity", "", 0],
  ["inclination_deg", "Inclination", "°", 0], ["raan_deg", "RAAN", "°", 0],
  ["argument_of_periapsis_deg", "Argument of periapsis", "°", 0], ["true_anomaly_deg", "True anomaly", "°", 0],
];
export const propellant = [
  ["initial_mass_kg", "Wet mass", "kg", 1000], ["dry_mass_kg", "Dry mass", "kg", 450],
  ["isp_s", "Specific impulse", "s", 320],
];

export const events = {
  sample: "Sample", pre_burn: "Before impulse", post_burn: "After impulse",
  burn_start: "Ignition", burn_end: "Cutoff", apoapsis: "Apoapsis", periapsis: "Periapsis",
  complete: "Mission complete", impact: "Surface impact",
};
export const metrics = {
  altitude: { label: "Altitude", unit: "km" }, speed: { label: "Speed", unit: "km/s" },
  mass: { label: "Mass", unit: "kg" }, axis: { label: "Semi-major axis", unit: "km" },
};

const decimal = /^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;
const optionalColumns = new Set([11, 12, 14, 15, 16, 17]);

export function parseTelemetry(csv) {
  const [header, ...lines] = csv.replace(/\r?\n$/, "").split(/\r?\n/);
  const expected = "time_s,kind,x_km,y_km,z_km,vx_km_s,vy_km_s,vz_km_s,altitude_km,speed_km_s," +
    "specific_energy_km2_s2,mass_kg,semi_major_axis_km,eccentricity,inclination_deg,raan_deg," +
    "argument_of_periapsis_deg,true_anomaly_deg";
  if (header !== expected || lines.length === 0 || lines.length > 4000) {
    throw new Error("The workspace returned an unexpected telemetry format.");
  }
  let previousTime = -1;
  const rows = lines.map((line, index) => {
    const values = line.split(",");
    const [time, kind, x, y, z, vx, vy, vz, altitude, speed, , mass, axis] = values;
    if (values.length !== 18 || !Object.hasOwn(events, kind) ||
        values.some((value, column) => column !== 1 && !(value === "" && optionalColumns.has(column)) &&
          (!decimal.test(value) || !Number.isFinite(Number(value)))) || Number(time) < previousTime) {
      throw new Error("The workspace returned an invalid telemetry row.");
    }
    if ((kind === "complete" || kind === "impact") && index !== lines.length - 1) {
      throw new Error("The workspace returned telemetry after a terminal row.");
    }
    previousTime = Number(time);
    return {time: previousTime, kind, position: [Number(x), Number(y), Number(z)],
      velocity: [Number(vx), Number(vy), Number(vz)], altitude: Number(altitude), speed: Number(speed),
      mass: mass === "" ? null : Number(mass), axis: axis === "" ? null : Number(axis)};
  });
  if (rows[0].time !== 0 || !["complete", "impact"].includes(rows.at(-1).kind)) {
    throw new Error("The workspace returned an incomplete flight timeline.");
  }
  return rows;
}

export function nearestSample(rows, time) {
  let low = 0;
  let high = rows.length;
  // Select the last row at a shared epoch when navigating by time, rather than by row.
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (rows[middle].time <= time) low = middle + 1;
    else high = middle;
  }
  if (low === 0) return 0;
  if (low === rows.length || time - rows[low - 1].time <= rows[low].time - time) return low - 1;
  return low;
}

export function measure(value, unit, digits = 2) {
  if (value === null) return "—";
  const number = Math.abs(value) >= 1e7 ? value.toExponential(3) :
    value.toLocaleString("en-US", {maximumFractionDigits: digits});
  return unit ? `${number} ${unit}` : number;
}

export function elapsed(time) {
  const millis = Math.round(time * 1000);
  const hours = Math.floor(millis / 3600000);
  const minutes = Math.floor(millis / 60000) % 60;
  const seconds = Math.floor(millis / 1000) % 60;
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":") +
    "." + String(millis % 1000).padStart(3, "0");
}
