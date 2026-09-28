// The transport/orchestration layer of the shared browser client library. A consuming
// site imports only this module (and `WebauthnSession` if it wants to configure
// persistence - see below) and never touches `StoffelWasmClient`, raw WebSocket
// connections, `SignedBrowserRequest` construction, or WebCrypto calls directly at all.
//
// Scoped, for now, to the one-shot pattern voting and calculator both actually use: one
// fixed execution id per page session, inputs masked and submitted once, outputs decrypted
// once. Battleship and poker's multi-execution, turn-based pattern is a different shape
// (see the design plan's "Scope" section) and isn't supported here.

import init, { StoffelWasmClient, authenticationMessage } from "./pkg/stoffel_wasm_client.js";
import { WebauthnSession } from "./webauthn-session.js";

const textEncoder = new TextEncoder();

let wasmInitPromise = null;
function ensureWasmInit() {
  if (!wasmInitPromise) wasmInitPromise = init();
  return wasmInitPromise;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** Generic "retry until truthy or timeout" helper - identical idiom to what
 * voting/calculator/battleship's own `app.js` files already hand-roll. */
async function poll(label, action, { timeoutMs = 180_000, intervalMs = 700, onWaiting } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const value = await action();
    if (value !== null && value !== undefined && value !== false) return value;
    if (onWaiting) onWaiting(label);
    await sleep(intervalMs);
  }
  throw new Error(`Timed out while ${label.toLowerCase()}`);
}

/** Minimal id-correlated JSON-RPC-over-WebSocket client - the same shape voting/
 * calculator/battleship's own `app.js` files already hand-roll independently. */
class JsonRpcSocket {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.socket = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(this.url);
      this.socket = socket;
      socket.addEventListener("open", () => resolve(), { once: true });
      socket.addEventListener("error", () => reject(new Error(`Cannot connect to ${this.url}`)), {
        once: true,
      });
      socket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data);
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
      });
      socket.addEventListener("close", () => {
        for (const pending of this.pending.values()) pending.reject(new Error(`${this.url} closed`));
        this.pending.clear();
      });
    });
  }

  async connectWithRetry({ timeoutMs = 120_000, onWaiting } = {}) {
    const started = Date.now();
    for (;;) {
      try {
        await this.connect();
        return;
      } catch (error) {
        if (Date.now() - started >= timeoutMs) throw new Error(`Cannot connect to ${this.url}`);
        if (onWaiting) onWaiting();
        await sleep(700);
      }
    }
  }

  call(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }
}

function toPlain(value) {
  if (typeof value === "bigint") return Number(value);
  if (ArrayBuffer.isView(value)) return Array.from(value);
  if (Array.isArray(value)) return value.map(toPlain);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, toPlain(entry)]));
  }
  return value;
}

function toBytesArray(bytes) {
  return Array.from(bytes);
}

function hexToExecutionIdBytes(executionIdHex) {
  if (!/^[0-9a-fA-F]{64}$/.test(executionIdHex)) {
    throw new Error("execution id must be 64 hex characters");
  }
  return Array.from({ length: 32 }, (_, index) =>
    Number.parseInt(executionIdHex.slice(index * 2, index * 2 + 2), 16),
  );
}

/** One server connection's worth of session state: the JSON-RPC socket plus this
 * session's role label (matches whatever key `WebauthnSession.recordBinding`/
 * `getBinding` used for it - e.g. "coordinator", "party0"). */
class BoundConnection {
  constructor(role, socket) {
    this.role = role;
    this.socket = socket;
  }
}

