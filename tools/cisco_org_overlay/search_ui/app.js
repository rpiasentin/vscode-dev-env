const scopeInfo = {
  people: {
    label: "People",
    subtitle: "Current person rows",
  },
  snapshots: {
    label: "Snapshots",
    subtitle: "Historical rows",
  },
  documents: {
    label: "Documents",
    subtitle: "Generic overlay docs",
  },
  manager_checks: {
    label: "Manager checks",
    subtitle: "Completion status",
  },
  unresolved_aliases: {
    label: "Unresolved aliases",
    subtitle: "404s and follow-up",
  },
  org_edges: {
    label: "Org edges",
    subtitle: "Parent-child links",
  },
};

const chartableDatabases = new Set(["generic", "jeetu", "oliver"]);
const searchTimeoutMs = 90000;
const crawlerProgressIntervalMs = 10000;

const state = {
  databases: [],
  hits: [],
  selectedHit: null,
  lastQuery: "",
  lastSearch: null,
  lastSearchRequest: null,
  isSearching: false,
  searchRequestId: 0,
  isChartLoading: false,
  selectedChartHits: new Map(),
  orgChart: null,
  crawlerProgress: null,
  isCrawlerProgressLoading: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  wireEvents();
  loadDatabases();
  loadCrawlerProgress();
  window.setInterval(loadCrawlerProgress, crawlerProgressIntervalMs);
});

function bindElements() {
  elements.searchForm = document.getElementById("search-form");
  elements.queryInput = document.getElementById("query-input");
  elements.databaseSelector = document.getElementById("database-selector");
  elements.scopeSelector = document.getElementById("scope-selector");
  elements.matchMode = document.getElementById("match-mode");
  elements.limitInput = document.getElementById("limit-input");
  elements.deepInput = document.getElementById("deep-input");
  elements.searchButton = document.getElementById("search-button");
  elements.resetButton = document.getElementById("reset-button");
  elements.refreshHealthButton = document.getElementById("refresh-health");
  elements.refreshCrawlerProgressButton = document.getElementById("refresh-crawler-progress");
  elements.healthStatus = document.getElementById("health-status");
  elements.databaseHealth = document.getElementById("database-health");
  elements.crawlerProgressStatus = document.getElementById("crawler-progress-status");
  elements.crawlerProgress = document.getElementById("crawler-progress");
  elements.resultSummary = document.getElementById("result-summary");
  elements.exportMarkdownButton = document.getElementById("export-markdown");
  elements.buildOrgChartButton = document.getElementById("build-org-chart");
  elements.clearOrgChartButton = document.getElementById("clear-org-chart");
  elements.chartSelectionSummary = document.getElementById("chart-selection-summary");
  elements.searchStatus = document.getElementById("search-status");
  elements.paginationTop = document.getElementById("pagination-top");
  elements.paginationBottom = document.getElementById("pagination-bottom");
  elements.resultsList = document.getElementById("results-list");
  elements.chartStatus = document.getElementById("chart-status");
  elements.orgChart = document.getElementById("org-chart");
  elements.detailPane = document.getElementById("detail-pane");
  elements.copyJsonButton = document.getElementById("copy-json");
}

function wireEvents() {
  elements.searchForm.addEventListener("submit", onSearchSubmit);
  elements.resetButton.addEventListener("click", resetForm);
  elements.refreshHealthButton.addEventListener("click", loadDatabases);
  elements.refreshCrawlerProgressButton.addEventListener("click", () => loadCrawlerProgress(true));
  elements.exportMarkdownButton.addEventListener("click", exportResultsAsMarkdown);
  elements.buildOrgChartButton.addEventListener("click", buildOrgChart);
  elements.clearOrgChartButton.addEventListener("click", () => clearChartSelections());
  elements.orgChart.addEventListener("click", onOrgChartClick);
  elements.copyJsonButton.addEventListener("click", copySelectedJson);
  document.querySelectorAll(".quick-example").forEach((button) => {
    button.addEventListener("click", () => applyQuickExample(button));
  });
}

async function loadDatabases() {
  setHealthStatus("Checking local database access…");
  try {
    const response = await fetch("/api/databases", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "failed to load databases");
    }
    state.databases = payload.databases || [];
    renderDatabaseSelectors();
    renderScopeSelectors();
    renderHealthCards();
    setHealthStatus(`Loaded ${state.databases.length} local database sources.`);
  } catch (error) {
    state.databases = [];
    elements.databaseSelector.innerHTML = "";
    elements.scopeSelector.innerHTML = "";
    elements.databaseHealth.innerHTML = "";
    setHealthStatus(error.message, true);
  }
}

async function loadCrawlerProgress(force = false) {
  if (!elements.crawlerProgress) {
    return;
  }
  if (state.isCrawlerProgressLoading && !force) {
    return;
  }
  state.isCrawlerProgressLoading = true;
  elements.refreshCrawlerProgressButton.disabled = true;
  elements.crawlerProgressStatus.textContent = "Refreshing crawler progress…";
  try {
    const response = await fetch("/api/crawler-progress", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "failed to load crawler progress");
    }
    state.crawlerProgress = payload;
    renderCrawlerProgress(payload);
    elements.crawlerProgressStatus.textContent = `Last checked ${formatClockTime(payload.generated_at)} · auto-refreshes every ${Math.round(crawlerProgressIntervalMs / 1000)}s.`;
  } catch (error) {
    elements.crawlerProgress.innerHTML = emptyCard(
      "Progress unavailable",
      error.message || "The crawler progress endpoint did not respond."
    );
    elements.crawlerProgressStatus.textContent = error.message;
  } finally {
    state.isCrawlerProgressLoading = false;
    elements.refreshCrawlerProgressButton.disabled = false;
  }
}

function renderCrawlerProgress(payload) {
  const crawlers = payload.crawlers || [];
  if (!crawlers.length) {
    elements.crawlerProgress.innerHTML = emptyCard(
      "No focused crawlers found",
      "Jeetu and Oliver focused DBs are not available in the local configuration."
    );
    return;
  }
  elements.crawlerProgress.innerHTML = crawlers.map(renderCrawlerCard).join("");
}

