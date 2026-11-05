"""Sign-in for the routing panel: browser sessions and passkeys (WebAuthn).

Kept apart from panel.py and free of its globals so the protocol checks can be
exercised on their own. One `Auth` object per panel holds:

  sessions   — random cookie values; only their SHA-256 is stored, in
               <state>/sessions.json (0600), so sessions survive a panel
               restart but a leaked file cannot be replayed.
  passkeys   — WebAuthn credentials (public keys) in <state>/passkeys.json.
  challenges — single-use, short-lived, in memory.
  a throttle — for failed setup-token sign-ins.

Passkeys are bound to the address the panel is opened at (WebAuthn's "RP ID"
is the hostname), and need https — or http://localhost. Each passkey remembers
the origin it was enrolled from, and is only accepted there.

What is checked, in WebAuthn's terms. Enrolment (only ever started by a
signed-in user): the challenge is one we issued for enrolment, clientData type
and origin, authData's rpIdHash, the UP and UV flags (a biometric or PIN check
really happened), the credential id inside authData, and that the public key —
taken from the browser's getPublicKey(), which spares us a CBOR parser — is
ECDSA P-256 or RSA >= 2048 and is the key authData carries. No attestation is
requested. Sign-in: challenge, type, origin, rpIdHash, UP and UV, the
signature over authData || SHA-256(clientDataJSON), and the signature counter.

Signature verification uses the `cryptography` package (Debian/Ubuntu:
python3-cryptography). It is imported lazily: without it the panel still
runs, passkeys are reported unavailable, and the setup token still signs in.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

CHALLENGE_TTL = 300        # seconds a challenge stays valid
MAX_CHALLENGES = 200       # outstanding challenges kept (oldest dropped)
FAIL_WINDOW = 600          # seconds over which failed token sign-ins count
FAIL_LIMIT = 10            # failures in the window before a lock-out
FAIL_LOCKOUT = 600         # seconds token sign-in stays refused
ALG_ES256, ALG_RS256 = -7, -257
FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40
TRANSPORTS = {"ble", "hybrid", "internal", "nfc", "usb", "smart-card", "cable"}


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text, what="value"):
    if not isinstance(text, str) or not text:
        raise AuthError(400, f"missing {what}")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise AuthError(400, f"{what} is not valid base64url")


def normalize_origin(origin):
    """(origin, hostname) for an origin passkeys can work on, else AuthError."""
    if not isinstance(origin, str):
        raise AuthError(400, "missing origin")
    u = urlparse(origin.strip().lower())
    host = u.hostname or ""
    if not ((u.scheme == "https" and host) or
            (u.scheme == "http" and host == "localhost")):
        raise AuthError(400, "passkeys need an https address (or "
                             "http://localhost)")
    return f"{u.scheme}://{u.netloc}", host


def _crypto():
    """The bits of `cryptography` we use, or AuthError if it is missing."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    except ImportError:
        raise AuthError(501, "passkeys need the python3-cryptography package "
                             "on the server")
    return InvalidSignature, hashes, serialization, ec, padding, rsa


def _load_public_key(spki, alg):
    """Parse a DER SubjectPublicKeyInfo and insist it suits the algorithm."""
    _, _, serialization, ec, _, rsa = _crypto()
    try:
        key = serialization.load_der_public_key(spki)
    except Exception:
        raise AuthError(400, "the passkey's public key could not be read")
    if alg == ALG_ES256:
        if not (isinstance(key, ec.EllipticCurvePublicKey)
                and key.curve.name == "secp256r1"):
            raise AuthError(400, "expected an ECDSA P-256 passkey")
    elif alg == ALG_RS256:
        if not (isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048):
            raise AuthError(400, "expected an RSA passkey of at least 2048 bits")
    else:
        raise AuthError(400, "unsupported passkey algorithm")
    return key


