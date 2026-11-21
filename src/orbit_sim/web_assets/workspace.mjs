import {cartesian, elapsed, events, keplerian, measure, parseTelemetry, propellant} from "./mission.mjs";
import {drawOrbit, drawTelemetry, sampleAtPointer} from "./plots.mjs";

const byId = (id) => document.getElementById(id);
const form = byId("plan-form");
const representation = byId("representation");
const orbitFields = byId("orbit-fields");
const spacecraftFields = byId("spacecraft-fields");
const propellantToggle = byId("track-propellant");
const maneuvers = byId("maneuvers");
const trajectory = byId("trajectory");
const telemetry = byId("telemetry");
const slider = byId("sample-index");
let plan;
let filename = "mission";
let result;
let selected = 0;
let revision = 0;
let busy = false;
let fieldCount = 0;
let activeRepresentation;
let orbitDrafts;
let useMissionJobs = false;
let activeJob;
let cancelPending = false;

const jobStates = {
  queued: "Mission queued…",
  running: "Simulating mission…",
  cancelling: "Stopping simulation…",
};

function numberField([name, label, unit, fallback], value = fallback) {
  const wrapper = document.createElement("label");
  wrapper.append(label + " ");
  if (unit) {
    const units = document.createElement("span");
    units.textContent = unit;
    wrapper.append(units);
  }
  const input = document.createElement("input");
  Object.assign(input, {name, type: "number", value, step: "any", required: true, id: `number-${++fieldCount}`});
  wrapper.append(input);
  return wrapper;
}

function orbitInputs(orbit = {}) {
  const isCartesian = representation.value === "initial_state";
  byId("orbit-context").textContent = isCartesian ? "Earth-centered inertial position and velocity." :
    "Earth-centered elements. RAAN is the right ascension of the ascending node.";
  orbitFields.replaceChildren(...(isCartesian ? cartesian : keplerian).map((spec) => {
    const [field, item] = spec[0].split(".");
    return numberField(spec, item === undefined ? orbit[field] : orbit[field]?.[Number(item)]);
  }));
}

function setMassStatus() {
  spacecraftFields.hidden = !propellantToggle.checked;
  byId("spacecraft-status").textContent = propellantToggle.checked ? "Propellant tracked" : "Propellant off";
}

function addBurn(type, entry = {}) {
  const finite = type === "finite_burns";
  const card = document.createElement("div");
  card.className = "burn-card";
  card.dataset.type = type;
  const header = document.createElement("div");
  header.className = "subhead";
  const title = document.createElement("strong");
  title.textContent = finite ? "Finite burn" : "Impulse";
  const remove = document.createElement("button");
  Object.assign(remove, {className: "text-button", type: "button", textContent: "Remove"});
  remove.setAttribute("aria-label", finite ? "Remove finite burn" : "Remove impulse");
  remove.addEventListener("click", () => {
    card.remove();
    edited();
    byId(finite ? "add-finite" : "add-impulse").focus();
  });
  header.append(title, remove);
  const fields = document.createElement("div");
  fields.className = "form-grid";
  const timing = finite ? [["start_s", "Start", "s", 0], ["end_s", "Cutoff", "s", 60]] :
    [["time_s", "Time", "s", 0]];
  fields.append(...timing.map((spec) => numberField(spec, entry[spec[0]])));
  const frameLabel = document.createElement("label");
  frameLabel.textContent = "Burn frame";
  const frame = document.createElement("select");
  frame.name = "frame";
  for (const name of ["ECI", "RTN"]) {
    frame.append(new Option(name, name));
  }
  frame.value = entry.frame ?? "RTN";
  frameLabel.append(frame);
  fields.append(frameLabel);
  const key = finite ? "thrust_n" : "delta_v_km_s";
  const defaults = finite ? [0, 100, 0] : [0, .05, 0];
  const vectorFields = defaults.map((fallback, index) =>
    numberField([`${key}.${index}`, "", finite ? "N" : "km/s", fallback], entry[key]?.[index]));
  const updateAxes = () => {
    const axes = frame.value === "ECI" ? ["X", "Y", "Z"] : ["Radial", "Transverse", "Normal"];
    vectorFields.forEach((label, index) => {
      label.firstChild.textContent = `${finite ? "Force" : "Δv"} ${axes[index]}`;
    });
  };
  frame.addEventListener("change", updateAxes);
  updateAxes();
  fields.append(...vectorFields);
  card.append(header, fields);
  maneuvers.append(card);
  return card.querySelector("input");
}

