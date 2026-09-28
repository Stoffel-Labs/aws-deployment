// The crypto/identity layer of the shared browser client library: one WebAuthn ceremony
// per tab session bootstraps two non-extractable WebCrypto keys (ECDSA for signing every
// `browser_*` request, ECDH for HPKE-decrypting output) that do all the ongoing work for
// the rest of the session, with no further WebAuthn prompts. See the design plan
// ("Replace uploaded private-key file with WebAuthn-bootstrapped, non-extractable session
// keys") for the full rationale - in short: WebAuthn itself can only ever produce
// signatures over its own fixed assertion format, requires an interactive prompt on every
// use, and can't do decryption at all, so it authorizes a disposable software key pair to
// stand in for it rather than being used directly for ongoing traffic.
//
// A session talks to several independent servers (the coordinator, and each MPC party
// directly) - each one has its own `WebauthnBindings` table server-side, so each needs its
// own bind call and gets back its own `session_token`. The same WebAuthn assertion and key
// pair are reused across all of them (the assertion's challenge commits to the specific
// ephemeral keys, not to which server it's presented to - see the coordinator's
// `webauthn_bind_challenge` doc), so only one interactive prompt is ever needed per tab
// session, no matter how many servers get bound.

import { hpkeOpenP256 } from "./hpke.js";

const DB_NAME = "stoffel-webauthn-sessions";
const DB_VERSION = 1;
const STORE_NAME = "sessions";

function concatBytes(...parts) {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

/** In-memory only - a session record is gone the moment the page unloads. */
export class MemoryKeyStore {
  constructor() {
    this.records = new Map();
  }

  async get(sessionId) {
    return this.records.get(sessionId) ?? null;
  }

  async set(sessionId, record) {
    this.records.set(sessionId, record);
  }

  async delete(sessionId) {
    this.records.delete(sessionId);
  }
}

/**
 * Persists across tab closes/reloads via IndexedDB. Non-extractable `CryptoKey` objects
 * survive the structured-clone round trip into and out of IndexedDB intact (still
 * non-extractable - the raw key material is never exposed to JS at either end), so this
 * does not weaken the "ordinary JS can't export the key" property at all; it only means
 * the key persists on disk (plain IndexedDB storage - LevelDB/SQLite in the browser
 * profile, not a hardware enclave) rather than only in page memory. See the design plan's
 * "what non-extractable really means" note for the precise threat model this does and
 * doesn't cover.
 */
export class IndexedDBKeyStore {
  constructor() {
    this._dbPromise = null;
  }

  _openDb() {
    if (!this._dbPromise) {
      this._dbPromise = new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains(STORE_NAME)) {
            request.result.createObjectStore(STORE_NAME);
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    return this._dbPromise;
  }

  async get(sessionId) {
    const db = await this._openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const request = tx.objectStore(STORE_NAME).get(sessionId);
      request.onsuccess = () => resolve(request.result ?? null);
      request.onerror = () => reject(request.error);
    });
  }

  async set(sessionId, record) {
    const db = await this._openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).put(record, sessionId);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async delete(sessionId) {
    const db = await this._openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).delete(sessionId);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }
}

async function generateEcdsaKeyPair() {
  return crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, [
    "sign",
  ]);
}

async function generateEcdhKeyPair() {
  return crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, [
    "deriveBits",
  ]);
}

async function exportRawPublicKey(publicKey) {
  return new Uint8Array(await crypto.subtle.exportKey("raw", publicKey));
}

/**
 * The challenge one WebAuthn bind ceremony's assertion must cover - must match the
 * coordinator's `webauthn_bind_challenge` exactly (SHA-256 of a fixed domain tag followed
 * by the two ephemeral public keys being bound), so the assertion cryptographically commits
 * to these specific keys rather than being replayable with different, attacker-chosen ones.
 */
async function bindChallenge(ecdsaPublicKey, ecdhPublicKey) {
  const domain = new TextEncoder().encode("stoffel-browser-webauthn-bind-v1");
  const data = concatBytes(domain, ecdsaPublicKey, ecdhPublicKey);
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
}

