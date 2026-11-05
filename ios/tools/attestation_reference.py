#!/usr/bin/env python3
"""Reference for the one piece of passkey code the iPhone app has to port.

The panel's enrolment endpoint (POST api/auth/passkeys/register, see
scripts/panel_auth.py `Auth.register`) wants what a browser hands over:

    authenticatorData   the raw authenticator data
    publicKey           the new key as a DER SubjectPublicKeyInfo
    id                  the credential id

iOS gives a native app none of those directly. A registration
(ASAuthorizationPlatformPublicKeyCredentialRegistration) carries only
`rawClientDataJSON`, `credentialID` and `rawAttestationObject` — a CBOR
map {"fmt", "attStmt", "authData"}. `parse_attestation` below turns that
attestation object into the three values; the Swift `AttestationParser`
must do exactly the same and is tested against the vector this script
writes.

Run it to (re)generate ios/Fixtures/attestation-vector.json and to prove
the result against the real server code: it enrols the parsed passkey
with panel_auth.Auth and then signs in with it.

    python3 ios/tools/attestation_reference.py
"""

import base64
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

ORIGIN = "https://panel.example.ts.net"
RP_ID = "panel.example.ts.net"

# DER header of a SubjectPublicKeyInfo for an uncompressed P-256 point:
# SEQUENCE { SEQUENCE { OID ecPublicKey, OID prime256v1 }, BIT STRING }.
# The key is this header + 0x04 + X (32 bytes) + Y (32 bytes) = 91 bytes.
P256_SPKI_PREFIX = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d030107034200")


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --- the part to port to Swift ----------------------------------------------

def cbor_read(data, pos=0):
    """Decode one CBOR item at `pos`; returns (value, next position).

    Covers what an attestation object and a COSE key contain: unsigned and
    negative integers, byte strings, text strings, arrays and maps, all with
    definite lengths. Anything else is rejected.
    """
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        arg = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        raise ValueError("indefinite-length or reserved CBOR is not supported")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        raw = bytes(data[pos:pos + arg])
        if len(raw) != arg:
            raise ValueError("CBOR string runs past the end of the data")
        return (raw if major == 2 else raw.decode()), pos + arg
    if major == 4:
        items = []
        for _ in range(arg):
            item, pos = cbor_read(data, pos)
            items.append(item)
        return items, pos
    if major == 5:
        mapping = {}
        for _ in range(arg):
            key, pos = cbor_read(data, pos)
            mapping[key], pos = cbor_read(data, pos)
        return mapping, pos
    raise ValueError(f"unsupported CBOR major type {major}")


def parse_attestation(attestation_object):
    """attestationObject -> (authenticatorData, credentialId, SPKI, alg)."""
    top, _ = cbor_read(attestation_object)
    auth_data = top["authData"]
    # authData: rpIdHash 32 | flags 1 | signCount 4 | aaguid 16 |
    #           credIdLen 2 | credId | COSE public key (CBOR map) | ...
    if len(auth_data) < 55 or not auth_data[32] & 0x40:
        raise ValueError("no attested credential data in the response")
    id_len = int.from_bytes(auth_data[53:55], "big")
    cred_id = auth_data[55:55 + id_len]
    cose, _ = cbor_read(auth_data, 55 + id_len)
    # COSE EC2 key: 1 (kty) = 2, 3 (alg) = -7, -1 (crv) = 1 (P-256),
    # -2 = X, -3 = Y. iOS only makes ES256 passkeys.
    if cose.get(1) != 2 or cose.get(3) != -7 or cose.get(-1) != 1:
        raise ValueError("expected an ES256 (P-256) passkey")
    x, y = cose[-2], cose[-3]
    if len(x) != 32 or len(y) != 32:
        raise ValueError("unexpected P-256 coordinate length")
    return auth_data, cred_id, P256_SPKI_PREFIX + b"\x04" + x + y, -7


# --- building a vector and proving it against the server ---------------------

