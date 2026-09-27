# Security model

## Identities and secrets

| Item | Where it lives | Who can read it |
|---|---|---|
| Tag id (u32) | Tag UICR enrollment blob, companion DB, Cremind device row | public |
| Tag secret (32 B) | Tag UICR, companion OS credential store (Windows Credential Manager / Secret Service / macOS Keychain via `keyring`; 0600 file fallback) | tag + companion only |
| `K_epoch` (16 B) = HKDF(tag secret, tag id, epoch) | assigned bridge (RAM + settings, encrypted in transit by the mesh app key) | the bridge assigned for that epoch |
| Session keys `k_b2t`, `k_t2b` | RAM of bridge and tag for one connection | that session only |
| Mesh net/app/device keys | gateway CDB (settings), bridges (settings) | mesh nodes |
| Connector credential secret | companion credential store; Cremind stores SHA-256 only | companion |

Cremind never receives tag secrets or mesh keys. Bridges never receive tag
secrets. A bridge removed from a tag's assignment keeps only keys for epochs the
tag will refuse once it has authenticated a newer epoch.

## Tag session

Fresh 16-byte nonces from both endpoints, mutual HMAC-SHA256 authentication
under `K_epoch`, HKDF-SHA256 session keys with separate directions, AES-128-CCM
records with an 8-byte tag, in-order counters, constant-time comparisons. A
recorded session cannot be replayed (the tag's nonce changes every session). A
revision replay with a different frame is refused (`REVISION_CONFLICT`); older
revisions are refused (`STALE_REVISION`). Results sent to the bridge are
authenticated records, so a spoofed tag cannot forge an ACK.

Implementations: the firmware uses the platform's PSA Crypto API (audited
implementations shipped with the SDK); the companion uses `cryptography`
(OpenSSL). Neither side implements a primitive itself. The security
configuration is part of every memory-fit measurement.

## Mesh provisioning

PB-ADV without OOB authentication in v1: provision bridges in a controlled
environment, only devices whose UUID the operator explicitly selected.
Provisioning is refused while a tag session is active and vice versa.

## Connector

`Authorization: CremindTag <id>.<secret>` is accepted only on
`/api/tag-connector/v1/*`. Hardware and content grants are separate; a content
credential is bound to one profile and one companion and derives the profile.
Revocation is immediate. The companion only connects outbound.

## Content policy

Default screens show status and titles. Message excerpts require explicit
routing. OTPs, credentials, raw reasoning and raw terminal/tool output are never
journalled for display (filtered in Cremind before a delivery exists, and again
by the companion's card validator). QR codes may only encode short, token-free
https links.

## Physical attacks (out of scope for v1, documented)

UICR is readable over SWD unless APPROTECT is enabled. `cremind-tag tag enroll
--protect` enables APPROTECT after programming (irreversible without a full
erase, which also erases the secret). Without it, anyone with physical SWD
access to a tag can read its secret and impersonate that single tag.
