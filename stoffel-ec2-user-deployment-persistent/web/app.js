import { StoffelBrowserClient } from "./stoffel-browser-client.js";

const DEFAULT_EXECUTION = "0000000000000000000000000000000000000000000000000000000000000001";
const PARTY_COUNT = 5;
const THRESHOLD = 1;
const VOTER_COUNT = 3;
const I64 = Object.freeze({ kind: "signed_integer", bit_length: 64 });

const form = document.querySelector("#ballot");
const castButton = document.querySelector("#cast");
const networkState = document.querySelector("#network-state");
const networkWrap = document.querySelector(".network-state");
const resultElement = document.querySelector("#result");
const resultProof = document.querySelector("#result-proof");
const formNote = document.querySelector("#form-note");
const voterRole = document.querySelector("#voter-role");
const unlockButton = document.querySelector("#unlock-identity");
const voteChoice = document.querySelector(".vote-choice");
const voteInputs = Array.from(document.querySelectorAll('input[name="vote"]'));
const terminals = Array.from({ length: VOTER_COUNT }, (_, index) =>
  document.querySelector(`[data-voter="${index}"]`),
);

let client;
let executionId;
let voterSlot;
let inputIndex;

function localEndpoint(port) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:${port}`;
}

// AWS mode: start.html hands off ?coordinator=<host:port>&node0=<host:port>...&node4=<host:port>
// (the *_browser endpoints POST /executions returns) via the link it builds. Those are always
// TLS (see stoffel-mpc-coordinator's browser_rpc module - the AWS mesh's browser listener only
// ever speaks TLS, unlike this page's own possibly-plain-http origin), so wss: is forced
// regardless of window.location.protocol. Local mode (the default) is unchanged: every node
// lives on window.location.hostname at a fixed port, matching examples/voting/compose.yaml.
function resolveEndpoints() {
  const params = new URLSearchParams(window.location.search);
  const coordinatorParam = params.get("coordinator");
  const nodeParams = [0, 1, 2, 3, 4].map((index) => params.get(`node${index}`));
  if (coordinatorParam && nodeParams.every(Boolean)) {
    return {
      coordinator: `wss://${coordinatorParam}`,
      nodes: nodeParams.map((hostPort) => `wss://${hostPort}`),
    };
  }
  return {
    coordinator: localEndpoint(31426),
    nodes: [17480, 17481, 17482, 17483, 17484].map((port) => localEndpoint(port)),
  };
}

function setNetwork(label, state = "") {
  networkState.textContent = label;
  networkWrap.classList.toggle("ready", state === "ready");
  networkWrap.classList.toggle("error", state === "error");
}

function setLedger(name, state, label) {
  const item = document.querySelector(`[data-step="${name}"]`);
  item.classList.toggle("active", state === "active");
  item.classList.toggle("done", state === "done");
  item.querySelector("em").textContent = label;
}

function renderRole(slot) {
  voterRole.textContent = `Voter ${slot + 1} · seat ${slot}`;
  terminals[slot].classList.add("you");
  terminals[slot].querySelector('[data-role="state"]').textContent = "this tab";
}

function renderOwnProgress(status) {
  const terminal = terminals[voterSlot];
  const sealed = status.own_input_submitted;
  terminal.querySelector('[data-role="ballot"]').textContent = sealed ? "sealed" : "—";
  terminal.querySelector('[data-role="state"]').textContent = sealed ? "sealed" : "this tab";
  terminal.classList.toggle("ready", sealed);
  setLedger(
    "rendezvous",
    status.submitted_clients >= status.total_clients ? "done" : "active",
    `${status.submitted_clients} / ${status.total_clients}`,
  );
}