export class StoffelBrowserClient {
  /**
   * @param {object} options
   * @param {string} options.executionId 64-hex-character execution id
   * @param {string} options.coordinatorUrl wss:// URL
   * @param {string[]} options.partyUrls wss:// URLs, one per MPC party
   * @param {number} options.parties total party count (topology, not partyUrls.length -
   *   kept separate since a browser client only ever opens direct connections to the
   *   parties it needs to, not necessarily all of them)
   * @param {number} options.threshold
   * @param {"memory"|"indexeddb"} [options.persistence] passed straight to
   *   `WebauthnSession` unless `options.identity` is given instead
   * @param {WebauthnSession} [options.identity] an already-constructed `WebauthnSession`,
   *   for a caller that wants to share one across multiple `StoffelBrowserClient`s or
   *   configure it beyond what `persistence` alone covers
   */
  constructor({
    executionId,
    coordinatorUrl,
    partyUrls,
    parties,
    threshold,
    persistence,
    identity,
  }) {
    this.executionId = executionId;
    this.coordinatorUrl = coordinatorUrl;
    this.partyUrls = partyUrls;
    this.parties = parties;
    this.threshold = threshold;
    this.maskShareQuorum = 2 * threshold + 1;
    this.identity = identity ?? new WebauthnSession({ persistence });

    this.coordinator = null;
    this.partyConnections = [];
    this.wasmClient = null;
    this.wasmExecution = null;
  }

  /** Opens every configured connection (coordinator + parties) with retry. Call once,
   * before `bind()`. */
  async connect({ onWaiting } = {}) {
    this.coordinator = new JsonRpcSocket(this.coordinatorUrl);
    this.partyConnections = this.partyUrls.map((url, index) => ({
      role: `party${index}`,
      socket: new JsonRpcSocket(url),
    }));
    await Promise.all([
      this.coordinator.connectWithRetry({ onWaiting: () => onWaiting?.("coordinator") }),
      ...this.partyConnections.map((party) =>
        party.socket.connectWithRetry({ onWaiting: () => onWaiting?.(party.role) }),
      ),
    ]);
  }

  /** Runs the WebAuthn ceremony (or resumes a stored session - see `WebauthnSession`'s
   * persistence mode) and binds to every connected server. One interactive prompt at most,
   * regardless of how many parties are configured. Must be called after `connect()`. */
  async bind() {
    await ensureWasmInit();

    const material = await this.identity.getBindMaterial(this.executionId);
    const assertionPayload = {
      authenticator_data: toBytesArray(material.assertion.authenticatorData),
      client_data_json: toBytesArray(material.assertion.clientDataJson),
      signature: toBytesArray(material.assertion.signature),
    };
    const ecdsaPublicKeyBytes = toBytesArray(material.ecdsaPublicKey);
    const ecdhPublicKeyBytes = toBytesArray(material.ecdhPublicKey);

    let coordinatorBinding = await this.identity.getBinding(this.executionId, "coordinator");
    if (!coordinatorBinding) {
      const response = await this.coordinator.call("browser_bind_webauthn_identity", [
        {
          execution_id: hexToExecutionIdBytes(this.executionId),
          assertion: assertionPayload,
          // material.credentialId is undefined for bind material persisted before this field
          // existed (e.g. IndexedDB from an older page load) - send empty, which the
          // coordinator's `!credential_id.is_empty()` check treats as "no index lookup,
          // fall back to the roster scan" rather than an error.
          credential_id: toBytesArray(material.credentialId || new Uint8Array()),
          ecdsa_public_key: ecdsaPublicKeyBytes,
          ecdh_public_key: ecdhPublicKeyBytes,
        },
      ]);
      coordinatorBinding = {
        sessionToken: response.session_token,
        clientIdentity: response.client_identity,
      };
      await this.identity.recordBinding(this.executionId, "coordinator", coordinatorBinding);
    }

    await Promise.all(
      this.partyConnections.map(async (party) => {
        let binding = await this.identity.getBinding(this.executionId, party.role);
        if (binding) return;
        const response = await party.socket.call("browser_bind_webauthn_identity", [
          {
            client_identity: coordinatorBinding.clientIdentity,
            assertion: assertionPayload,
            ecdsa_public_key: ecdsaPublicKeyBytes,
            ecdh_public_key: ecdhPublicKeyBytes,
          },
        ]);
        binding = {
          sessionToken: response.session_token,
          clientIdentity: response.client_identity,
        };
        await this.identity.recordBinding(this.executionId, party.role, binding);
      }),
    );

    this.wasmClient = StoffelWasmClient.fromPublicKey(
      material.ecdsaPublicKey,
      this.parties,
      this.threshold,
    );
    const nonceKey = `stoffel:webauthn-session:nonce:${bytesHex(material.ecdsaPublicKey)}:${this.executionId}`;
    const savedNonce = localStorage.getItem(nonceKey);
    this.wasmExecution =
      savedNonce === null
        ? this.wasmClient.open_execution(this.executionId)
        : this.wasmClient.resume_execution(this.executionId, BigInt(savedNonce));
    this._nonceKey = nonceKey;
  }

