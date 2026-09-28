// Admits a new voting execution on an AWS stoffel-ec2-user-deployment-persistent mesh via its
// self-service API (POST /executions), then builds the exact link index.html needs to reach it:
// the execution ID plus every *_browser endpoint (the coordinator/party listeners a browser can
// actually use - see stoffel-mpc-coordinator's browser_rpc module). This page never touches any
// MPC protocol itself; it only talks to the REST API. Nothing here runs against the local
// docker-compose demo - that one bakes its single execution into the coordinator at container
// startup (see ../compose.yaml) and has no admission API to call.

const form = document.querySelector("#start-form");
const startButton = document.querySelector("#start-button");
const apiUrlInput = document.querySelector("#api-url");
const apiKeyInput = document.querySelector("#api-key");
const loadVotersButton = document.querySelector("#load-voters-button");
const voterSelects = [
  document.querySelector("#voter-0"),
  document.querySelector("#voter-1"),
  document.querySelector("#voter-2"),
];
const statusState = document.querySelector("#network-state");
const statusWrap = document.querySelector(".network-state");
const resultPanel = document.querySelector("#result-panel");
const resultExecutionIdInput = document.querySelector("#result-execution-id");
const voteLinkInput = document.querySelector("#vote-link");
const resultError = document.querySelector("#result-error");
const cancelledNote = document.querySelector("#cancelled-note");
const startButtonLabel = startButton.querySelector("span:first-child");
const voteQrCode = document.querySelector("#vote-qr-code");

// The QRCode instance (from ./qrcode.js, loaded as a plain global script before this
// module - see start.html) - created once, then reused via .makeCode() for every
// subsequent election this tab starts, per that library's own documented pattern.
let qrCode = null;

// Set once an election is successfully started - this page admits at most one execution per
// tab, and #start-button itself becomes the cancel action from that point on (see the submit
// handler below), rather than a separate button, so it needs to know what to cancel and with
// which credentials once repurposed.
let activeApiUrl;
let activeApiKey;
let activeExecutionId;

// Persists the API key across a page refresh - #api-key has autocomplete="off" (deliberately
// opting out of the browser's own autofill/credential storage), and otherwise nothing else
// remembers it, so a reload would mean retyping it every time. Plain localStorage, not a
// secrets vault - fine for this operator convenience, but worth knowing if that matters to
// how this page gets used.
const API_KEY_STORAGE_KEY = "stoffel:start:api-key";
try {
  const savedApiKey = localStorage.getItem(API_KEY_STORAGE_KEY);
  if (savedApiKey) apiKeyInput.value = savedApiKey;
} catch (_) {
  // Storage can be unavailable (private browsing, blocked site data) - just skip restoring.
}
apiKeyInput.addEventListener("input", () => {
  try {
    if (apiKeyInput.value) localStorage.setItem(API_KEY_STORAGE_KEY, apiKeyInput.value);
    else localStorage.removeItem(API_KEY_STORAGE_KEY);
  } catch (_) {
    // Ignore storage failures - the field still works, it just won't survive a reload.
  }
});

// Both the execution ID and the vote link are shown the same way: a readonly
// input next to a button that copies it, so wire both pairs through one helper.
function setupCopyButton(buttonId, input) {
  const button = document.querySelector(buttonId);
  button.addEventListener("click", async () => {
    input.select();
    try {
      await navigator.clipboard.writeText(input.value);
      button.textContent = "Copied";
      window.setTimeout(() => { button.textContent = "Copy"; }, 1500);
    } catch (_) {
      // Clipboard permission can be denied; the input is already selected as a fallback.
    }
  });
}
setupCopyButton("#copy-execution-id", resultExecutionIdInput);
setupCopyButton("#copy-link", voteLinkInput);

function setStatus(label, state = "") {
  statusState.textContent = label;
  statusWrap.classList.toggle("ready", state === "ready");
  statusWrap.classList.toggle("error", state === "error");
}

function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

// Duplicated verbatim from stoffel-browser-client.js rather than imported - that module's
// top-level import of the compiled WASM client would drag it into this page for no reason;
// this page never needs it for anything else.
function hexToExecutionIdBytes(executionIdHex) {
  if (!/^[0-9a-fA-F]{64}$/.test(executionIdHex)) {
    throw new Error("execution id must be 64 hex characters");
  }
  return Array.from({ length: 32 }, (_, index) =>
    Number.parseInt(executionIdHex.slice(index * 2, index * 2 + 2), 16),
  );
}