function presentPlan(next, name, stem) {
  plan = next;
  filename = stem;
  byId("plan-name").textContent = name;
  representation.value = plan.initial_orbit ? "initial_orbit" : "initial_state";
  activeRepresentation = representation.value;
  orbitDrafts = new Map();
  orbitInputs(plan[activeRepresentation]);
  for (const name of ["duration_s", "sample_interval_s", "gravity"]) {
    form.elements.namedItem(name).value = plan[name] ?? "point_mass";
  }
  spacecraftFields.replaceChildren(...propellant.map((spec) => numberField(spec, plan.spacecraft?.[spec[0]])));
  propellantToggle.checked = Object.hasOwn(plan, "spacecraft");
  setMassStatus();
  maneuvers.replaceChildren();
  for (const type of ["impulses", "finite_burns"]) {
    for (const entry of plan[type] ?? []) addBurn(type, entry);
  }
  edited();
}

function readNumber(input) {
  const value = input.valueAsNumber;
  let message;
  if (!Number.isFinite(value)) message = "Enter a finite number.";
  else if (input.min && value < Number(input.min)) message = `Minimum value is ${input.min}.`;
  else if (input.max && value > Number(input.max)) message = `Maximum value is ${input.max}.`;
  if (message) {
    const error = new Error(message);
    error.input = input;
    throw error;
  }
  return value;
}

function readNumbers(scope) {
  return Object.fromEntries(Array.from(scope.querySelectorAll('input[type="number"]'),
    (input) => [input.name, readNumber(input)]));
}

function readVector(values, key) {
  return [0, 1, 2].map((index) => values[`${key}.${index}`]);
}

function authoredPlan() {
  const next = structuredClone(plan);
  const values = readNumbers(orbitFields);
  delete next.initial_state;
  delete next.initial_orbit;
  if (representation.value === "initial_state") {
    next.initial_state = {position_km: readVector(values, "position_km"),
      velocity_km_s: readVector(values, "velocity_km_s")};
  } else next.initial_orbit = values;
  next.duration_s = readNumber(form.elements.namedItem("duration_s"));
  next.sample_interval_s = readNumber(form.elements.namedItem("sample_interval_s"));
  next.gravity = form.elements.namedItem("gravity").value;
  if (propellantToggle.checked) next.spacecraft = readNumbers(spacecraftFields);
  else delete next.spacecraft;
  next.impulses = [];
  next.finite_burns = [];
  for (const card of maneuvers.children) {
    const values = readNumbers(card);
    const frame = card.querySelector('select[name="frame"]').value;
    if (card.dataset.type === "impulses") {
      next.impulses.push({time_s: values.time_s, delta_v_km_s: readVector(values, "delta_v_km_s"), frame});
    } else {
      next.finite_burns.push({start_s: values.start_s, end_s: values.end_s, thrust_n: readVector(values, "thrust_n"), frame});
    }
  }
  return next;
}

function clearError() {
  const banner = byId("errors");
  banner.hidden = true;
  banner.textContent = "";
  for (const field of form.querySelectorAll('[aria-invalid="true"]')) {
    field.removeAttribute("aria-invalid");
    field.removeAttribute("aria-describedby");
  }
  for (const error of form.querySelectorAll(".field-error")) error.remove();
}

function showError(error) {
  const banner = byId("errors");
  banner.textContent = error.message;
  banner.hidden = false;
  const fields = Array.from(form.querySelectorAll("[name]")).filter((field) => !field.closest("[hidden]"));
  const matching = fields.filter((field) => {
    const base = field.name.split(".")[0];
    return error.field === field.name || error.message.includes(`${base} must`) ||
      error.message.includes(`${base} exceeds`);
  });
  const input = error.input ?? (matching.length === 1 ? matching[0] : null);
  if (input) {
    const inline = document.createElement("span");
    inline.id = "active-field-error";
    inline.className = "field-error";
    inline.textContent = error.message;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", inline.id);
    input.closest("label").append(inline);
    const disclosure = input.closest("details");
    if (disclosure) disclosure.open = true;
    input.focus();
  }
}

function status(text) {
  byId("status").textContent = text;
}

function controls() {
  for (const id of ["upload", "example"]) byId(id).disabled = busy;
  for (const id of ["editor-fields", "run", "validate", "save-plan"]) byId(id).disabled = busy || !plan;
  byId("plan-form").setAttribute("aria-busy", String(busy));
  const cancel = byId("cancel-run");
  cancel.hidden = !activeJob;
  cancel.disabled = cancelPending || activeJob?.state === "cancelling";
}

function edited() {
  revision++;
  clearError();
  status("");
  updateCaption();
}