def cbor_bytes(raw):
    n = len(raw)
    head = (bytes([0x40 | n]) if n < 24 else bytes([0x58, n]) if n < 256
            else bytes([0x59]) + n.to_bytes(2, "big"))
    return head + raw


def cbor_text(text):
    raw = text.encode()
    assert len(raw) < 24
    return bytes([0x60 | len(raw)]) + raw


def make_attestation(private_key, cred_id):
    numbers = private_key.public_key().public_numbers()
    x, y = numbers.x.to_bytes(32, "big"), numbers.y.to_bytes(32, "big")
    cose = (b"\xa5" + b"\x01\x02" + b"\x03\x26" + b"\x20\x01"
            + b"\x21" + cbor_bytes(x) + b"\x22" + cbor_bytes(y))
    flags = 0x01 | 0x04 | 0x08 | 0x10 | 0x40  # UP, UV, BE, BS, AT: a synced passkey
    auth_data = (hashlib.sha256(RP_ID.encode()).digest() + bytes([flags])
                 + (0).to_bytes(4, "big") + bytes(16)
                 + len(cred_id).to_bytes(2, "big") + cred_id + cose)
    # the key order Apple uses: fmt, attStmt, authData
    return (b"\xa3" + cbor_text("fmt") + cbor_text("none")
            + cbor_text("attStmt") + b"\xa0"
            + cbor_text("authData") + cbor_bytes(auth_data))


def main():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    import panel_auth

    private_key = ec.generate_private_key(ec.SECP256R1())
    cred_id = hashlib.sha256(b"routing panel test credential").digest()[:20]
    attestation = make_attestation(private_key, cred_id)

    auth_data, parsed_id, spki, alg = parse_attestation(attestation)
    assert parsed_id == cred_id
    assert spki == private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)

    # Enrol and sign in through the real server code.
    with tempfile.TemporaryDirectory() as state:
        auth = panel_auth.Auth(Path(state), session_days=1,
                               rp_name="routing panel /test")
        options = auth.register_options(ORIGIN)
        client = json.dumps({"type": "webauthn.create",
                             "challenge": options["challenge"],
                             "origin": ORIGIN}).encode()
        auth.register({"name": "test", "id": b64u(parsed_id),
                       "clientDataJSON": b64u(client),
                       "authenticatorData": b64u(auth_data),
                       "publicKey": b64u(spki), "alg": alg,
                       "transports": ["internal", "hybrid"]})

        options = auth.login_options(ORIGIN)
        assert [c["id"] for c in options["allowCredentials"]] == [b64u(cred_id)]
        client = json.dumps({"type": "webauthn.get",
                             "challenge": options["challenge"],
                             "origin": ORIGIN}).encode()
        assertion_data = (hashlib.sha256(RP_ID.encode()).digest()
                          + bytes([0x01 | 0x04 | 0x08 | 0x10])
                          + (0).to_bytes(4, "big"))
        signature = private_key.sign(
            assertion_data + hashlib.sha256(client).digest(),
            ec.ECDSA(hashes.SHA256()))
        auth.login_verify({"id": b64u(cred_id), "clientDataJSON": b64u(client),
                           "authenticatorData": b64u(assertion_data),
                           "signature": b64u(signature)})

    vector = {
        "note": "Generated by ios/tools/attestation_reference.py. All values "
                "are base64url without padding. AttestationParser must turn "
                "attestationObject into the three expected values.",
        "rpId": RP_ID,
        "attestationObject": b64u(attestation),
        "expected": {
            "authenticatorData": b64u(auth_data),
            "credentialId": b64u(cred_id),
            "publicKey": b64u(spki),
            "alg": alg,
        },
    }
    out = ROOT / "ios" / "Fixtures" / "attestation-vector.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(vector, indent=2) + "\n")
    print(f"server accepted the enrolment and the sign-in; wrote {out}")


if __name__ == "__main__":
    main()