async function apiCall(apiUrl, apiKey, method, path, body) {
  const response = await fetch(`${apiUrl.replace(/\/+$/, "")}${path}`, {
    method,
    headers: {
      "x-api-key": apiKey,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `${method} ${path} returned HTTP ${response.status}`);
  }
  return payload;
}

// Populates the three voter selects from GET /client-registrations (operator-only - requires
// the same API key, unlike the public registration link's own POST). Only registered *and*
// materialized clients are offered at all (see list_client_registrations.py) - anything else
// would fail admission's resolve_admission lookup if picked, since its .crt file wouldn't
// exist on the parties yet.
function optionLabel(client) {
  const when = new Date(client.created_at).toLocaleString();
  return client.label ? `${client.label} (${client.client_name}) · ${when}` : `${client.client_name} · ${when}`;
}

function updateStartButtonEnabled() {
  const values = voterSelects.map((select) => select.value);
  const allChosen = values.every((value) => value !== "");
  const allDistinct = new Set(values).size === values.length;
  startButton.disabled = !(allChosen && allDistinct);
  if (allChosen && !allDistinct) {
    resultError.textContent = "Each voter must be a different registered identity.";
    resultError.hidden = false;
  } else {
    resultError.hidden = true;
  }
}
voterSelects.forEach((select) => select.addEventListener("change", updateStartButtonEnabled));

async function loadRegisteredVoters(apiUrl, apiKey) {
  setStatus("Loading registered voters");
  const { clients } = await apiCall(apiUrl, apiKey, "GET", "/client-registrations");
  if (clients.length < 3) {
    throw new Error(
      `Only ${clients.length} registered-and-materialized voter(s) available - need at least 3. ` +
      "See ./generate-registration-link, ./materialize-registered-clients, and ./deploy.",
    );
  }
  voterSelects.forEach((select) => {
    select.innerHTML = "";
    select.appendChild(new Option("Choose a voter…", ""));
    for (const client of clients) {
      select.appendChild(new Option(optionLabel(client), client.client_name));
    }
    select.disabled = false;
  });
  updateStartButtonEnabled();
  setStatus(`Loaded ${clients.length} registered voter(s)`, "ready");
}

loadVotersButton.addEventListener("click", async () => {
  const apiUrl = apiUrlInput.value.trim();
  const apiKey = apiKeyInput.value.trim();
  if (!apiUrl || !apiKey) {
    setStatus("Enter the API URL and key first", "error");
    return;
  }
  loadVotersButton.disabled = true;
  resultError.hidden = true;
  try {
    await loadRegisteredVoters(apiUrl, apiKey);
  } catch (error) {
    console.error(error);
    setStatus("Could not load registered voters", "error");
    resultError.textContent = error.message;
    resultError.hidden = false;
  } finally {
    loadVotersButton.disabled = false;
  }
});

// Shared by voteLinkFor and the round-watch below - both need the same coordinator/party
// endpoints, resolved the same way.
function resolvedEndpoints(status) {
  // Prefer the hosted site's own config.json (fixed Elastic IPs, identical for every
  // execution - see aws-deployment/stoffel-ec2-user-deployment-persistent's app.py
  // _add_web_site) over the admission response's endpoints field, falling back to
  // the latter only when this page isn't served with a config.json at all.
  const endpoints = hostedConfig?.endpoints || status.endpoints || {};
  const required = [
    "coordinator_browser",
    "party0_browser",
    "party1_browser",
    "party2_browser",
    "party3_browser",
    "party4_browser",
  ];
  const missing = required.filter((key) => !endpoints[key]);
  if (missing.length > 0) {
    throw new Error(
      `The API response is missing ${missing.join(", ")}. The mesh may not have browser TLS ` +
      "enabled yet - see ./upload-browser-tls-cert and ./deploy.",
    );
  }
  return endpoints;
}

function voteLinkFor(status, endpoints) {
  const url = new URL("index.html", window.location.href);
  url.searchParams.set("execution", status.execution_id);
  url.searchParams.set("coordinator", endpoints.coordinator_browser);
  for (let index = 0; index < 5; index += 1) {
    url.searchParams.set(`node${index}`, endpoints[`party${index}_browser`]);
  }
  return url.toString();
}

// Tracks the in-flight round-watch (one WebSocket + polling interval) for the currently
// active election, if any, so it can be torn down cleanly on cancel or before starting a
// fresh one.
let activeRoundWatch = null;

function stopWatchingForProgramFinished() {
  if (!activeRoundWatch) return;
  window.clearInterval(activeRoundWatch.intervalId);
  activeRoundWatch.socket.close();
  activeRoundWatch = null;
}

// Polls the coordinator's own browser_round RPC directly over WebSocket - unauthenticated
// (see stoffel-mpc-coordinator's browser_rpc module), since this page has no WebAuthn-bound
// identity of its own to call the voter-facing signed RPCs with, and GET /executions/{id}
// never reports anything past RUNNING (see app.py's _add_orchestration_lambdas comment).
// Best-effort: any connection failure just stops the watch silently (console-logged) rather
// than surfacing an error to the operator - this is a status indicator, not a critical path,
// so no reconnect-with-backoff machinery is worth building for a first version.
function watchForProgramFinished(coordinatorBrowserEndpoint, executionIdHex) {
  stopWatchingForProgramFinished();
  let nextId = 1;
  const socket = new WebSocket(`wss://${coordinatorBrowserEndpoint}`);

  const poll = () => {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({
      jsonrpc: "2.0",
      id: nextId++,
      method: "browser_round",
      params: [{ execution_id: hexToExecutionIdBytes(executionIdHex) }],
    }));
  };

  socket.addEventListener("open", poll);

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      console.error("browser_round: could not parse response", error);
      return;
    }
    if (message.error) {
      console.error("browser_round failed - stopping the round watch", message.error);
      stopWatchingForProgramFinished();
      return;
    }
    if (message.result === "ProgramFinished") {
      stopWatchingForProgramFinished();
      startButton.disabled = true;
      startButtonLabel.textContent = "Election succeeded";
      setStatus("Election succeeded", "ready");
    }
  });

  socket.addEventListener("error", (event) => {
    console.error("browser_round watch connection error", event);
  });

  socket.addEventListener("close", () => {
    if (activeRoundWatch && activeRoundWatch.socket === socket) {
      window.clearInterval(activeRoundWatch.intervalId);
      activeRoundWatch = null;
    }
  });

  activeRoundWatch = { socket, intervalId: window.setInterval(poll, 3000) };
}

