import assert from "node:assert/strict";
import test from "node:test";
import {elapsed, nearestSample, parseTelemetry} from "../src/orbit_sim/web_assets/mission.mjs";

const header = "time_s,kind,x_km,y_km,z_km,vx_km_s,vy_km_s,vz_km_s,altitude_km,speed_km_s," +
  "specific_energy_km2_s2,mass_kg,semi_major_axis_km,eccentricity,inclination_deg,raan_deg," +
  "argument_of_periapsis_deg,true_anomaly_deg";
const row = (time, kind, speed = 7.5, mass = "") =>
  `${time},${kind},7000,0,0,0,${speed},0,621.863,${speed},-28.47,${mass},7000,0,0,,,\n`;

test("retain both states at an impulse and select the post-burn row on a time seek", () => {
  const rows = parseTelemetry(header + "\n" + row(0, "sample") +
    row(2, "pre_burn", 7.5, "1000") + row(2, "post_burn", 8, "850") + row(5, "complete", 8));
  assert.equal(rows[0].mass, null);
  assert.equal(rows[1].velocity[1], 7.5);
  assert.equal(rows[2].velocity[1], 8);
  assert.equal(nearestSample(rows, 2), 2);
  assert.equal(nearestSample(rows, 1.95), 1);
  assert.equal(nearestSample(rows, 3.4), 2);
  assert.equal(nearestSample(rows, 4), 3);
});

test("reject incomplete, decreasing, or non-finite telemetry", () => {
  const start = header + "\n" + row(0, "sample");
  assert.throws(() => parseTelemetry(start), /incomplete/);
  assert.throws(() => parseTelemetry(start + row(5, "sample") + row(1, "complete")), /invalid telemetry row/);
  assert.throws(() => parseTelemetry(start + row(1, "complete", "Infinity")), /invalid telemetry row/);
});

test("elapsed time labels retain milliseconds and can exceed a day", () => {
  assert.equal(elapsed(4607.5111276723765), "01:16:47.511");
  assert.equal(elapsed(86400), "24:00:00.000");
});
