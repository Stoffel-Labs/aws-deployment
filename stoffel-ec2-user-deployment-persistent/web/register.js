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
  const base = apiUrl.replace(/\/+$/, "");

  // First half of the standard two-step WebAuthn registration ceremony: a server-generated,
  // server-recorded challenge (see registration_options.py) - a client-generated challenge
  // the server never sees in advance would satisfy the WebAuthn API's own "some challenge is
  // present" requirement while providing none of the anti-replay guarantee it exists for.
  setStatus("Requesting a registration challenge");
  const optionsResponse = await fetch(`${base}/client-registrations/options`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  const optionsPayload = await optionsResponse.json().catch(() => ({}));
  if (!optionsResponse.ok) {
    throw new Error(optionsPayload.error || `could not start registration (HTTP ${optionsResponse.status})`);
  }

  setStatus("Waiting for your device");
  const userId = crypto.getRandomValues(new Uint8Array(16));
  const challenge = new Uint8Array(optionsPayload.challenge);
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

  setStatus("Registering with the operator");
  // The full attestation response, byte fields as JSON arrays of ints (this codebase's
  // established over-the-wire convention) - register_client.py verifies the ceremony itself
  // server-side (challenge, origin, RP ID hash, signature) and derives the public key from
  // the verified attestation object, rather than trusting a client-asserted key.
  const response = await fetch(`${base}/client-registrations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token,
      credential: {
        id: credential.id,
        rawId: bytesToArray(credential.rawId),
        response: {
          clientDataJSON: bytesToArray(credential.response.clientDataJSON),
          attestationObject: bytesToArray(credential.response.attestationObject),
        },
      },
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