async function startElection(apiUrl, apiKey) {
  setStatus("Submitting to the mesh");
  // Each selected voter fills exactly one manifest slot, in the order shown (Voter 1 -> slot
  // 0, etc.) - see the design discussion on how {certificate, manifest_slot} admission works:
  // this is a one-off, per-execution assignment, unrelated to how the client got registered.
  const clients = voterSelects.map((select, slot) => ({
    certificate: `${select.value}.crt`,
    manifest_slot: slot,
  }));
  const submitted = await apiCall(apiUrl, apiKey, "POST", "/executions", {
    program_name: "voting",
    clients,
  });

  let status = submitted;
  const started = Date.now();
  while (status.status === "PREPARING" || status.status === undefined) {
    if (Date.now() - started > 180_000) throw new Error("Timed out waiting for admission");
    setStatus("Waiting for admission (PREPARING)");
    await sleep(3000);
    status = await apiCall(apiUrl, apiKey, "GET", `/executions/${submitted.execution_id}`);
  }

  if (status.status !== "RUNNING") {
    throw new Error(`Execution ended up ${status.status}${status.error ? `: ${status.error}` : ""}`);
  }
  return status;
}

// Once this tab has admitted an execution, everything that could change it (which voters,
// which deployment/key) is locked in - greyed out rather than removed, so the choice stays
// visible - and #start-button itself is repurposed into the cancel action instead of a
// second, separate button, since this page only ever admits one execution per tab.
function lockInStartedElection() {
  apiUrlInput.disabled = true;
  apiKeyInput.disabled = true;
  loadVotersButton.disabled = true;
  voterSelects.forEach((select) => { select.disabled = true; });
  startButton.type = "button";
  startButton.disabled = false;
  startButtonLabel.textContent = "Cancel this election";
  // {once: true} so cancelling and starting another election in the same tab doesn't
  // accumulate a second listener - lockInStartedElection() runs again for every election
  // this tab starts, and each prior listener already fired (and removed itself) via its
  // own cancel click before a new one is ever added.
  startButton.addEventListener("click", cancelActiveElection, { once: true });
}

