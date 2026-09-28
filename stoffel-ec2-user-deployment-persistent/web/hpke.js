// RFC 9180 HPKE "Open" (base mode), implemented against the specific ciphersuite
// `stoffel-wasm-client` uses (confirmed in its Cargo.toml/lib.rs):
//   KEM  = DHKEM(P-256, HKDF-SHA256)   (kem_id  = 0x0010)
//   KDF  = HKDF-SHA256                  (kdf_id  = 0x0001)
//   AEAD = AES-256-GCM                  (aead_id = 0x0002)
//
// This exists so the browser's non-extractable ECDH private key never has to leave
// WebCrypto's protected boundary to be handed to `stoffel-wasm-client`'s Rust/WASM
// `decrypt_fields_core` - only this module ever touches it, via `deriveBits`, and only
// the resulting one-shot DH output and the final AES key ever become plain bytes (each
// useless beyond decrypting the one message they were derived for).
//
// Every actual cryptographic primitive below is a native WebCrypto call (ECDH
// `deriveBits`, HMAC `sign`, AES-GCM `decrypt`) - the only "glue" code implemented here
// by hand is HPKE's own labeled key-schedule construction (RFC 9180 §4, §5.1, §7.1),
// since WebCrypto's own combined `HKDF` algorithm only exposes extract-then-expand as
// one atomic operation and can't produce an intermediate PRK reusable across HPKE's
// several separately-labeled expands ("key", "base_nonce"). HKDF-Extract/Expand are
// therefore built here directly from RFC 5869's definition, on top of WebCrypto's
// native HMAC-SHA256 - not a reimplementation of any actual cryptographic primitive.

const KEM_ID = 0x0010; // DHKEM(P-256, HKDF-SHA256)
const KDF_ID = 0x0001; // HKDF-SHA256
const AEAD_ID = 0x0002; // AES-256-GCM
const NH = 32; // HKDF-SHA256 output size
const NSECRET = 32; // DHKEM(P-256, ...) shared-secret size
const NK = 32; // AES-256-GCM key size
const NN = 12; // AES-GCM nonce size

const HPKE_VERSION = new TextEncoder().encode("HPKE-v1");

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

function i2osp2(value) {
  if (value < 0 || value > 0xffff) throw new RangeError("i2osp2: value must fit in 2 bytes");
  return new Uint8Array([(value >> 8) & 0xff, value & 0xff]);
}

function kemSuiteId() {
  return concatBytes(new TextEncoder().encode("KEM"), i2osp2(KEM_ID));
}

function hpkeSuiteId() {
  return concatBytes(
    new TextEncoder().encode("HPKE"),
    i2osp2(KEM_ID),
    i2osp2(KDF_ID),
    i2osp2(AEAD_ID),
  );
}

async function hmacSha256(keyBytes, data) {
  // keyBytes must be non-empty - WebCrypto's HMAC key import rejects a
  // zero-length key. Callers needing RFC 5869's "absent salt" behavior
  // handle the substitution themselves (see hkdfExtract).
  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, data));
}

// RFC 5869 §2.2: "salt ... if not provided, is set to a string of HashLen
// zeros." WebCrypto's own HMAC key import additionally rejects a
// zero-length key outright (confirmed against Node's implementation, which
// throws `DataError: Zero-length key is not supported` for it), so this
// substitution is load-bearing, not optional, for HPKE's `LabeledExtract`
// calls with an empty salt (`eae_prk`, `psk_id_hash`, `info_hash` below).
function hkdfExtract(salt, ikm) {
  const effectiveSalt = salt.length > 0 ? salt : new Uint8Array(NH);
  return hmacSha256(effectiveSalt, ikm);
}

// RFC 5869 §2.3.
async function hkdfExpand(prk, info, length) {
  const blocksNeeded = Math.ceil(length / NH);
  let previousBlock = new Uint8Array(0);
  const output = new Uint8Array(blocksNeeded * NH);
  for (let counter = 1; counter <= blocksNeeded; counter += 1) {
    const block = await hmacSha256(prk, concatBytes(previousBlock, info, new Uint8Array([counter])));
    output.set(block, (counter - 1) * NH);
    previousBlock = block;
  }
  return output.slice(0, length);
}

// RFC 9180 §4: LabeledExtract(salt, label, ikm).
function labeledExtract(suiteId, salt, label, ikm) {
  const labeledIkm = concatBytes(HPKE_VERSION, suiteId, new TextEncoder().encode(label), ikm);
  return hkdfExtract(salt, labeledIkm);
}

