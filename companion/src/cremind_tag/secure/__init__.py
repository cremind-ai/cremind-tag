"""Protocol v2: identity, setup codes, grants and secure sessions.

The normative description is ``docs/connect-setup.md``; numeric identifiers
come from the generated :mod:`cremind_tag.protocol.ids`. This package is the
reference implementation the firmware is checked against (``protocol/fixtures/
v2_*.json``) and what Cremind Connect's workers and the simulator run.

Primitives come from ``cryptography`` only (X25519, Ed25519, ChaCha20-Poly1305,
SHA-256, HMAC, HKDF); nothing here implements a primitive itself.
"""
