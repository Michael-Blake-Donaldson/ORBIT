const rank = {queued: 0, running: 1, cancelling: 2, complete: 3, failed: 3, cancelled: 3};
const jobId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;

export function jobIsActive(state) {
  return state === "queued" || state === "running" || state === "cancelling";
}

function retryable(error) {
  return error instanceof TypeError || error.name === "TimeoutError" || error.status === 502;
}

async function retryOnce(send) {
  try {
    return await send();
  } catch (error) {
    if (!retryable(error)) throw error;
    return send();
  }
}

/** Retain one unresolved submission in this tab, including its exact plan and retry key. */
export class JobSession {
  #request;
  #key;
  #wait;
  #attempt = null;
  #running = false;

  constructor(request, makeKey = () => crypto.randomUUID(),
              wait = () => new Promise((resolve) => setTimeout(resolve, 350))) {
    this.#request = request;
    this.#key = makeKey;
    this.#wait = wait;
  }

  async run(body, onState) {
    if (this.#running) throw new Error("A mission is already being monitored.");
    if (this.#attempt?.body !== body) {
      this.#attempt = {body, key: this.#key(), snapshot: null};
    }
    const attempt = this.#attempt;
    this.#running = true;
    try {
      if (!attempt.snapshot) {
        const response = await retryOnce(() => this.#request("/api/jobs", body, "POST", attempt.key));
        this.#observe(attempt, await response.json());
      }
      const path = `/api/jobs/${attempt.snapshot.id}`;
      const deadline = performance.now() + 120_000;
      for (;;) {
        onState(attempt.snapshot);
        const state = attempt.snapshot.state;
        if (!jobIsActive(state)) break;
        if (performance.now() >= deadline) throw new Error("Stopped waiting for this job. Retry to resume monitoring.");
        await this.#wait();
        const response = await retryOnce(() => this.#request(path));
        this.#observe(attempt, await response.json());
      }
      if (attempt.snapshot.state === "complete") {
        return await retryOnce(() => this.#request(`${path}/telemetry`));
      }
      this.#attempt = null;
      if (attempt.snapshot.state === "cancelled") return null;
      throw new Error(attempt.snapshot.message || "The simulation job failed.");
    } catch (error) {
      if (error.status === 404 || error.status === 409) this.#attempt = null;
      throw error;
    } finally {
      this.#running = false;
    }
  }

  async cancel(onState) {
    const attempt = this.#attempt;
    if (!this.#running || !jobIsActive(attempt?.snapshot?.state)) return;
    const path = `/api/jobs/${attempt.snapshot.id}`;
    const response = await retryOnce(() => this.#request(path, undefined, "DELETE"));
    const snapshot = await response.json();
    if (this.#running && this.#attempt === attempt) {
      this.#observe(attempt, snapshot);
      onState(attempt.snapshot);
    }
  }

  acknowledgeResult() {
    this.#attempt = null;
  }

  #observe(attempt, snapshot) {
    if (!snapshot || typeof snapshot.id !== "string" || !jobId.test(snapshot.id)
        || !Object.hasOwn(rank, snapshot.state) || (attempt.snapshot && snapshot.id !== attempt.snapshot.id)) {
      throw new Error("The job service returned an invalid status.");
    }
    // A poll started before DELETE can arrive after the cancellation response.
    if (!attempt.snapshot || (jobIsActive(attempt.snapshot.state) && rank[snapshot.state] >= rank[attempt.snapshot.state])) {
      attempt.snapshot = snapshot;
    }
  }
}