function renderCrawlerCard(crawler) {
  const database = crawler.database || {};
  const latest = database.latest_run || {};
  const counts = database.counts || {};
  const log = crawler.log || {};
  const checkpoint = log.last_checkpoint || {};
  const process = (crawler.crawler_processes || [])[0] || null;
  const helper = (crawler.session_helper_processes || [])[0] || null;
  const supervisor = (crawler.supervisor_processes || [])[0] || null;
  const stateInfo = crawlerStateInfo(crawler, latest);
  const traversal = traversalProgress(checkpoint);
  const warning = progressWarning(crawler, latest, log, database);

  return `
    <article class="crawler-card ${escapeHtml(stateInfo.className)}">
      <div class="crawler-card-head">
        <div>
          <h3>${escapeHtml(crawler.root_label || crawler.db_label)}</h3>
          <p>${escapeHtml(crawler.db_label)} · root ${escapeHtml(crawler.root_alias || "unknown")}</p>
        </div>
        <span class="crawler-badge">${escapeHtml(stateInfo.label)}</span>
      </div>

      <div class="crawler-process-row">
        ${process ? `<span>PID ${escapeHtml(process.pid)} · ${escapeHtml(process.elapsed)} · CPU ${escapeHtml(process.pcpu)}%</span>` : "<span>No crawler process</span>"}
        ${supervisor ? `<span>Supervisor PID ${escapeHtml(supervisor.pid)}</span>` : ""}
        ${helper ? `<span>Session helper PID ${escapeHtml(helper.pid)}</span>` : ""}
      </div>

      <div class="crawler-progress-bar" aria-label="Traversal progress">
        <span style="width: ${escapeHtml(String(traversal.percent))}%"></span>
      </div>
      <div class="crawler-progress-caption">
        ${traversal.label}
      </div>

      <div class="counts-grid crawler-counts">
        ${crawlerCountCard("Run", latest.id ? `#${latest.id}` : "None", latest.status || "no run")}
        ${crawlerCountCard("Processed", checkpoint.processed, "last checkpoint")}
        ${crawlerCountCard("Queue", checkpoint.queue, "pending aliases")}
        ${crawlerCountCard("Checks", counts.checks_this_run, "this run")}
        ${crawlerCountCard("Snapshots", counts.snapshots_this_run, "this run")}
        ${crawlerCountCard("Observations", counts.observations_this_run ?? counts.observations, "edge sightings")}
        ${crawlerCountCard("Inactive", counts.inactive_edges, "edges retired")}
        ${crawlerCountCard("Deficits", counts.deficits_this_run, "this run")}
        ${crawlerCountCard("Transient", counts.transient_this_run, "this run")}
      </div>

      <div class="crawler-meta">
        <span>People ${formatMaybeNumber(counts.people)}</span>
        <span>Active edges ${formatMaybeNumber(counts.active_edges)}</span>
        <span>Manager checks ${formatMaybeNumber(counts.manager_checks)}</span>
        ${latest.started_at ? `<span>Started ${escapeHtml(latest.started_at)}</span>` : ""}
      </div>

      ${warning ? `<div class="crawler-warning">${escapeHtml(warning)}</div>` : ""}
      ${checkpoint.line ? `<div class="crawler-log-line">${escapeHtml(checkpoint.line)}</div>` : ""}
    </article>
  `;
}

function crawlerStateInfo(crawler, latest) {
  if ((crawler.crawler_processes || []).length) {
    return { label: "Running", className: "is-running" };
  }
  if ((crawler.session_helper_processes || []).length) {
    return { label: "Auth refresh", className: "is-working" };
  }
  if (latest && latest.status === "running") {
    return { label: "Run record active", className: "is-warning" };
  }
  if (latest && latest.status === "completed") {
    return { label: "Idle", className: "is-idle" };
  }
  if (latest && latest.status === "failed") {
    return { label: "Failed", className: "is-error" };
  }
  return { label: "Unknown", className: "is-warning" };
}

function traversalProgress(checkpoint) {
  const processed = Number(checkpoint.processed || 0);
  const queue = Number(checkpoint.queue || 0);
  const total = processed + queue;
  if (!total) {
    return {
      percent: 0,
      label: "Waiting for first checkpoint.",
    };
  }
  const percent = Math.max(3, Math.min(100, Math.round((processed / total) * 100)));
  return {
    percent,
    label: `${formatNumber(processed)} processed · ${formatNumber(queue)} queued · dynamic traversal estimate`,
  };
}

function progressWarning(crawler, latest, log, database) {
  if (database && database.error) {
    return `Database read issue: ${database.error}`;
  }
  if (log && log.last_warning) {
    return log.last_warning;
  }
  if (latest && latest.status === "running" && !(crawler.crawler_processes || []).length) {
    return "The latest run is marked running, but no crawler process is visible.";
  }
  return "";
}

function crawlerCountCard(label, value, subtitle) {
  return `
    <div class="count-card">
      <span class="count-label">${escapeHtml(label)}</span>
      <span class="count-value">${escapeHtml(formatMaybeNumber(value))}</span>
      <span class="count-subtitle">${escapeHtml(subtitle || "")}</span>
    </div>
  `;
}

function formatMaybeNumber(value) {
  if (value === undefined || value === null || value === "") {
    return "0";
  }
  const number = Number(value);
  return Number.isFinite(number) ? formatNumber(number) : String(value);
}

function formatClockTime(epochSeconds) {
  const date = new Date(Number(epochSeconds || 0) * 1000);
  if (Number.isNaN(date.getTime())) {
    return "just now";
  }
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function renderDatabaseSelectors() {
  const selected = new Set(getSelectedDatabases());
  elements.databaseSelector.innerHTML = "";

  state.databases.forEach((database) => {
    const label = document.createElement("label");
    label.className = "selector";

    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "databases";
    input.value = database.label;
    input.checked = selected.size === 0 ? Boolean(database.exists) : selected.has(database.label);
    input.disabled = !database.exists || database.schema_kind === "unreadable";
    input.addEventListener("change", renderScopeSelectors);

    const span = document.createElement("span");
    span.innerHTML = `
      <strong>${escapeHtml(database.label)}</strong>
      <small class="selector-subtitle">${escapeHtml(database.schema_kind)}</small>
    `;

    label.append(input, span);
    elements.databaseSelector.append(label);
  });
}

function renderScopeSelectors() {
  const selectedDatabases = selectedDatabaseObjects();
  const supportedScopeSet = new Set();
  const selectedScopeValues = new Set(getSelectedScopes());

  selectedDatabases.forEach((database) => {
    (database.supported_scopes || []).forEach((scope) => supportedScopeSet.add(scope));
  });

  if (supportedScopeSet.size === 0) {
    Object.keys(scopeInfo).forEach((scope) => supportedScopeSet.add(scope));
  }

  elements.scopeSelector.innerHTML = "";
  Object.entries(scopeInfo).forEach(([scope, meta]) => {
    const label = document.createElement("label");
    label.className = "selector";

    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "scopes";
    input.value = scope;
    input.disabled = !supportedScopeSet.has(scope);
    input.checked = !input.disabled && selectedScopeValues.has(scope);

    const span = document.createElement("span");
    span.innerHTML = `
      <strong>${escapeHtml(meta.label)}</strong>
      <small class="selector-subtitle">${escapeHtml(meta.subtitle)}</small>
    `;

    label.append(input, span);
    elements.scopeSelector.append(label);
  });
}

function renderHealthCards() {
  elements.databaseHealth.innerHTML = "";
  if (!state.databases.length) {
    elements.databaseHealth.innerHTML = emptyCard("No databases loaded", "The health panel will populate after a successful refresh.");
    return;
  }

  state.databases.forEach((database) => {
    const card = document.createElement("article");
    card.className = `health-card${database.error ? " is-error" : ""}`;

    const counts = Object.entries(database.table_counts || {}).slice(0, 6);
    const badgeLabel = database.error ? "Needs attention" : "Ready";

    card.innerHTML = `
      <div class="health-top">
        <div>
          <h3 class="health-title">${escapeHtml(database.label)}</h3>
          <div class="microcopy">${escapeHtml(database.schema_kind || "unknown")} schema</div>
        </div>
        <span class="badge">${escapeHtml(badgeLabel)}</span>
      </div>
      <div class="badge-row">
        ${(database.default_scopes || []).map((scope) => `<span class="meta-pill">${escapeHtml(prettyScope(scope))}</span>`).join("")}
      </div>
      <div class="counts-grid">
        ${counts.map(([name, value]) => `
          <div class="count-card">
            <span class="count-label">${escapeHtml(prettyScope(name))}</span>
            <span class="count-value">${formatNumber(value)}</span>
          </div>
        `).join("")}
      </div>
      <div class="path-text">${escapeHtml(database.path)}</div>
      ${database.error ? `<div class="path-text">${escapeHtml(database.error)}</div>` : ""}
    `;

    elements.databaseHealth.append(card);
  });
}

async function onSearchSubmit(event) {
  event.preventDefault();
  const options = readSearchOptions();
  if (!options) {
    return;
  }
  await runSearch(1, options);
}

function readSearchOptions() {
  const query = elements.queryInput.value.trim();
  if (!query) {
    showStatus("Enter a search query first.", true);
    elements.queryInput.focus();
    return null;
  }

  const databases = getSelectedDatabases();
  if (!databases.length) {
    showStatus("Select at least one database.", true);
    return null;
  }

  const scopes = getSelectedScopes();
  const limit = Number.parseInt(elements.limitInput.value, 10);
  if (!Number.isFinite(limit) || limit < 1 || limit > 250) {
    showStatus("Page size must be between 1 and 250.", true);
    elements.limitInput.focus();
    return null;
  }

  return {
    query,
    databases,
    scopes,
    mode: elements.matchMode.value,
    deep: elements.deepInput.checked,
    limit,
  };
}

async function runSearch(page, options = state.lastSearchRequest) {
  if (!options) {
    showStatus("Run a search before paging through results.", true);
    return;
  }

  const requestId = ++state.searchRequestId;
  setSearching(true);
  showStatus(page > 1 ? `Loading page ${formatNumber(page)}…` : "Searching local databases…");

  try {
    const { response, payload } = await fetchJsonWithTimeout("/api/search", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ...options,
        page,
      }),
    }, searchTimeoutMs);
    if (requestId !== state.searchRequestId) {
      return;
    }
    if (!response.ok) {
      throw new Error(payload.error || "search failed");
    }
    state.lastSearchRequest = { ...options };
    state.lastQuery = payload.query;
    state.hits = payload.results || [];
    state.selectedHit = state.hits[0] || null;
    state.lastSearch = {
      ...payload,
      exported_at: new Date().toISOString(),
    };
    clearChartSelections(false);
    renderResults(payload);
    renderDetailPane();
    renderOrgChart();
    const dbText = payload.databases.join(", ");
    const timeText = `${payload.duration_ms.toFixed(1)} ms`;
    const rangeText = payload.result_count
      ? `showing ${formatNumber(payload.start)}-${formatNumber(payload.end)}`
      : "showing none";
    const capText = payload.max_results_reached ? " Result set reached the local safety cap." : "";
    showStatus(
      `Found ${formatNumber(payload.count)} result${payload.count === 1 ? "" : "s"} across ${dbText}; ${rangeText} on page ${formatNumber(payload.page)} of ${formatNumber(payload.total_pages || 1)} in ${timeText}.${capText}`
    );
  } catch (error) {
    if (requestId !== state.searchRequestId) {
      return;
    }
    state.hits = [];
    state.selectedHit = null;
    state.lastSearch = null;
    if (page === 1) {
      state.lastSearchRequest = null;
    }
    clearChartSelections(false);
    renderResults(null);
    renderDetailPane();
    renderOrgChart();
    showStatus(error.message, true);
  } finally {
    if (requestId === state.searchRequestId) {
      setSearching(false);
    }
  }
}

function renderResults(payload) {
  elements.resultsList.innerHTML = "";
  syncExportButton();
  syncChartButtons();
  renderPagination(payload);

  if (!payload || !state.hits.length) {
    elements.resultSummary.textContent = payload ? "No matches for this query." : "Run a search to populate this list.";
    elements.resultsList.innerHTML = emptyCard(
      payload ? "No matches" : "Ready when you are",
      payload
        ? "Try a broader query, switch to Any term, or include snapshots and deep search."
        : "The result list will show concise cards here, and selecting one will open its structured details on the right."
    );
    return;
  }

  elements.resultSummary.textContent = `${formatNumber(payload.count)} total result${payload.count === 1 ? "" : "s"} · showing ${formatNumber(payload.start)}-${formatNumber(payload.end)} · page ${formatNumber(payload.page)} of ${formatNumber(payload.total_pages || 1)}.`;

  state.hits.forEach((hit, index) => {
    const card = document.createElement("article");
    card.className = `result-card${state.selectedHit === hit ? " is-selected" : ""}`;
    card.tabIndex = 0;
    card.addEventListener("click", () => selectHit(hit));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectHit(hit);
      }
    });

    const metaClass = hit.scope.includes("unresolved") ? "danger" : hit.scope.includes("documents") ? "signal" : "";
    const chartable = isChartableHit(hit);
    const selectedForChart = chartable && state.selectedChartHits.has(chartKey(hit));
    const chartCopy = chartable ? "Chart" : "Not chartable";
    const chartReason = chartable ? "Add this record to an org chart." : chartDisabledReason(hit);

    card.innerHTML = `
      <div class="result-top">
        <div>
          <h3 class="result-title">${escapeHtml(hit.summary)}</h3>
          <div class="result-summary">${escapeHtml(hit.db_path)}</div>
        </div>
        <span class="meta-pill ${metaClass}">${escapeHtml(prettyScope(hit.scope))}</span>
      </div>
      <div class="result-tool-row">
        <label class="result-pick${chartable ? "" : " is-disabled"}" title="${escapeHtml(chartReason)}">
          <input type="checkbox" ${selectedForChart ? "checked" : ""} ${chartable ? "" : "disabled"} aria-label="Select result for org chart">
          <span>${escapeHtml(chartCopy)}</span>
        </label>
      </div>
      <div class="meta-row">
        <span class="meta-pill">${escapeHtml(hit.db_label)}</span>
        <span class="meta-pill">${escapeHtml(hit.schema_kind)}</span>
        <span class="meta-pill">score ${escapeHtml(String(hit.score))}</span>
        <span class="meta-pill">id ${escapeHtml(hit.record_id)}</span>
      </div>
      ${hit.snippet ? `<p class="snippet">${escapeHtml(hit.snippet)}</p>` : ""}
    `;

    if (index === 0 && !state.selectedHit) {
      state.selectedHit = hit;
    }
    const pickControl = card.querySelector(".result-pick");
    const pickInput = card.querySelector(".result-pick input");
    if (pickControl && pickInput) {
      pickControl.addEventListener("click", (event) => event.stopPropagation());
      pickInput.addEventListener("change", () => {
        toggleChartSelection(hit, pickInput.checked);
      });
    }
    elements.resultsList.append(card);
  });
}

function selectHit(hit) {
  state.selectedHit = hit;
  [...elements.resultsList.querySelectorAll(".result-card")].forEach((card, index) => {
    card.classList.toggle("is-selected", state.hits[index] === hit);
  });
  renderDetailPane();
  syncChartButtons();
}

function renderDetailPane() {
  elements.copyJsonButton.disabled = !state.selectedHit;
  if (!state.selectedHit) {
    elements.detailPane.innerHTML = emptyCard(
      "No record selected",
      "Choose a result card to inspect its structured fields and JSON payload."
    );
    return;
  }

  const hit = state.selectedHit;
  const inspectorPayload = buildInspectorPayload(hit);
  const detailRows = Object.entries(inspectorPayload)
    .filter(([, value]) => shouldIncludeInspectorValue(value))
    .map(([key, value]) => `
      <div class="kv-key">${escapeHtml(key)}</div>
      <div class="kv-value">${escapeHtml(stringifyValue(value))}</div>
    `)
    .join("");

  elements.detailPane.innerHTML = `
    <div class="detail-header">
      <h3>${escapeHtml(hit.summary)}</h3>
      <p>${escapeHtml(hit.db_label)} • ${escapeHtml(prettyScope(hit.scope))} • record ${escapeHtml(hit.record_id)}</p>
    </div>
    <div class="meta-row">
      <span class="meta-pill">${escapeHtml(hit.schema_kind)}</span>
      <span class="meta-pill">score ${escapeHtml(String(hit.score))}</span>
    </div>
    <div class="kv-grid">${detailRows || '<div class="detail-empty-copy">No structured fields to show.</div>'}</div>
    <div class="json-panel">
      <pre>${escapeHtml(JSON.stringify(inspectorPayload, null, 2))}</pre>
    </div>
  `;
}

async function copySelectedJson() {
  if (!state.selectedHit) {
    return;
  }
  try {
    await navigator.clipboard.writeText(JSON.stringify(buildInspectorPayload(state.selectedHit), null, 2));
    showStatus("Copied selected record JSON to the clipboard.");
  } catch (error) {
    showStatus("Clipboard copy failed in this browser context.", true);
  }
}

async function exportResultsAsMarkdown() {
  if (!state.lastSearch) {
    showStatus("Run a search before exporting Markdown.", true);
    return;
  }

  const markdown = buildMarkdownExport(state.lastSearch);
  const filename = buildExportFilename(state.lastSearch);

  try {
    const saveMode = await saveMarkdownDocument(filename, markdown);
    if (saveMode === "cancelled") {
      showStatus("Markdown export cancelled.");
      return;
    }
    showStatus(`Saved Markdown export as ${filename}.`);
  } catch (error) {
    showStatus(`Markdown export failed: ${error.message}`, true);
  }
}

function applyQuickExample(button) {
  const query = button.dataset.query || "";
  const databases = (button.dataset.databases || "").split(",").filter(Boolean);
  const scopes = (button.dataset.scopes || "").split(",").filter(Boolean);
  const deep = button.dataset.deep === "true";

  elements.queryInput.value = query;
  setSelectedCheckboxes("databases", databases);
  renderScopeSelectors();
  setSelectedCheckboxes("scopes", scopes);
  elements.deepInput.checked = deep;
  elements.queryInput.focus();
}

function setSelectedCheckboxes(name, values) {
  const valueSet = new Set(values);
  document.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
    input.checked = valueSet.has(input.value) && !input.disabled;
  });
}

function resetForm() {
  state.searchRequestId += 1;
  state.isSearching = false;
  elements.searchForm.reset();
  setSelectedCheckboxes("databases", state.databases.map((database) => database.label));
  renderScopeSelectors();
  state.hits = [];
  state.selectedHit = null;
  state.lastSearch = null;
  state.lastSearchRequest = null;
  clearChartSelections(false);
  renderResults(null);
  renderDetailPane();
  renderOrgChart();
  hideStatus();
  setSearching(false);
}

function getSelectedDatabases() {
  return [...document.querySelectorAll('input[name="databases"]:checked')].map((input) => input.value);
}

function getSelectedScopes() {
  return [...document.querySelectorAll('input[name="scopes"]:checked')].map((input) => input.value);
}

function selectedDatabaseObjects() {
  const selected = new Set(getSelectedDatabases());
  return state.databases.filter((database) => selected.has(database.label));
}

function showStatus(message, isError = false) {
  elements.searchStatus.hidden = false;
  elements.searchStatus.textContent = message;
  elements.searchStatus.classList.toggle("is-error", Boolean(isError));
}

function hideStatus() {
  elements.searchStatus.hidden = true;
  elements.searchStatus.textContent = "";
  elements.searchStatus.classList.remove("is-error");
}

function setSearching(isSearching) {
  state.isSearching = isSearching;
  elements.searchButton.disabled = isSearching;
  elements.searchButton.textContent = isSearching ? "Searching…" : "Run search";
  syncExportButton();
  syncPaginationButtons();
}

function setHealthStatus(message, isError = false) {
  elements.healthStatus.textContent = message;
  elements.healthStatus.style.color = isError ? "var(--danger)" : "";
}

function emptyCard(title, body) {
  return `
    <div class="empty-state">
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(body)}</p>
    </div>
  `;
}

function syncExportButton() {
  elements.exportMarkdownButton.disabled = state.isSearching || !state.lastSearch;
}

function renderPagination(payload) {
  const containers = [elements.paginationTop, elements.paginationBottom];
  if (!payload || !payload.count || !payload.total_pages || payload.total_pages <= 1) {
    containers.forEach((container) => {
      container.hidden = true;
      container.innerHTML = "";
    });
    return;
  }

  const html = buildPaginationHtml(payload);
  containers.forEach((container) => {
    container.hidden = false;
    container.innerHTML = html;
    container.querySelectorAll("button[data-page]").forEach((button) => {
      button.addEventListener("click", () => {
        const page = Number.parseInt(button.dataset.page, 10);
        if (Number.isFinite(page)) {
          runSearch(page);
        }
      });
    });
  });
  syncPaginationButtons();
}

function buildPaginationHtml(payload) {
  const page = payload.page || 1;
  const totalPages = payload.total_pages || 1;
  const pageButtons = paginationWindow(page, totalPages).map((item) => {
    if (item === "gap") {
      return '<span class="pagination-gap">…</span>';
    }
    return `
      <button class="page-button${item === page ? " is-current" : ""}" type="button" data-page="${item}" data-base-disabled="${item === page ? "true" : "false"}" ${item === page ? "disabled" : ""}>
        ${formatNumber(item)}
      </button>
    `;
  }).join("");

  return `
    <div class="pagination-copy">
      ${formatNumber(payload.count)} total · showing ${formatNumber(payload.start)}-${formatNumber(payload.end)}
    </div>
    <div class="pagination-buttons">
      <button class="page-button" type="button" data-page="1" data-base-disabled="${page <= 1 ? "true" : "false"}" ${page <= 1 ? "disabled" : ""}>First</button>
      <button class="page-button" type="button" data-page="${Math.max(1, page - 1)}" data-base-disabled="${page <= 1 ? "true" : "false"}" ${page <= 1 ? "disabled" : ""}>Previous</button>
      ${pageButtons}
      <button class="page-button" type="button" data-page="${Math.min(totalPages, page + 1)}" data-base-disabled="${page >= totalPages ? "true" : "false"}" ${page >= totalPages ? "disabled" : ""}>Next</button>
      <button class="page-button" type="button" data-page="${totalPages}" data-base-disabled="${page >= totalPages ? "true" : "false"}" ${page >= totalPages ? "disabled" : ""}>Last</button>
    </div>
  `;
}

function paginationWindow(page, totalPages) {
  const pages = new Set([1, totalPages]);
  for (let candidate = page - 2; candidate <= page + 2; candidate += 1) {
    if (candidate >= 1 && candidate <= totalPages) {
      pages.add(candidate);
    }
  }
  const sorted = [...pages].sort((left, right) => left - right);
  const output = [];
  sorted.forEach((item, index) => {
    if (index > 0 && item - sorted[index - 1] > 1) {
      output.push("gap");
    }
    output.push(item);
  });
  return output;
}

function syncPaginationButtons() {
  [elements.paginationTop, elements.paginationBottom].forEach((container) => {
    if (!container) {
      return;
    }
    container.querySelectorAll("button[data-page]").forEach((button) => {
      button.disabled = state.isSearching || button.dataset.baseDisabled === "true";
    });
  });
}

async function fetchJsonWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    const payload = await response.json();
    return { response, payload };
  } catch (error) {
    if (error && error.name === "AbortError") {
      const seconds = Math.round(timeoutMs / 1000);
      throw new Error(`Search timed out after ${seconds} seconds. Try fewer databases, narrower scopes, or turn off deep search.`);
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function syncChartButtons() {
  const manualCount = state.selectedChartHits.size;
  const chartSelections = effectiveChartSelections();
  const selectedCount = chartSelections.length;
  elements.buildOrgChartButton.disabled = state.isChartLoading || selectedCount === 0;
  elements.clearOrgChartButton.disabled = state.isChartLoading || (manualCount === 0 && !state.orgChart);
  elements.buildOrgChartButton.textContent = state.isChartLoading ? "Building…" : "Build chart";
  if (manualCount > 0) {
    elements.chartSelectionSummary.textContent = `${manualCount} selected for org chart.`;
  } else if (state.selectedHit && isChartableHit(state.selectedHit)) {
    elements.chartSelectionSummary.textContent = "Selected record is ready for org chart.";
  } else {
    elements.chartSelectionSummary.textContent = "Select a generic, Jeetu, or Oliver person/status row to chart peers and leaders.";
  }
}

function isChartableHit(hit) {
  if (!chartableDatabases.has(hit.db_label)) {
    return false;
  }
  if (hit.schema_kind === "generic") {
    return (hit.scope === "people" || hit.scope === "snapshots") && Boolean(chartAlias(hit));
  }
  if (hit.schema_kind !== "focused") {
    return false;
  }
  if (hit.scope === "org_edges" || hit.scope === "unresolved_aliases") {
    return false;
  }
  return Boolean(chartAlias(hit));
}

function chartDisabledReason(hit) {
  if (!chartableDatabases.has(hit.db_label)) {
    return "This database is not wired into the org chart builder.";
  }
  if (hit.scope === "documents") {
    return "Document hits do not have reporting-line graph data.";
  }
  if (hit.scope === "org_edges") {
    return "Edge rows describe a relationship; select a person row instead.";
  }
  if (hit.scope === "unresolved_aliases") {
    return "Unresolved aliases do not have a confirmed profile row.";
  }
  if (!chartAlias(hit)) {
    return "This row does not expose a chartable alias.";
  }
  return "This row is not a supported chart source.";
}

function chartAlias(hit) {
  const alias = hit.details && typeof hit.details.alias === "string" ? hit.details.alias : hit.record_id;
  return normalizeAliasForChart(alias);
}

function normalizeAliasForChart(value) {
  if (typeof value !== "string") {
    return "";
  }
  const alias = value.trim().toLowerCase();
  return /^[a-z0-9_.-]+$/.test(alias) ? alias : "";
}

function chartKey(hit) {
  return `${hit.db_label}:${chartAlias(hit)}`;
}

function chartSelectionFromHit(hit) {
  return {
    db_label: hit.db_label,
    alias: chartAlias(hit),
    record_id: hit.record_id,
    summary: hit.summary,
  };
}

function effectiveChartSelections() {
  const explicitSelections = [...state.selectedChartHits.values()];
  if (explicitSelections.length) {
    return explicitSelections;
  }
  if (state.selectedHit && isChartableHit(state.selectedHit)) {
    return [chartSelectionFromHit(state.selectedHit)];
  }
  return [];
}

function toggleChartSelection(hit, selected) {
  if (!isChartableHit(hit)) {
    return;
  }
  const key = chartKey(hit);
  if (selected) {
    state.selectedChartHits.set(key, chartSelectionFromHit(hit));
  } else {
    state.selectedChartHits.delete(key);
  }
  syncChartButtons();
}

function clearChartSelections(rerender = true) {
  state.selectedChartHits.clear();
  state.orgChart = null;
  hideChartStatus();
  syncChartButtons();
  if (rerender) {
    renderResults(state.lastSearch);
    renderOrgChart();
  }
}

async function buildOrgChart() {
  const selections = effectiveChartSelections();
  if (!selections.length) {
    showStatus("Select a generic, Jeetu, or Oliver person/status result before building a chart.", true);
    return;
  }

  state.isChartLoading = true;
  syncChartButtons();
  setChartStatus("Building org chart from local graph data…");

  try {
    const response = await fetch("/api/org-chart", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        selections,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "org chart failed");
    }
    state.orgChart = payload;
    renderOrgChart();
    const chartCount = (payload.charts || []).length;
    setChartStatus(`Built ${chartCount} org chart${chartCount === 1 ? "" : "s"} in ${payload.duration_ms.toFixed(1)} ms.`);
  } catch (error) {
    state.orgChart = null;
    renderOrgChart();
    setChartStatus(error.message, true);
  } finally {
    state.isChartLoading = false;
    syncChartButtons();
  }
}

async function expandDirectReports(dbLabel, alias) {
  const normalizedAlias = normalizeAliasForChart(alias);
  if (!chartableDatabases.has(dbLabel) || !normalizedAlias) {
    setChartStatus("That chart node is missing a chartable database or alias.", true);
    return;
  }

  const selection = {
    db_label: dbLabel,
    alias: normalizedAlias,
    record_id: normalizedAlias,
    summary: normalizedAlias,
  };

  state.isChartLoading = true;
  syncChartButtons();
  setChartStatus(`Loading direct reports for ${normalizedAlias}…`);

  try {
    const response = await fetch("/api/org-chart", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        selections: [selection],
        direct_report_expansions: [selection],
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "direct-report chart failed");
    }
    state.selectedChartHits.clear();
    state.orgChart = payload;
    renderResults(state.lastSearch);
    renderOrgChart();
    const chartCount = (payload.charts || []).length;
    setChartStatus(`Built direct-report view for ${normalizedAlias}: ${chartCount} chart${chartCount === 1 ? "" : "s"} in ${payload.duration_ms.toFixed(1)} ms.`);
  } catch (error) {
    setChartStatus(error.message, true);
  } finally {
    state.isChartLoading = false;
    syncChartButtons();
  }
}

function onOrgChartClick(event) {
  const button = event.target.closest("[data-action='expand-direct-reports']");
  if (!button) {
    return;
  }
  expandDirectReports(button.dataset.dbLabel, button.dataset.alias);
}

function renderOrgChart() {
  if (!state.orgChart) {
    elements.orgChart.innerHTML = emptyCard(
      "No chart yet",
      "Select chartable generic, Jeetu, or Oliver people from the results, then build a peer group and leader chain."
    );
    return;
  }

  const charts = state.orgChart.charts || [];
  const warnings = state.orgChart.warnings || [];
  const warningHtml = warnings.length ? renderChartWarnings(warnings) : "";

  if (!charts.length) {
    elements.orgChart.innerHTML = warningHtml + emptyCard(
      "No chartable selections",
      "The selected rows did not map to local graph-backed people records."
    );
    return;
  }

  elements.orgChart.innerHTML = warningHtml + charts.map(renderSingleChart).join("");
}

function renderSingleChart(chart) {
  const nodesByDepth = groupNodesByDepth(chart.nodes || []);
  const nodeLookup = new Map((chart.nodes || []).map((node) => [node.alias, node]));
  const parentByChild = buildParentByChild(chart.edges || []);
  const levels = [...nodesByDepth.entries()]
    .sort(([left], [right]) => Number(left) - Number(right))
    .map(([depth, nodes]) => renderChartLevel(depth, nodes, nodeLookup, parentByChild, chart.db_label))
    .join("");
  const peerGroups = (chart.peer_groups || []).map((group) => `
    <div class="peer-group-card">
      <strong>${escapeHtml(group.manager_name || group.manager_alias)}</strong>
      <span>${escapeHtml(group.manager_alias)} manager group • ${formatNumber(group.peer_count)} peer${group.peer_count === 1 ? "" : "s"}${group.truncated ? " shown with cap" : ""}</span>
      <span>Selected: ${(group.selected_aliases || []).map(escapeHtml).join(", ")}</span>
    </div>
  `).join("");
  const directReportGroups = (chart.direct_report_groups || []).map((group) => `
    <div class="direct-report-group-card">
      <strong>${escapeHtml(group.manager_name || group.manager_alias)}</strong>
      <span>${escapeHtml(group.manager_alias)} expanded • ${formatNumber(group.direct_report_count)} direct report${group.direct_report_count === 1 ? "" : "s"}${group.truncated ? " shown with cap" : ""}</span>
      ${Number.isFinite(Number(group.captured_direct_report_count)) ? `<span>Captured count: ${formatNumber(group.captured_direct_report_count)}</span>` : ""}
    </div>
  `).join("");
  const warnings = chart.warnings && chart.warnings.length ? renderChartWarnings(chart.warnings) : "";
  return `
    <article class="chart-card">
      <div class="chart-card-head">
        <div>
          <h3>${escapeHtml(chart.root_label)} chain</h3>
          <p>${escapeHtml(chart.db_label)} • root ${escapeHtml(chart.root_alias)} • ${formatNumber((chart.nodes || []).length)} nodes</p>
        </div>
        <span class="meta-pill">${formatNumber((chart.edges || []).length)} edges</span>
      </div>
      ${warnings}
      <div class="peer-groups">${peerGroups}</div>
      ${directReportGroups ? `<div class="direct-report-groups">${directReportGroups}</div>` : ""}
      <div class="chart-levels">${levels}</div>
    </article>
  `;
}

function groupNodesByDepth(nodes) {
  return nodes.reduce((groups, node) => {
    const depth = Number.isFinite(Number(node.depth)) ? Number(node.depth) : 0;
    if (!groups.has(depth)) {
      groups.set(depth, []);
    }
    groups.get(depth).push(node);
    return groups;
  }, new Map());
}

function buildParentByChild(edges) {
  return edges.reduce((parents, edge) => {
    if (edge.child_alias && edge.parent_alias) {
      parents.set(edge.child_alias, edge.parent_alias);
    }
    return parents;
  }, new Map());
}

function groupNodesByParent(nodes, depth, parentByChild) {
  const groups = new Map();
  nodes.forEach((node) => {
    const parentAlias = depth === 0 ? "" : parentByChild.get(node.alias) || "";
    if (!groups.has(parentAlias)) {
      groups.set(parentAlias, []);
    }
    groups.get(parentAlias).push(node);
  });
  return groups;
}

function renderChartLevel(depth, nodes, nodeLookup, parentByChild, dbLabel) {
  const sortedNodes = [...nodes].sort((left, right) => {
    if (left.selected !== right.selected) {
      return left.selected ? -1 : 1;
    }
    return (left.full_name || left.alias).localeCompare(right.full_name || right.alias);
  });
  const parentGroups = [...groupNodesByParent(sortedNodes, depth, parentByChild).entries()]
    .map(([parentAlias, groupNodes]) => renderChartParentGroup(parentAlias, groupNodes, nodeLookup, depth, dbLabel))
    .join("");
  const label = depth === 0 ? "Root" : `Level ${depth}`;
  const levelClass = sortedNodes.length > 3 ? " is-peer-grid" : "";
  return `
    <section class="chart-level${levelClass}">
      <div class="chart-level-label">${escapeHtml(label)}</div>
      <div class="chart-node-row">
        ${parentGroups}
      </div>
    </section>
  `;
}

function renderChartParentGroup(parentAlias, nodes, nodeLookup, depth, dbLabel) {
  const parent = parentAlias ? nodeLookup.get(parentAlias) : null;
  const relationship = parentAlias ? `
    <div class="relationship-label">
      <span>Reports to</span>
      <strong>${escapeHtml((parent && parent.full_name) || parentAlias)}</strong>
      <em>${escapeHtml(parentAlias)}</em>
    </div>
  ` : "";
  return `
    <div class="chart-parent-group">
      ${relationship}
      <div class="chart-node-grid">
        ${nodes.map((node) => renderChartNode(node, dbLabel)).join("")}
      </div>
    </div>
  `;
}

function renderChartNode(node, dbLabel) {
  const title = node.title || "No title captured";
  const organization = node.organization_name || "No org captured";
  const directReports = Number(node.direct_report_count || 0);
  const classes = [
    "chart-node",
    `role-${node.primary_role || "person"}`,
    node.selected ? "is-selected" : "",
    node.missing ? "is-missing" : "",
  ].filter(Boolean).join(" ");
  return `
    <article class="${escapeHtml(classes)}">
      <div class="chart-node-top">
        <strong>${escapeHtml(node.full_name || node.alias)}</strong>
        <span>${escapeHtml(node.alias)}</span>
      </div>
      <p>${escapeHtml(title)}</p>
      <p>${escapeHtml(organization)}</p>
      <div class="meta-row compact-meta">
        <span class="meta-pill">${escapeHtml(node.primary_role || "person")}</span>
        ${directReports > 0 ? `<span class="meta-pill">${formatNumber(directReports)} directs</span>` : ""}
        ${node.location_text ? `<span class="meta-pill">${escapeHtml(node.location_text)}</span>` : ""}
      </div>
      ${directReports > 0 ? `
        <button
          class="node-action"
          type="button"
          data-action="expand-direct-reports"
          data-db-label="${escapeHtml(dbLabel)}"
          data-alias="${escapeHtml(node.alias)}"
        >
          Show directs
        </button>
      ` : ""}
    </article>
  `;
}

function renderChartWarnings(warnings) {
  return `
    <div class="chart-warnings">
      ${warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join("")}
    </div>
  `;
}

function setChartStatus(message, isError = false) {
  elements.chartStatus.hidden = false;
  elements.chartStatus.textContent = message;
  elements.chartStatus.classList.toggle("is-error", Boolean(isError));
}

function hideChartStatus() {
  elements.chartStatus.hidden = true;
  elements.chartStatus.textContent = "";
  elements.chartStatus.classList.remove("is-error");
}

function prettyScope(scope) {
  return (scopeInfo[scope] && scopeInfo[scope].label) || scope.replace(/_/g, " ");
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US").format(Number(value));
}

function stringifyValue(value) {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

function buildInspectorPayload(hit) {
  const payload = {
    summary: hit.summary,
    snippet: hit.snippet,
    db_label: hit.db_label,
    db_path: hit.db_path,
    schema_kind: hit.schema_kind,
    scope: hit.scope,
    record_id: hit.record_id,
    score: hit.score,
  };

  Object.entries(hit.details || {}).forEach(([key, value]) => {
    payload[key] = value;
  });

  return Object.fromEntries(
    Object.entries(payload).filter(([, value]) => shouldIncludeInspectorValue(value))
  );
}

function shouldIncludeInspectorValue(value) {
  if (value === null || value === undefined) {
    return false;
  }
  if (typeof value === "number") {
    return value !== 0;
  }
  if (typeof value === "string") {
    return value.trim() !== "";
  }
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  if (typeof value === "object") {
    return Object.keys(value).length > 0;
  }
  return true;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function buildMarkdownExport(payload) {
  const lines = [];
  const results = payload.results || [];
  lines.push("# Cisco Org Search Export");
  lines.push("");
  lines.push(`- Exported: ${payload.exported_at || new Date().toISOString()}`);
  lines.push(`- Query: ${flattenText(payload.query)}`);
  lines.push(`- Databases: ${(payload.databases || []).join(", ") || "none"}`);
  lines.push(`- Scopes: ${payload.scopes && payload.scopes.length ? payload.scopes.map(prettyScope).join(", ") : "default scopes"}`);
  lines.push(`- Match mode: ${payload.mode}`);
  lines.push(`- Deep search: ${payload.deep ? "true" : "false"}`);
  lines.push(`- Page size: ${payload.page_size || payload.limit}`);
  lines.push(`- Page: ${payload.page || 1} of ${payload.total_pages || 1}`);
  lines.push(`- Total results: ${payload.count}`);
  lines.push(`- Results on this page: ${payload.result_count || results.length}`);
  if (payload.result_count) {
    lines.push(`- Showing: ${payload.start}-${payload.end}`);
  }
  lines.push(`- Search duration ms: ${payload.duration_ms}`);
  lines.push("");
  lines.push("## Results");
  lines.push("");

  if (!results.length) {
    lines.push("No matches were returned for this search.");
    return lines.join("\n");
  }

  results.forEach((hit, index) => {
    lines.push(`### ${index + 1}. ${flattenText(hit.summary)}`);
    lines.push("");
    lines.push(`- Database: ${hit.db_label}`);
    lines.push(`- Scope: ${prettyScope(hit.scope)}`);
    lines.push(`- Schema: ${hit.schema_kind}`);
    lines.push(`- Record ID: ${hit.record_id}`);
    lines.push(`- Score: ${hit.score}`);
    lines.push(`- Source DB: ${hit.db_path}`);
    if (hit.snippet) {
      lines.push(`- Snippet: ${flattenText(hit.snippet)}`);
    }
    lines.push("");
    lines.push("#### Details");
    lines.push("");
    lines.push("```json");
    lines.push(JSON.stringify(hit.details || {}, null, 2));
    lines.push("```");
    lines.push("");
  });

  return lines.join("\n");
}

function buildExportFilename(payload) {
  const stamp = buildTimestampForFilename(new Date());
  const querySlug = slugify(payload.query || "search");
  return `cisco-org-search-${querySlug}-${stamp}.md`;
}

function buildTimestampForFilename(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  const second = String(date.getSeconds()).padStart(2, "0");
  return `${year}${month}${day}-${hour}${minute}${second}`;
}

function slugify(value) {
  const slug = flattenText(value)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return slug || "search";
}

function flattenText(value) {
  return String(value).replace(/\s+/g, " ").trim();
}

async function saveMarkdownDocument(filename, markdown) {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  if (window.showSaveFilePicker) {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [
          {
            description: "Markdown document",
            accept: {
              "text/markdown": [".md"],
              "text/plain": [".md"],
            },
          },
        ],
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return "picker";
    } catch (error) {
      if (error && error.name === "AbortError") {
        return "cancelled";
      }
      throw error;
    }
  }
  triggerMarkdownDownload(blob, filename);
  return "download";
}

function triggerMarkdownDownload(blob, filename) {
  const blobUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 0);
}