  /** Signs and sends one `browser_*` RPC call to the given connection (`this.coordinator`
   * or one of `this.partyConnections`), retrying once with a fresh bind if the server
   * rejects a stale/unknown session token - see the design plan's stale-session fallback. */
  async _signedCall(role, socket, method, bodyValue) {
    const body = textEncoder.encode(JSON.stringify(bodyValue));
    const request = await this._buildSignedRequest(role, method, body);
    try {
      return await socket.call(method, [
        { execution_id: hexToExecutionIdBytes(this.executionId), request },
      ]);
    } catch (error) {
      if (!/session token/i.test(error.message)) throw error;
      await this.identity.forgetBinding(this.executionId, role);
      await this.bind();
      const retryRequest = await this._buildSignedRequest(role, method, body);
      return socket.call(method, [
        { execution_id: hexToExecutionIdBytes(this.executionId), request: retryRequest },
      ]);
    }
  }

  async _buildSignedRequest(role, method, body) {
    const binding = await this.identity.getBinding(this.executionId, role);
    if (!binding) throw new Error(`not bound to ${role} yet - call bind() first`);

    const lockKey = this._nonceKey;
    const sign = async () => {
      const nonce = this.wasmExecution.allocate_nonce();
      // Build the exact same signature-base bytes the coordinator verifies against by
      // calling into WASM directly (authenticationMessage, exposed specifically for this)
      // rather than reimplementing the byte layout here where it could drift out of sync.
      const message = authenticationMessage(method, this.executionId, nonce, body);
      const sessionTokenBytes = textEncoder.encode(binding.sessionToken);
      const fullMessage = new Uint8Array(message.length + sessionTokenBytes.length);
      fullMessage.set(message, 0);
      fullMessage.set(sessionTokenBytes, message.length);

      const ecdsaPublicKey = await this.identity.getEcdsaPublicKey(this.executionId);
      const signature = await this.identity.sign(this.executionId, fullMessage);
      localStorage.setItem(lockKey, this.wasmExecution.current_nonce().toString());
      return {
        public_key: toBytesArray(ecdsaPublicKey),
        // allocate_nonce()/authenticationMessage() use u64 <-> BigInt (wasm-bindgen's
        // standard mapping) - JSON.stringify can't serialize a BigInt at all, so this must
        // become a plain Number before going into the JSON-RPC request below (safe: nonces
        // never realistically approach Number.MAX_SAFE_INTEGER).
        nonce: Number(nonce),
        signature: toBytesArray(signature),
        body: toBytesArray(body),
        session_token: binding.sessionToken,
      };
    };
    return navigator.locks ? navigator.locks.request(lockKey, sign) : sign();
  }

  /** Polls `browser_execution_status` on the coordinator until it reports `round`. */
  async waitForRound(round, { onWaiting } = {}) {
    return poll(
      `Waiting for ${round}`,
      async () => {
        const status = await this._signedCall(
          "coordinator",
          this.coordinator,
          "browser_execution_status",
          {},
        );
        return status.round === round ? status : null;
      },
      { onWaiting },
    );
  }

  async executionStatus() {
    return this._signedCall("coordinator", this.coordinator, "browser_execution_status", {});
  }

