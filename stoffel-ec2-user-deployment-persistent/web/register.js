// One-time WebAuthn registration - the client-side half of the design plan's §1. An
// operator-issued link (`?token=...`) gates this: the server-side POST /client-registrations
// endpoint atomically claims the token, so a used or unknown one is rejected outright. No PRF
// extension, no derived key - this just registers an ordinary passkey; the actual per-session
// signing/decryption keys are generated fresh, locally, every time someone unlocks a ballot
// (see stoffel-browser-client.js/webauthn-session.js) - registration only ever has to prove
// "this person holds a credential," once, permanently.

const statusLabel = document.querySelector("#register-status");
const note = document.querySelector("#register-note");
const button = document.querySelector("#register-button");
const result = document.querySelector("#register-result");

// This page's own URL *is* the one-time registration link (the token/api/label params
// register() reads below) - a QR code of it lets someone who opened it on the wrong
// device (e.g. a laptop, when they meant to register a phone's passkey) switch without
// needing the link resent. Static for the page's whole lifetime, so unlike start.js's
// vote-link QR code, this only ever needs to be drawn once, not re-rendered.
new QRCode(document.querySelector("#register-qr-code"), {
  text: window.location.href,
  width: 180,
  height: 180,
});

function setStatus(label, state = "") {
  statusLabel.textContent = label;
  document.querySelector(".network-state").classList.toggle("ready", state === "ready");
  document.querySelector(".network-state").classList.toggle("error", state === "error");
}

function bytesToArray(buffer) {
  return Array.from(new Uint8Array(buffer));
}

/** SPKI DER for an uncompressed P-256 public key always has this exact 26-byte prefix
 * (SEQUENCE { SEQUENCE { id-ecPublicKey, prime256v1 } }, BIT STRING tag/length/unused-bits)
 * followed directly by the raw 65-byte SEC1 point - fixed length because the algorithm
 * identifier itself is fixed (we only ever request ES256/P-256 below). Extracting the last
 * 65 bytes and checking they start with the uncompressed-point marker (0x04) is a simpler,
 * equally reliable alternative to a full ASN.1 parser for this one fixed shape. */
function sec1PointFromP256Spki(spkiDer) {
  const bytes = new Uint8Array(spkiDer);
  if (bytes.length !== 91) {
    throw new Error(
      `unexpected SPKI length ${bytes.length} for a P-256 key (expected 91) - was a non-ES256 credential created?`,
    );
  }
  const point = bytes.slice(bytes.length - 65);
  if (point[0] !== 0x04) {
    throw new Error("expected an uncompressed EC point (0x04 prefix)");
  }
  return point;
}

async function registerDevice(token, apiUrl, label) {
  if (!window.PublicKeyCredential) {
    throw new Error("This browser doesn't support passkeys (WebAuthn).");
  }

  // Shown by the OS/browser/password-manager's own passkey list (Keychain, 1Password,
  // Android's password manager, ...) - it's the only way someone can tell one registrant's
  // passkey apart from another's there, so a fixed literal here would make every passkey
  // this site ever creates look identical. `name` mirrors client_name - the identifier
  // ./list-registered-clients, start.html's voter picker, and admission's `clients` list all
  // already use for this same registration - derived the same way
  // ./generate-registration-link derives it server-side (`client-` + the token's first 24
  // hex characters), so no extra query param is needed just to carry it here. `displayName`
  // is the operator-supplied label (./generate-registration-link [label]) when there is one,
  // falling back to that same client_name when there isn't.
  const clientName = `client-${token.slice(0, 24)}`;
  const displayName = label || clientName;

  setStatus("Waiting for your device");
  const userId = crypto.getRandomValues(new Uint8Array(16));
  const challenge = crypto.getRandomValues(new Uint8Array(32));
  const credential = await navigator.credentials.create({
    publicKey: {
      rp: { id: window.location.hostname, name: "Stoffel private voting" },
      user: { id: userId, name: clientName, displayName },
      challenge,
      pubKeyCredParams: [{ type: "public-key", alg: -7 }], // ES256 / P-256
      authenticatorSelection: { residentKey: "required", userVerification: "required" },
      attestation: "none",
    },
  });

  if (typeof credential.response.getPublicKey !== "function") {
    // A full CBOR/COSE parse of attestationObject would be the fallback here - not
    // implemented yet; every current major browser supports getPublicKey() (a WebAuthn
    // Level 2+ convenience method), so this should be rare in practice.
    throw new Error(
      "This browser's passkey implementation doesn't expose getPublicKey() - registration can't continue.",
    );
  }
  const spki = await credential.response.getPublicKey();
  const publicKey = sec1PointFromP256Spki(spki);

  setStatus("Registering with the operator");
  const response = await fetch(`${apiUrl.replace(/\/+$/, "")}/client-registrations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token,
      credential_id: bytesToArray(credential.rawId),
      public_key: bytesToArray(publicKey),
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `registration failed (HTTP ${response.status})`);
  }
  return payload;
}

button.addEventListener("click", async () => {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");
  const apiUrl = params.get("api");
  const label = params.get("label");
  if (!token || !apiUrl) {
    setStatus("Invalid link", "error");
    result.textContent = "This link is missing its token - ask the operator to resend it.";
    return;
  }

  button.disabled = true;
  note.textContent = "";
  result.textContent = "";
  try {
    const payload = await registerDevice(token, apiUrl, label);
    setStatus("Registered", "ready");
    result.textContent = `Registered as ${payload.client_name}. You can close this page - the operator will let you know when an election is ready.`;
  } catch (error) {
    console.error(error);
    setStatus("Registration failed", "error");
    result.textContent = error.message;
    button.disabled = false;
  }
});