def _key_material(key):
    """The raw numbers of a public key, as they appear inside authData."""
    numbers = key.public_numbers()
    if hasattr(numbers, "x"):
        return [numbers.x.to_bytes(32, "big"), numbers.y.to_bytes(32, "big")]
    return [numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")]


def _client_data(raw, expected_type):
    try:
        data = json.loads(raw)
    except ValueError:
        raise AuthError(400, "clientDataJSON is not valid JSON")
    if not isinstance(data, dict) or data.get("type") != expected_type:
        raise AuthError(400, "unexpected passkey response type")
    if data.get("crossOrigin") is True:
        raise AuthError(400, "cross-origin passkey use is not allowed")
    return data


def _check_auth_data(auth_data, rp_id):
    """Common authData checks; returns the signature counter."""
    if len(auth_data) < 37:
        raise AuthError(400, "authenticator data is too short")
    if auth_data[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise AuthError(400, "the passkey response is for a different address")
    flags = auth_data[32]
    if not flags & FLAG_UP:
        raise AuthError(400, "the passkey did not confirm user presence")
    if not flags & FLAG_UV:
        raise AuthError(400, "the passkey did not verify the user "
                             "(biometric or PIN is required)")
    return int.from_bytes(auth_data[33:37], "big")


class Auth:
    def __init__(self, state_dir, session_days=30, rp_name="routing panel"):
        self.passkeys_file = Path(state_dir) / "passkeys.json"
        self.sessions_file = Path(state_dir) / "sessions.json"
        self.session_seconds = max(60, int(session_days * 86400))
        self.rp_name = rp_name
        self.lock = threading.RLock()
        self.challenges = {}  # challenge -> (purpose, origin, expires)
        self.fails = []       # monotonic stamps of failed token sign-ins
        self.blocked_until = 0.0
        # The files are only ever written by this process, so memory is the
        # truth after load; every change is written straight back.
        self.sessions = self._load(self.sessions_file, {})
        store = self._load(self.passkeys_file, {})
        self.user_id = store.get("userId") or b64u(secrets.token_bytes(16))
        self.passkeys = [p for p in store.get("passkeys", [])
                         if isinstance(p, dict)]

    # --- storage ------------------------------------------------------------
    @staticmethod
    def _load(path, default):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    @staticmethod
    def _save(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)

    def _save_passkeys(self):
        self._save(self.passkeys_file,
                   {"userId": self.user_id, "passkeys": self.passkeys})

    # --- sessions -----------------------------------------------------------
    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _prune_sessions(self):
        now = time.time()
        dead = [k for k, v in self.sessions.items()
                if not isinstance(v, dict) or v.get("expires", 0) <= now]
        for k in dead:
            del self.sessions[k]
        return bool(dead)

    def new_session(self, how):
        """Start a session; returns (cookie value, lifetime in seconds)."""
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.lock:
            self._prune_sessions()
            self.sessions[self._digest(token)] = {
                "created": now, "expires": now + self.session_seconds,
                "how": how}
            self._save(self.sessions_file, self.sessions)
        return token, self.session_seconds

    def session_valid(self, token):
        if not token:
            return False
        with self.lock:
            entry = self.sessions.get(self._digest(token))
            return bool(entry and entry.get("expires", 0) > time.time())

    def end_session(self, token):
        with self.lock:
            if self.sessions.pop(self._digest(token or ""), None) is not None:
                self._save(self.sessions_file, self.sessions)

    def end_all_sessions(self):
        with self.lock:
            self.sessions.clear()
            self._save(self.sessions_file, self.sessions)

    # --- setup-token throttle -----------------------------------------------
    def token_login_allowed(self):
        with self.lock:
            wait = self.blocked_until - time.monotonic()
        if wait > 0:
            raise AuthError(429, "too many failed attempts — token sign-in is "
                                 f"paused for {int(wait // 60) + 1} more "
                                 "minute(s); passkeys still work")

    def token_login_failed(self):
        now = time.monotonic()
        with self.lock:
            self.fails = [t for t in self.fails if now - t < FAIL_WINDOW]
            self.fails.append(now)
            if len(self.fails) >= FAIL_LIMIT:
                self.blocked_until = now + FAIL_LOCKOUT
                self.fails = []

    # --- challenges ---------------------------------------------------------
    def _new_challenge(self, purpose, origin):
        now = time.monotonic()
        challenge = b64u(secrets.token_bytes(32))
        with self.lock:
            for key in [k for k, v in self.challenges.items() if v[2] <= now]:
                del self.challenges[key]
            while len(self.challenges) >= MAX_CHALLENGES:
                del self.challenges[next(iter(self.challenges))]
            self.challenges[challenge] = (purpose, origin, now + CHALLENGE_TTL)
        return challenge

    def _take_challenge(self, challenge, purpose):
        """Use up a challenge; returns the origin it was issued for."""
        with self.lock:
            entry = self.challenges.pop(challenge, None) \
                if isinstance(challenge, str) else None
        if (not entry or entry[0] != purpose
                or entry[2] <= time.monotonic()):
            raise AuthError(400, "the passkey request expired or was already "
                                 "used — try again")
        return entry[1]

    # --- passkeys -----------------------------------------------------------
    @staticmethod
    def passkeys_available():
        """(ok, reason): can this server verify passkeys at all?"""
        try:
            _crypto()
        except AuthError as e:
            return False, str(e)
        return True, ""

    def passkey_count(self, origin=None):
        with self.lock:
            return sum(1 for p in self.passkeys
                       if origin is None or p.get("origin") == origin)

    def list_passkeys(self):
        with self.lock:
            return [{"id": p["id"], "name": p.get("name", ""),
                     "origin": p.get("origin", ""),
                     "created": p.get("created"), "lastUsed": p.get("lastUsed")}
                    for p in self.passkeys]

    def delete_passkey(self, cred_id):
        with self.lock:
            kept = [p for p in self.passkeys if p["id"] != cred_id]
            if len(kept) == len(self.passkeys):
                raise AuthError(404, "no such passkey")
            self.passkeys = kept
            self._save_passkeys()

    def register_options(self, origin):
        """publicKey options for navigator.credentials.create()."""
        _crypto()
        origin, host = normalize_origin(origin)
        with self.lock:
            exclude = [{"type": "public-key", "id": p["id"]}
                       for p in self.passkeys if p.get("origin") == origin]
        return {
            "challenge": self._new_challenge("register", origin),
            "rp": {"id": host, "name": self.rp_name},
            "user": {"id": self.user_id, "name": self.rp_name,
                     "displayName": self.rp_name},
            "pubKeyCredParams": [{"type": "public-key", "alg": ALG_ES256},
                                 {"type": "public-key", "alg": ALG_RS256}],
            "timeout": 120000,
            "attestation": "none",
            "authenticatorSelection": {"residentKey": "preferred",
                                       "userVerification": "required"},
            "excludeCredentials": exclude,
        }

    def register(self, body):
        """Verify an enrolment response and store the passkey; returns its name."""
        if not isinstance(body, dict):
            raise AuthError(400, "bad request")
        cred_id = unb64u(body.get("id"), "credential id")
        client_raw = unb64u(body.get("clientDataJSON"), "clientDataJSON")
        auth_data = unb64u(body.get("authenticatorData"), "authenticatorData")
        spki = unb64u(body.get("publicKey"), "publicKey")
        alg = body.get("alg")

        client = _client_data(client_raw, "webauthn.create")
        origin = self._take_challenge(client.get("challenge"), "register")
        claimed, host = normalize_origin(client.get("origin"))
        if claimed != origin:
            raise AuthError(400, "the passkey response came from a different "
                                 "address than the request")
        counter = _check_auth_data(auth_data, host)
        if not auth_data[32] & FLAG_AT or len(auth_data) < 55:
            raise AuthError(400, "the response carries no new passkey")
        id_len = int.from_bytes(auth_data[53:55], "big")
        if not 1 <= id_len <= 1023 or auth_data[55:55 + id_len] != cred_id:
            raise AuthError(400, "the passkey id does not match the response")
        key = _load_public_key(spki, alg)
        cose_key = auth_data[55 + id_len:]
        if not all(part in cose_key for part in _key_material(key)):
            raise AuthError(400, "the public key does not match the response")

        name = body.get("name")
        name = name.strip()[:60] if isinstance(name, str) else ""
        transports = [t for t in body.get("transports") or []
                      if isinstance(t, str) and t in TRANSPORTS]
        entry = {"id": b64u(cred_id), "name": name or "passkey",
                 "publicKey": b64u(spki), "alg": alg, "signCount": counter,
                 "origin": origin, "rpId": host, "transports": transports,
                 "created": int(time.time()), "lastUsed": None}
        with self.lock:
            if any(p["id"] == entry["id"] for p in self.passkeys):
                raise AuthError(409, "that passkey is already registered")
            self.passkeys.append(entry)
            self._save_passkeys()
        return entry["name"]

    def login_options(self, origin):
        """publicKey options for navigator.credentials.get()."""
        _crypto()
        origin, host = normalize_origin(origin)
        with self.lock:
            allow = [{"type": "public-key", "id": p["id"],
                      **({"transports": p["transports"]}
                         if p.get("transports") else {})}
                     for p in self.passkeys if p.get("origin") == origin]
        if not allow:
            raise AuthError(404, "no passkey is registered for this address — "
                                 "sign in with the setup token and add one")
        return {"challenge": self._new_challenge("login", origin),
                "rpId": host, "timeout": 120000,
                "userVerification": "required", "allowCredentials": allow}

    def login_verify(self, body):
        """Verify a sign-in response; returns the passkey's name."""
        if not isinstance(body, dict):
            raise AuthError(400, "bad request")
        InvalidSignature, hashes, _, ec, padding, _ = _crypto()
        cred_id = b64u(unb64u(body.get("id"), "credential id"))
        client_raw = unb64u(body.get("clientDataJSON"), "clientDataJSON")
        auth_data = unb64u(body.get("authenticatorData"), "authenticatorData")
        signature = unb64u(body.get("signature"), "signature")

        client = _client_data(client_raw, "webauthn.get")
        # use the challenge up first: a failed attempt cannot be retried on it
        origin = self._take_challenge(client.get("challenge"), "login")
        with self.lock:
            cred = next((dict(p) for p in self.passkeys if p["id"] == cred_id),
                        None)
        if not cred:
            raise AuthError(401, "this passkey is not registered here")
        claimed, _ = normalize_origin(client.get("origin"))
        if claimed != origin or origin != cred.get("origin"):
            raise AuthError(401, "this passkey belongs to a different address")
        counter = _check_auth_data(auth_data, cred["rpId"])

        key = _load_public_key(unb64u(cred["publicKey"]), cred["alg"])
        signed = auth_data + hashlib.sha256(client_raw).digest()
        try:
            if cred["alg"] == ALG_ES256:
                key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
            else:
                key.verify(signature, signed, padding.PKCS1v15(),
                           hashes.SHA256())
        except InvalidSignature:
            raise AuthError(401, "the passkey signature is not valid")

        with self.lock:
            stored = next((p for p in self.passkeys if p["id"] == cred_id), None)
            if stored is None:
                raise AuthError(401, "this passkey is not registered here")
            # Synced passkeys always report 0; a counter that is in use must
            # only ever go up (a repeat means a cloned authenticator).
            previous = stored.get("signCount", 0)
            if (counter or previous) and counter <= previous:
                raise AuthError(401, "the passkey's counter went backwards — "
                                     "refusing it")
            stored["signCount"] = counter
            stored["lastUsed"] = int(time.time())
            self._save_passkeys()
            return stored.get("name", "passkey")