  /** Reserves `inputIndex`, collects a mask-share quorum from the configured parties, masks
   * `typedInputs` (via the WASM client's pure field-arithmetic `mask_inputs` - unaffected by
   * any of the WebAuthn machinery above), and submits them. Mirrors voting/calculator's
   * existing cast-a-vote flow exactly, just generalized behind this one call. */
  async submitInputs(inputIndex, typedInputs, { onWaiting } = {}) {
    await this.waitForRound("InputMaskReservation", { onWaiting });
    await this._signedCall(
      "coordinator",
      this.coordinator,
      "browser_reserve_mask_indices",
      [inputIndex],
    );

    const maskResponses = await this._collectMaskShares(inputIndex, { onWaiting });
    const masked = toPlain(
      this.wasmExecution.mask_inputs(BigInt(inputIndex), typedInputs, maskResponses),
    );

    await this.waitForRound("InputCollection", { onWaiting });
    await this._signedCall("coordinator", this.coordinator, "browser_submit_masked_inputs", {
      reserved_indices: masked.map((entry) => entry.reserved_index),
      masked_inputs: masked.map((entry) => entry.masked_input),
    });
  }

  async _collectMaskShares(inputIndex, { onWaiting, timeoutMs = 180_000 } = {}) {
    const responses = new Map();
    const started = Date.now();
    while (responses.size < this.maskShareQuorum && Date.now() - started < timeoutMs) {
      await Promise.all(
        this.partyConnections.map(async (party, index) => {
          if (responses.has(index)) return;
          try {
            const response = await this._signedCall(
              party.role,
              party.socket,
              "browser_assigned_mask_shares",
              { start: inputIndex, count: 1 },
            );
            if (response) responses.set(index, response);
          } catch (_error) {
            // A delayed party is expected to be survivable - robust reconstruction
            // verifies the first quorum of distinct party shares before using it.
          }
        }),
      );
      if (responses.size < this.maskShareQuorum) {
        onWaiting?.(`Collecting mask shares · ${responses.size} / ${this.maskShareQuorum}`);
        await sleep(700);
      }
    }
    if (responses.size < this.maskShareQuorum) {
      throw new Error(`Only ${responses.size} of ${this.maskShareQuorum} required mask shares arrived`);
    }
    return Array.from(responses.values());
  }

  /** Waits for, decrypts, and robustly reconstructs the execution's outputs. `outputTypes`
   * is the same `ClientScalarType[]` shape `StoffelWasmClient.decrypt_outputs` already
   * takes. Decryption happens here in JS (via `WebauthnSession.decryptShare`, since only it
   * can reach the non-extractable ECDH key); reconstruction/type-conversion is delegated
   * back to the WASM client's `reconstruct_outputs`, unaffected by any of this. */
  async getOutputs(outputTypes, { onWaiting } = {}) {
    const encrypted = await poll(
      "Waiting for outputs",
      () => this._signedCall("coordinator", this.coordinator, "browser_output_shares", {}),
      { onWaiting },
    );
    const infoDomain = textEncoder.encode("StoffelOutputShareEncryption");
    const executionIdBytes = new Uint8Array(hexToExecutionIdBytes(this.executionId));
    const info = new Uint8Array(infoDomain.length + executionIdBytes.length);
    info.set(infoDomain, 0);
    info.set(executionIdBytes, infoDomain.length);

    const plaintexts = await Promise.all(
      encrypted.map(([encappedKey, ciphertext]) =>
        this.identity.decryptShare(
          this.executionId,
          { encapped_key: new Uint8Array(encappedKey), ciphertext: new Uint8Array(ciphertext) },
          info,
        ),
      ),
    );
    return this.wasmExecution.reconstruct_outputs(outputTypes, plaintexts);
  }
}

function bytesHex(value) {
  return Array.from(value || [], (byte) => Number(byte).toString(16).padStart(2, "0")).join("");
}

export { WebauthnSession } from "./webauthn-session.js";