// After a cancel, this tab can start another election right away with the same API
// URL/key - those fields were only ever disabled, never cleared, so their values are
// already intact; this just makes the form usable again and clears the voter picks so
// each new election gets an explicit, conscious choice rather than silently reusing the
// previous one's.
function resetForNewElection() {
  apiUrlInput.disabled = false;
  apiKeyInput.disabled = false;
  loadVotersButton.disabled = false;
  voterSelects.forEach((select) => {
    select.selectedIndex = 0;
    // Only re-enable a select that's actually been populated with real choices already
    // (loadRegisteredVoters replaces the placeholder-only option list) - otherwise leave
    // it disabled with its "Load registered voters first" placeholder, same as initial load.
    select.disabled = select.options.length <= 1;
  });
  startButton.type = "submit";
  startButtonLabel.textContent = "Start election";
  updateStartButtonEnabled();
  activeApiUrl = undefined;
  activeApiKey = undefined;
  activeExecutionId = undefined;
  qrCode?.clear();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const apiUrl = apiUrlInput.value.trim();
  const apiKey = apiKeyInput.value.trim();
  startButton.disabled = true;
  resultPanel.hidden = true;
  resultError.hidden = true;
  cancelledNote.hidden = true;
  try {
    const status = await startElection(apiUrl, apiKey);
    const endpoints = resolvedEndpoints(status);
    const link = voteLinkFor(status, endpoints);
    resultExecutionIdInput.value = status.execution_id;
    voteLinkInput.value = link;
    if (qrCode) qrCode.makeCode(link);
    else qrCode = new QRCode(voteQrCode, { text: link, width: 180, height: 180 });
    resultPanel.hidden = false;
    setStatus("Election running", "ready");

    activeApiUrl = apiUrl;
    activeApiKey = apiKey;
    activeExecutionId = status.execution_id;
    lockInStartedElection();
    watchForProgramFinished(endpoints.coordinator_browser, status.execution_id);
  } catch (error) {
    console.error(error);
    setStatus("Could not start the election", "error");
    resultError.textContent = error.message;
    resultError.hidden = false;
    startButton.disabled = false;
  }
});

// Only ever wired up once, by lockInStartedElection(), after a successful start - matching
// this same apiUrl/apiKey's cancel-execution ownership (see cancel_execution.py's
// api_key_id check), since it's the same key that admitted it.
async function cancelActiveElection() {
  startButton.disabled = true;
  startButtonLabel.textContent = "Cancelling…";
  resultError.hidden = true;
  try {
    await apiCall(activeApiUrl, activeApiKey, "POST", `/executions/${activeExecutionId}/cancel`);
    stopWatchingForProgramFinished();
    setStatus("Election cancelled - ready to start another", "ready");
    resultPanel.hidden = true;
    cancelledNote.hidden = false;
    resetForNewElection();
  } catch (error) {
    console.error(error);
    resultError.textContent = error.message;
    resultError.hidden = false;
    startButton.disabled = false;
    startButtonLabel.textContent = "Cancel this election";
  }
}

// config.json only exists when this page is served from the
// stoffel-ec2-user-deployment-persistent CDK stack's own hosted site (see
// that repo's app.py _add_web_site/BucketDeployment) - it never exists for
// this copy or any other way of serving it, so a missing file (or any other
// fetch failure) is expected, not an error: leave the field for the operator
// to fill in by hand, same as before this existed. Its endpoints are read by
// voteLinkFor above; apiUrl just prefills the form.
let hostedConfig = null;
(async () => {
  try {
    const response = await fetch("./config.json");
    if (!response.ok) return;
    hostedConfig = await response.json();
    if (hostedConfig.apiUrl && !apiUrlInput.value) {
      apiUrlInput.value = hostedConfig.apiUrl;
    }
  } catch (_) {
    // Not hosted by that deployment (or offline) - nothing to prefill.
  }
})();

setStatus("Enter deployment details");
