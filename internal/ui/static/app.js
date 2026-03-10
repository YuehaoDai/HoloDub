const state = {
  selectedJobId: null,
};

function apiHeaders() {
  const headers = { "Content-Type": "application/json" };
  const apiKey = document.getElementById("apiKey").value.trim();
  if (apiKey) headers["X-API-Key"] = apiKey;
  return headers;
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...apiHeaders(), ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.message || payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

async function refreshJobs() {
  const data = await apiFetch("/jobs");
  const container = document.getElementById("jobs");
  container.innerHTML = "";
  for (const job of data.jobs || []) {
    const item = document.createElement("article");
    item.className = "item";
    item.innerHTML = `
      <h3>${job.name || `Job ${job.id}`}</h3>
      <div class="meta">#${job.id} <span class="pill">${job.status}</span> stage=${job.current_stage}</div>
      <div class="meta">input=${job.input_relpath || ""}</div>
      <div class="actions">
        <button data-job-id="${job.id}" data-action="view">View</button>
        <button data-job-id="${job.id}" data-action="start">Start</button>
        <button data-job-id="${job.id}" data-action="cancel">Cancel</button>
      </div>
    `;
    container.appendChild(item);
  }
}

async function refreshProfiles() {
  const data = await apiFetch("/voice-profiles");
  const container = document.getElementById("profiles");
  container.innerHTML = "";
  for (const profile of data.voice_profiles || []) {
    const item = document.createElement("article");
    item.className = "item";
    item.innerHTML = `
      <h3>${profile.name}</h3>
      <div class="meta">provider=${profile.provider || "n/a"} mode=${profile.mode || "n/a"}</div>
      <div class="meta">validation=${profile.validation_status || "unknown"}</div>
      <div class="actions">
        <button data-profile-id="${profile.id}" data-action="validate-profile">Validate</button>
      </div>
    `;
    container.appendChild(item);
  }
}

async function loadJobDetail(jobId) {
  state.selectedJobId = jobId;
  const [job, segments, stageRuns, bindings, artifacts] = await Promise.all([
    apiFetch(`/jobs/${jobId}`),
    apiFetch(`/jobs/${jobId}/segments`),
    apiFetch(`/jobs/${jobId}/stage-runs`),
    apiFetch(`/jobs/${jobId}/bindings`),
    apiFetch(`/jobs/${jobId}/artifacts`),
  ]);

  const container = document.getElementById("jobDetail");
  container.innerHTML = `
    <article class="item">
      <h3>Job</h3>
      <pre>${JSON.stringify(job, null, 2)}</pre>
    </article>
    <article class="item">
      <h3>Segments</h3>
      <pre>${JSON.stringify(segments.segments || [], null, 2)}</pre>
    </article>
    <article class="item">
      <h3>Stage runs</h3>
      <pre>${JSON.stringify(stageRuns.stage_runs || [], null, 2)}</pre>
    </article>
    <article class="item">
      <h3>Bindings</h3>
      <pre>${JSON.stringify(bindings.bindings || [], null, 2)}</pre>
    </article>
    <article class="item">
      <h3>Artifacts</h3>
      <pre>${JSON.stringify(artifacts.artifacts || [], null, 2)}</pre>
    </article>
  `;
}

async function submitJobForm(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = {
    name: form.get("name"),
    input_relpath: form.get("input_relpath"),
    source_language: form.get("source_language"),
    target_language: form.get("target_language"),
    webhook_url: form.get("webhook_url"),
    max_retries: Number(form.get("max_retries") || 0),
    auto_start: form.get("auto_start") === "on",
  };
  await apiFetch("/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  event.target.reset();
  await refreshJobs();
}

async function handleClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;

  const action = target.dataset.action;
  if (action === "view") {
    await loadJobDetail(target.dataset.jobId);
    return;
  }
  if (action === "start") {
    await apiFetch(`/jobs/${target.dataset.jobId}/start`, { method: "POST" });
    await refreshJobs();
    return;
  }
  if (action === "cancel") {
    await apiFetch(`/jobs/${target.dataset.jobId}/cancel`, { method: "POST" });
    await refreshJobs();
    return;
  }
  if (action === "validate-profile") {
    await apiFetch(`/voice-profiles/${target.dataset.profileId}/validate`, { method: "POST" });
    await refreshProfiles();
  }
}

async function boot() {
  document.getElementById("jobForm").addEventListener("submit", submitJobForm);
  document.getElementById("refreshJobs").addEventListener("click", refreshJobs);
  document.getElementById("refreshProfiles").addEventListener("click", refreshProfiles);
  document.body.addEventListener("click", (event) => {
    handleClick(event).catch((error) => window.alert(error.message));
  });

  await Promise.all([refreshJobs(), refreshProfiles()]);
}

boot().catch((error) => {
  window.alert(error.message);
});