// RFC 9180 §4: LabeledExpand(prk, label, info, length).
function labeledExpand(suiteId, prk, label, info, length) {
  const labeledInfo = concatBytes(
    i2osp2(length),
    HPKE_VERSION,
    suiteId,
    new TextEncoder().encode(label),
    info,
  );
  return hkdfExpand(prk, labeledInfo, length);
}

/**
 * DHKEM(P-256, HKDF-SHA256) Decap (RFC 9180 §4.1/§7.1.3), producing the KEM shared
 * secret. `encappedKeyBytes` is the sender's ephemeral public key (SEC1 uncompressed,
 * 65 bytes - the `encapped_key` field of an `EncryptedOutputShare`). `ecdhPrivateKey`
 * must be a WebCrypto `CryptoKey` with `{name: "ECDH", namedCurve: "P-256"}` and
 * `deriveBits` usage - non-extractable is fine and expected; this function never
 * exports it, only ever calls `deriveBits` against it.
 */
async function dhkemP256Decap(encappedKeyBytes, ecdhPrivateKey, recipientPublicKeyBytes) {
  const senderPublicKey = await crypto.subtle.importKey(
    "raw",
    encappedKeyBytes,
    { name: "ECDH", namedCurve: "P-256" },
    false,
    [],
  );
  const dh = new Uint8Array(
    await crypto.subtle.deriveBits({ name: "ECDH", public: senderPublicKey }, ecdhPrivateKey, 256),
  );
  const kemContext = concatBytes(encappedKeyBytes, recipientPublicKeyBytes);
  const suiteId = kemSuiteId();
  const eaePrk = await labeledExtract(suiteId, new Uint8Array(0), "eae_prk", dh);
  return labeledExpand(suiteId, eaePrk, "shared_secret", kemContext, NSECRET);
}

/** RFC 9180 §5.1's `KeySchedule`, base mode (no PSK) - returns `{key, baseNonce}`. */
async function keyScheduleBase(sharedSecret, info) {
  const suiteId = hpkeSuiteId();
  const pskIdHash = await labeledExtract(suiteId, new Uint8Array(0), "psk_id_hash", new Uint8Array(0));
  const infoHash = await labeledExtract(suiteId, new Uint8Array(0), "info_hash", info);
  const keyScheduleContext = concatBytes(new Uint8Array([0x00]), pskIdHash, infoHash); // mode_base = 0x00
  const secret = await labeledExtract(suiteId, sharedSecret, "secret", new Uint8Array(0));
  const key = await labeledExpand(suiteId, secret, "key", keyScheduleContext, NK);
  const baseNonce = await labeledExpand(suiteId, secret, "base_nonce", keyScheduleContext, NN);
  return { key, baseNonce };
}

/**
 * Full HPKE `Open` in base mode (RFC 9180 §6.2, single-shot / seq=0, empty AAD - matching
 * exactly how `stoffel-wasm-client`'s Rust side calls `single_shot_open` today). Returns
 * the decrypted plaintext bytes (a serialized `Vec<RobustShare<Fr>>`, to be handed
 * unmodified to `StoffelWasmClient.reconstruct_outputs` alongside every other party's
 * decrypted share for that same output).
 *
 * @param {{encapped_key: Uint8Array, ciphertext: Uint8Array}} encryptedShare
 * @param {CryptoKey} ecdhPrivateKey non-extractable ECDH P-256 private key
 * @param {Uint8Array} ecdhPublicKeyBytes the matching public key, SEC1 uncompressed (65 bytes)
 * @param {Uint8Array} info the HPKE `info` bytes - `output_encryption_info(execution_id)`
 *   on the Rust side: `b"StoffelOutputShareEncryption" || execution_id` (32 raw bytes)
 * @returns {Promise<Uint8Array>}
 */
export async function hpkeOpenP256(encryptedShare, ecdhPrivateKey, ecdhPublicKeyBytes, info) {
  const sharedSecret = await dhkemP256Decap(
    encryptedShare.encapped_key,
    ecdhPrivateKey,
    ecdhPublicKeyBytes,
  );
  const { key, baseNonce } = await keyScheduleBase(sharedSecret, info);
  // seq = 0 for a single-shot open, so nonce = baseNonce XOR 0 = baseNonce unchanged.
  const aesKey = await crypto.subtle.importKey("raw", key, "AES-GCM", false, ["decrypt"]);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: baseNonce, additionalData: new Uint8Array(0), tagLength: 128 },
    aesKey,
    encryptedShare.ciphertext,
  );
  return new Uint8Array(plaintext);
}

// Exported for unit testing against RFC 5869/9180 test vectors independent of the full
// P-256 KEM flow above.
export const _internal = {
  concatBytes,
  i2osp2,
  kemSuiteId,
  hpkeSuiteId,
  hkdfExtract,
  hkdfExpand,
  labeledExtract,
  labeledExpand,
};
