import assert from "node:assert/strict";
import test from "node:test";
import {JobSession} from "../src/orbit_sim/web_assets/jobs.mjs";
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

const id = "01234567-1234-1234-1234-0123456789ab";
const snapshot = (state) => ({json: async () => ({id, state})});

test("retrying an ambiguous submission reuses its key until the result is accepted", async () => {
  const keys = [];
  let sequence = 0;
  const csv = {csv: true};
  const session = new JobSession(async (path, body, method, key) => {
    if (method !== "POST") return csv;
    keys.push(key);
    if (keys.length < 3) throw new TypeError("lost response");
    return snapshot("complete");
  }, () => `key-${++sequence}`);

  await assert.rejects(session.run("same-plan", () => {}), /lost response/);
  assert.equal(await session.run("same-plan", () => {}), csv);
  assert.deepEqual(keys, ["key-1", "key-1", "key-1"]);
  session.acknowledgeResult();
  assert.equal(await session.run("same-plan", () => {}), csv);
  assert.equal(keys.at(-1), "key-2");
});

test("status retry resumes the known job instead of resubmitting the plan", async () => {
  let submits = 0;
  let polls = 0;
  const session = new JobSession(async (path, body, method) => {
    if (method === "POST") {
      submits++;
      return snapshot("running");
    }
    assert.equal(path, `/api/jobs/${id}`);
    if (++polls < 3) throw Object.assign(new Error("unavailable"), {status: 502});
    return snapshot("cancelled");
  }, () => "key", async () => {});
  await assert.rejects(session.run("same-plan", () => {}), /unavailable/);
  assert.equal(await session.run("same-plan", () => {}), null);
  assert.equal(submits, 1);
});

test("a delayed running response cannot undo a recorded cancellation", async () => {
  let releasePoll;
  let announcePoll;
  const pollStarted = new Promise((resolve) => { announcePoll = resolve; });
  const stale = new Promise((resolve) => { releasePoll = resolve; });
  const states = [];
  let polls = 0;
  const session = new JobSession(async (path, body, method) => {
    if (method === "POST") return snapshot("running");
    if (method === "DELETE") return snapshot("cancelling");
    if (++polls === 1) {
      announcePoll();
      return stale;
    }
    return snapshot("cancelled");
  }, () => "key", async () => {});
  const observe = ({state}) => { states.push(state); };
  const running = session.run("plan", observe);
  await pollStarted;
  await session.cancel(observe);
  releasePoll(snapshot("running"));
  assert.equal(await running, null);
  assert.deepEqual(states, ["running", "cancelling", "cancelling", "cancelled"]);
});