async function unlockIdentity() {
  unlockButton.disabled = true;
  setNetwork("Connecting to the election");
  const endpoints = resolveEndpoints();
  client = new StoffelBrowserClient({
    executionId,
    coordinatorUrl: endpoints.coordinator,
    partyUrls: endpoints.nodes,
    parties: PARTY_COUNT,
    threshold: THRESHOLD,
    // Reconnect across a tab close/reload matters more here than the tighter memory-only
    // exposure bound - see the design plan's persistence-mode tradeoff. A future project
    // wanting the narrower guarantee would omit this (memory-only is the library default).
    persistence: "indexeddb",
  });
  await client.connect({ onWaiting: () => setNetwork("Waiting for MPC services") });

  setNetwork("Unlock with your device");
  await client.bind();

  const status = await client.executionStatus();
  if (status.total_inputs !== VOTER_COUNT || status.input_indices.length !== 1) {
    throw new Error("This identity is not assigned a ballot in this election");
  }
  voterSlot = status.input_indices[0];
  inputIndex = voterSlot;
  renderRole(voterSlot);
  renderOwnProgress(status);
  setLedger("identity", "done", `Voter ${voterSlot + 1}`);
  setNetwork(`Voter ${voterSlot + 1} ready`, "ready");
  voteChoice.disabled = false;
  castButton.disabled = false;
}

async function boot() {
  executionId = new URLSearchParams(window.location.search).get("execution") || DEFAULT_EXECUTION;
  setNetwork("Ready to unlock");
}

async function castVote(vote) {
  setLedger("masks", "active", "reserving");
  const typedInputs = [{ share_type: I64, value: { kind: "signed_integer", value: BigInt(vote) } }];
  await client.submitInputs(inputIndex, typedInputs, {
    onWaiting: (label) => setNetwork(label),
  });
  setLedger("masks", "done", "shares combined");
  terminals[voterSlot].querySelector('[data-role="ballot"]').textContent = "sealed";
  terminals[voterSlot].classList.add("ready");

  setLedger("rendezvous", "active", "waiting for other voters");
  await pollUntilAllSubmitted();

  setLedger("output", "active", "tallying");
  const outputs = await client.getOutputs([I64], { onWaiting: (label) => setNetwork(label) });
  if (outputs[0]?.kind !== "signed_integer") throw new Error("The election returned an unexpected output type");
  setLedger("output", "done", "decrypted in all three tabs");
  return outputs[0].value;
}

async function pollUntilAllSubmitted() {
  for (;;) {
    const status = await client.executionStatus();
    renderOwnProgress(status);
    if (status.submitted_clients >= status.total_clients) return;
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

unlockButton.addEventListener("click", async () => {
  try {
    await unlockIdentity();
  } catch (error) {
    console.error(error);
    unlockButton.disabled = false;
    setNetwork("Identity or services unavailable", "error");
    resultProof.textContent = error.message;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const vote = Number(new FormData(form).get("vote"));
  castButton.disabled = true;
  voteInputs.forEach((input) => { input.disabled = true; });
  castButton.querySelector("span:first-child").textContent = "Waiting for all three browsers";
  resultElement.textContent = "···";
  resultProof.textContent = "Your vote stays inside this tab";
  try {
    const tally = await castVote(vote);
    resultElement.textContent = tally.toString();
    resultProof.textContent = "All three voters reconstructed the same MPC-computed tally";
    setNetwork("Tally complete", "ready");
    castButton.querySelector("span:first-child").textContent = "Tally complete";
    formNote.textContent = "The individual votes stayed secret-shared throughout. Restart the stack for another election.";

    // The execution has reached a terminal state for this tab (the tally is decrypted) -
    // bound the persisted session's lifetime to "this election," not forever, matching the
    // design plan's IndexedDB-mode scoping. Best-effort: a storage error here shouldn't hide
    // the tally that was already successfully shown above.
    try {
      await client.identity.forget(executionId);
    } catch (cleanupError) {
      console.error("failed to clear the persisted session after voting", cleanupError);
    }
  } catch (error) {
    console.error(error);
    setNetwork("Tally stopped", "error");
    resultElement.textContent = "—";
    resultProof.textContent = error.message;
    castButton.disabled = false;
    voteInputs.forEach((input) => { input.disabled = false; });
    castButton.querySelector("span:first-child").textContent = "Try again";
  }
});

castButton.disabled = true;
boot().catch((error) => {
  console.error(error);
  setNetwork("Setup failed", "error");
  resultProof.textContent = error.message;
});