async function request(path, body, method = body === undefined ? "GET" : "POST", key) {
  const headers = new Headers();
  if (body !== undefined) headers.set("Content-Type", "application/json");
  if (key) headers.set("Idempotency-Key", key);
  const response = await fetch(path, {method, headers, body, redirect: "manual"});
  if (response.type === "opaqueredirect") {
    const error = new Error("Sign-in expired. Reload the workspace to sign in again.");
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const issue = await response.json().catch(() => ({}));
    const message = response.status === 401 ? "Sign-in expired. Reload the workspace to sign in again." :
      issue.message ?? `Workspace request failed (${response.status}).`;
    const error = new Error(message);
    error.field = issue.field;
    error.status = response.status;
    throw error;
  }
  return response;
}

async function operate(task, activity) {
  if (busy) return;
  clearError();
  status(activity);
  busy = true;
  controls();
  let failure;
  try {
    await task();
  } catch (error) {
    failure = error;
    status("");
  } finally {
    busy = false;
    controls();
  }
  if (failure) showError(failure);
}

async function loadExample() {
  const menu = byId("example");
  const name = menu.selectedOptions[0]?.textContent;
  if (!menu.value) return;
  const stem = menu.value;
  await operate(async () => {
    const response = await request(`/api/examples/${stem}`);
    presentPlan(await response.json(), name, stem);
    if (useMissionJobs) status("Example ready to run.");
    else await runPlan();
  }, "Loading example…");
}

async function runPlan() {
  const body = JSON.stringify(authoredPlan());
  status(useMissionJobs ? "Submitting mission…" : "Simulating mission…");
  const response = useMissionJobs ? await runQueued(body) : await request("/api/run", body);
  if (!response) return;
  const csv = await response.blob();
  const rows = parseTelemetry(await csv.text());
  result = {csv, rows, revision, filename};
  showResult();
  status(`${events[rows.at(-1).kind]} · ${rows.length.toLocaleString("en-US")} telemetry rows`);
}

async function retryJobRequest(send) {
  try {
    return await send();
  } catch (error) {
    if (!(error instanceof TypeError) && error.status !== 502) throw error;
    return send();
  }
}

async function runQueued(body) {
  const key = crypto.randomUUID();
  try {
    const started = await retryJobRequest(() => request("/api/jobs", body, "POST", key));
    activeJob = await started.json();
    const path = `/api/jobs/${activeJob.id}`;
    const deadline = performance.now() + 120_000;
    while (Object.hasOwn(jobStates, activeJob.state)) {
      if (performance.now() >= deadline) throw new Error("The job did not reach a final state in two minutes.");
      status(jobStates[activeJob.state]);
      controls();
      await new Promise((resolve) => setTimeout(resolve, 350));
      const response = await retryJobRequest(() => request(path));
      activeJob = await response.json();
    }
    if (activeJob.state === "cancelled") {
      status("Simulation cancelled.");
      return null;
    }
    if (activeJob.state !== "complete") throw new Error(activeJob.message || "The simulation job failed.");
    return await retryJobRequest(() => request(`${path}/telemetry`));
  } finally {
    activeJob = null;
    controls();
  }
}