function base64UrlToBytes(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export class WebauthnSession {
  /**
   * @param {{persistence?: "memory" | "indexeddb"}} [options]
   */
  constructor({ persistence = "memory" } = {}) {
    this.store = persistence === "indexeddb" ? new IndexedDBKeyStore() : new MemoryKeyStore();
    // In-flight/completed bind-material generation per sessionId, so a caller binding to
    // six servers in a loop for the same session only ever generates keys/prompts WebAuthn
    // once, not six times - every call after the first reuses this cached promise.
    this._bindMaterial = new Map();
    // Serializes recordBinding/forgetBinding per sessionId. `bind()` calls recordBinding
    // once per party, concurrently (Promise.all) - each call is a read-modify-write of the
    // same stored record (fetch, mutate one role's entry, write the whole record back), so
    // without this, two racing calls can lose an update: call B reads the record before
    // call A's write lands, then overwrites it with a stale copy that's missing A's change.
    this._recordLocks = new Map();
  }

  /** Runs `fn` only after every earlier call queued for this `sessionId` has settled, so
   * concurrent recordBinding/forgetBinding calls for the same session never interleave their
   * get/mutate/set - see the constructor's `_recordLocks` note for why this exists. */
  _withRecordLock(sessionId, fn) {
    const tail = this._recordLocks.get(sessionId) ?? Promise.resolve();
    const result = tail.then(fn, fn);
    // The chain link always resolves - one failed op shouldn't wedge every later call for
    // this sessionId forever - while `result` (returned to the caller) still reflects fn's
    // actual outcome.
    this._recordLocks.set(
      sessionId,
      result.then(
        () => {},
        () => {},
      ),
    );
    return result;
  }

  /**
   * Ensures a non-extractable ECDSA + ECDH key pair and one WebAuthn assertion covering
   * them exist for `sessionId`, generating/prompting only if nothing usable is already
   * stored (including across a page reload, in IndexedDB mode) or cached from an earlier
   * call in this same page load. Returns everything a caller needs to bind to any number of
   * servers: `{assertion: {authenticatorData, clientDataJson, signature}, credentialId,
   * ecdsaPublicKey, ecdhPublicKey}` (all `Uint8Array`).
   */
  async getBindMaterial(sessionId) {
    if (this._bindMaterial.has(sessionId)) {
      return this._bindMaterial.get(sessionId);
    }
    const promise = this._loadOrCreateBindMaterial(sessionId);
    this._bindMaterial.set(sessionId, promise);
    try {
      return await promise;
    } catch (error) {
      // Don't cache a failed attempt - a later retry (e.g. after the user
      // dismisses a WebAuthn prompt by mistake) should try again cleanly.
      this._bindMaterial.delete(sessionId);
      throw error;
    }
  }

  async _loadOrCreateBindMaterial(sessionId) {
    const existing = await this.store.get(sessionId);
    if (existing && existing.ecdsaKey && existing.ecdhKey && existing.assertion) {
      return {
        assertion: existing.assertion,
        credentialId: existing.credentialId,
        ecdsaPublicKey: existing.ecdsaPublicKey,
        ecdhPublicKey: existing.ecdhPublicKey,
      };
    }

    const [ecdsaKeyPair, ecdhKeyPair] = await Promise.all([
      generateEcdsaKeyPair(),
      generateEcdhKeyPair(),
    ]);
    const ecdsaPublicKey = await exportRawPublicKey(ecdsaKeyPair.publicKey);
    const ecdhPublicKey = await exportRawPublicKey(ecdhKeyPair.publicKey);
    const challenge = await bindChallenge(ecdsaPublicKey, ecdhPublicKey);
    const { assertion, credentialId } = await this._requestWebauthnAssertion(challenge);

    await this.store.set(sessionId, {
      ecdsaKey: ecdsaKeyPair.privateKey,
      ecdhKey: ecdhKeyPair.privateKey,
      ecdsaPublicKey,
      ecdhPublicKey,
      assertion,
      credentialId,
      bindings: (existing && existing.bindings) || {},
    });

    return { assertion, credentialId, ecdsaPublicKey, ecdhPublicKey };
  }

  /** One `navigator.credentials.get()` prompt - discoverable (no `allowCredentials` hint),
   * so the platform's own picker shows if more than one registered identity is present.
   * Returns `{assertion, credentialId}` - `credentialId` is `credential.rawId`, the same
   * opaque value the coordinator's credential-ID index is keyed on. */
  async _requestWebauthnAssertion(challenge) {
    const credential = await navigator.credentials.get({
      publicKey: {
        challenge,
        userVerification: "required",
      },
    });
    const response = credential.response;
    return {
      assertion: {
        authenticatorData: new Uint8Array(response.authenticatorData),
        clientDataJson: new Uint8Array(response.clientDataJSON),
        signature: new Uint8Array(response.signature),
      },
      credentialId: new Uint8Array(credential.rawId),
    };
  }

  /** Records a successful bind to one server connection (`role` is caller-chosen, e.g.
   * "coordinator" or "party0" - just a label distinguishing this server's own
   * `session_token`/`client_identity` from every other server's). */
  async recordBinding(sessionId, role, { sessionToken, clientIdentity }) {
    return this._withRecordLock(sessionId, async () => {
      const record = await this.store.get(sessionId);
      if (!record) throw new Error(`recordBinding called before getBindMaterial for ${sessionId}`);
      record.bindings[role] = { sessionToken, clientIdentity };
      await this.store.set(sessionId, record);
    });
  }

  /** `{sessionToken, clientIdentity} | undefined` for a server this session has already
   * successfully bound to. */
  async getBinding(sessionId, role) {
    const record = await this.store.get(sessionId);
    return record?.bindings?.[role];
  }

  /** Clears a stale binding (e.g. the coordinator rejected the token - a restart, an
   * idle-timeout eviction) without discarding the session's keys - the caller should
   * re-bind using the same `getBindMaterial` result, not generate new keys/re-prompt. */
  async forgetBinding(sessionId, role) {
    return this._withRecordLock(sessionId, async () => {
      const record = await this.store.get(sessionId);
      if (!record) return;
      delete record.bindings[role];
      await this.store.set(sessionId, record);
    });
  }

  /** Signs `messageBytes` with this session's non-extractable ECDSA key. Returns raw
   * `r || s` (IEEE P1363) - WebCrypto's native ECDSA output format, which already matches
   * `SignedBrowserRequest`'s existing signature convention with no conversion needed. */
  async sign(sessionId, messageBytes) {
    const record = await this.store.get(sessionId);
    if (!record) throw new Error(`sign called before getBindMaterial for ${sessionId}`);
    // [diagnostic] Temporary - tracking down a Safari "OperationError: The operation
    // failed for an operation-specific reason" seen when several elections/tabs are open
    // at once. Remove once the real throw site (this, or decryptShare below) is confirmed.
    try {
      const signature = await crypto.subtle.sign(
        { name: "ECDSA", hash: "SHA-256" },
        record.ecdsaKey,
        messageBytes,
      );
      return new Uint8Array(signature);
    } catch (error) {
      console.error(`[diagnostic] crypto.subtle.sign failed (session ${sessionId}):`, error);
      throw error;
    }
  }

  async getEcdsaPublicKey(sessionId) {
    const record = await this.store.get(sessionId);
    return record?.ecdsaPublicKey;
  }

  async getEcdhPublicKey(sessionId) {
    const record = await this.store.get(sessionId);
    return record?.ecdhPublicKey;
  }

  /** JS-native HPKE-open (RFC 9180, see hpke.js) against this session's non-extractable ECDH
   * key - the private scalar never becomes JS-readable at any point; only the one-shot DH
   * shared secret and the resulting AES key ever exist as plain bytes, each useless beyond
   * the single message they were derived for. `info` is the raw HPKE info bytes
   * (`output_encryption_info(execution_id)` on the Rust side). */
  async decryptShare(sessionId, encryptedShare, info) {
    const record = await this.store.get(sessionId);
    if (!record) throw new Error(`decryptShare called before getBindMaterial for ${sessionId}`);
    // [diagnostic] Temporary - see sign()'s matching note.
    try {
      return await hpkeOpenP256(encryptedShare, record.ecdhKey, record.ecdhPublicKey, info);
    } catch (error) {
      console.error(`[diagnostic] hpkeOpenP256/decryptShare failed (session ${sessionId}):`, error);
      throw error;
    }
  }

  /** Clears everything stored for `sessionId` - call when an execution/session is done
   * (e.g. IndexedDB-mode voting deletes its entry once the execution reaches a terminal
   * state, per the design plan) so persisted keys don't outlive their usefulness. */
  async forget(sessionId) {
    this._bindMaterial.delete(sessionId);
    await this.store.delete(sessionId);
  }
}

export { base64UrlToBytes };