async function cancelRun() {
  if (!activeJob || cancelPending) return;
  const id = activeJob.id;
  cancelPending = true;
  controls();
  try {
    const response = await retryJobRequest(() => request(`/api/jobs/${id}`, undefined, "DELETE"));
    const cancellation = await response.json();
    if (activeJob?.id === id && cancellation.state === "cancelling") activeJob = cancellation;
  } catch (error) {
    showError(error);
  } finally {
    cancelPending = false;
    controls();
  }
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function updateCaption() {
  if (!result) return;
  const outdated = result.revision !== revision ? " · previous run, plan has changed" : "";
  const final = result.rows.at(-1);
  byId("result-caption").textContent = `${events[final.kind]} at T+${elapsed(final.time)}${outdated}`;
}

function showResult() {
  const {rows} = result;
  const final = rows.at(-1);
  byId("final-altitude").textContent = measure(final.altitude, "km");
  byId("final-speed").textContent = measure(final.speed, "km/s", 3);
  byId("final-mass").textContent = measure(final.mass, "kg");
  byId("row-count").textContent = rows.length.toLocaleString("en-US");
  byId("download").disabled = false;
  slider.max = rows.length - 1;
  slider.disabled = rows.length < 2;
  const eventMenu = byId("events");
  eventMenu.replaceChildren(new Option("Jump to event", ""));
  for (const [index, row] of rows.entries()) {
    if (row.kind !== "sample") eventMenu.append(new Option(`${events[row.kind]} · ${elapsed(row.time)}`, index));
  }
  eventMenu.disabled = eventMenu.options.length <= 1;
  updateCaption();
  inspect(rows.length - 1);
}

function inspect(index) {
  if (!result) return;
  selected = Math.max(0, Math.min(index, result.rows.length - 1));
  const row = result.rows[selected];
  slider.value = selected;
  const summary = `T+${elapsed(row.time)} · ${events[row.kind]} · altitude ${measure(row.altitude, "km")}` +
    ` · speed ${measure(row.speed, "km/s", 3)} · mass ${measure(row.mass, "kg")}`;
  slider.setAttribute("aria-valuetext", summary);
  byId("selected").textContent = summary;
  const components = (values, unit, digits) =>
    values.map((value, i) => `${"XYZ"[i]} ${measure(value, "", digits)}`).join(" · ") + ` ${unit}`;
  byId("selected-position").textContent = `Position · ${components(row.position, "km", 3)}`;
  byId("selected-velocity").textContent = `Velocity · ${components(row.velocity, "km/s", 6)}`;
  byId("events").value = row.kind === "sample" ? "" : String(selected);
  byId("previous").disabled = selected === 0;
  byId("next").disabled = selected >= result.rows.length - 1;
  redraw();
}

let framePending = false;
function redraw() {
  if (framePending) return;
  framePending = true;
  requestAnimationFrame(() => {
    framePending = false;
    drawOrbit(trajectory, result?.rows ?? [], selected, byId("plane").value);
    drawTelemetry(telemetry, result?.rows ?? [], selected, byId("series").value);
  });
}

form.addEventListener("input", edited);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  operate(runPlan, "Simulating mission…");
});
representation.addEventListener("change", () => {
  orbitDrafts.set(activeRepresentation, Array.from(orbitFields.querySelectorAll("input"), (input) => input.value));
  activeRepresentation = representation.value;
  orbitInputs(plan[activeRepresentation]);
  const draft = orbitDrafts.get(activeRepresentation);
  if (draft) orbitFields.querySelectorAll("input").forEach((input, index) => { input.value = draft[index]; });
});
propellantToggle.addEventListener("change", setMassStatus);
byId("add-impulse").addEventListener("click", () => {
  const input = addBurn("impulses");
  edited();
  input.focus();
});
byId("add-finite").addEventListener("click", () => {
  const input = addBurn("finite_burns");
  propellantToggle.checked = true;
  setMassStatus();
  edited();
  input.focus();
});
byId("example").addEventListener("change", loadExample);
byId("cancel-run").addEventListener("click", cancelRun);
byId("upload").addEventListener("change", (event) => {
  const picker = event.currentTarget;
  const file = picker.files[0];
  picker.value = "";
  if (!file) return;
  operate(async () => {
    if (file.size > 256 * 1024) throw new Error("Mission plan exceeds 256 KiB.");
    const raw = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(await file.arrayBuffer());
    await request("/api/validate", raw);
    const stem = file.name.replace(/\.json$/i, "").replace(/[^a-z0-9_-]/gi, "-").slice(0, 60) || "mission";
    presentPlan(JSON.parse(raw), file.name, stem);
    byId("example").selectedIndex = -1;
    status("Imported plan is valid.");
  }, "Checking imported plan…");
});
byId("validate").addEventListener("click", () => operate(async () => {
  await request("/api/validate", JSON.stringify(authoredPlan()));
  status("Mission plan is valid.");
}, "Validating plan…"));
byId("save-plan").addEventListener("click", () => {
  clearError();
  try {
    download(new Blob([JSON.stringify(authoredPlan(), null, 2) + "\n"], {type: "application/json"}), `${filename}.json`);
  } catch (error) {
    showError(error);
  }
});
byId("download").addEventListener("click", () => download(result.csv, `${result.filename}-telemetry.csv`));
slider.addEventListener("input", () => inspect(slider.valueAsNumber));
byId("events").addEventListener("change", (event) => {
  if (event.target.value !== "") inspect(Number(event.target.value));
});
byId("previous").addEventListener("click", () => inspect(selected - 1));
byId("next").addEventListener("click", () => inspect(selected + 1));
byId("plane").addEventListener("change", redraw);
byId("series").addEventListener("change", redraw);
telemetry.addEventListener("pointerdown", (event) => {
  if (result) inspect(sampleAtPointer(telemetry, result.rows, event.clientX));
});
const resize = new ResizeObserver(redraw);
resize.observe(trajectory);
resize.observe(telemetry);

async function initialize() {
  controls();
  try {
    const capabilities = await request("/api/capabilities");
    useMissionJobs = (await capabilities.json()).mission_jobs === true;
    await loadExample();
  } catch (error) {
    showError(error);
  }
}

initialize();
