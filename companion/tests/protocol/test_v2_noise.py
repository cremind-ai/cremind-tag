"""Noise_IK_25519_ChaChaPoly_SHA256 against the cacophony test vector, and round trips."""

from __future__ import annotations

import pytest

from cremind_tag.secure import identity, noise

# haskell-cryptography/cacophony vectors/cacophony.txt, "Noise_IK_25519_ChaChaPoly_SHA256".
VECTOR = {
    "prologue": "4a6f686e2047616c74",
    "init_static": "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1",
    "init_ephemeral": "893e28b9dc6ca8d611ab664754b8ceb7bac5117349a4439a6b0569da977c464a",
    "init_remote_static": "31e0303fd6418d2f8c0e78b91f22e8caed0fbe48656dcf4767e4834f701b8f62",
    "resp_static": "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893",
    "resp_ephemeral": "bbdb4cdbd309f1a1f2e1456967fe288cadd6f712d65dc7b7793d5e63da6b375b",
    "handshake_hash": "0b0f68fb0c27e03ce9b97565995ed4838cc0581b762ef72b062f6a546419fad7",
    "messages": [
        ("4c756477696720766f6e204d69736573",
         "ca35def5ae56cec33dc2036731ab14896bc4c75dbb07a61f879f8e3afa4c7944718da798efbcd91528520204f904b9bd"
         "6c7413dccdc214d951e15253e39987f18146e8cd0873654207148333479d4d16c289f0294b29960a72f48e0b7bba2e89"
         "083169825e59642148d492020664ccf7"),
        ("4d757272617920526f746862617264",
         "95ebc60d2b1fa672c1f46a8aa265ef51bfe38e7ccb39ec5be34069f1448088435361e70b2ed446e6c9ec387d1d6b3b84"
         "0f194e373979d241b203c4acafccf5"),
        ("462e20412e20486179656b", "050e9f3c8fac16b68dbce8f8c4bfbf6617c897f9ada4aa29aa19c8"),
        ("4361726c204d656e676572", "344233a6cabb7141d80f3da2fedc311d9646bbb0f505afe403a667"),
        ("4a65616e2d426170746973746520536179", "62cdeeb172ad7ade7aa7d9e069da5790f12331bfa00177787a1d0810c67dc3b2b4"),
        ("457567656e2042f6686d20766f6e2042617765726b",
         "029bead1b40992327044d409d9a1f3ad8f36c3c452775d557e18bbeb2e8dfcead32d514024"),
    ],
}


def _hex(name: str) -> bytes:
    return bytes.fromhex(VECTOR[name])


def test_cacophony_vector_byte_for_byte() -> None:
    prologue = _hex("prologue")
    init = noise.Initiator(_hex("init_static"), _hex("init_remote_static"), prologue,
                           ephemeral=lambda: _hex("init_ephemeral"))
    resp = noise.Responder(_hex("resp_static"), prologue, ephemeral=lambda: _hex("resp_ephemeral"))
    assert identity.x25519_public(_hex("resp_static")) == _hex("init_remote_static")
    messages = [(bytes.fromhex(p), bytes.fromhex(c)) for p, c in VECTOR["messages"]]

    msg1 = init.write_message1(messages[0][0])
    assert msg1 == messages[0][1]
    assert resp.read_message1(msg1) == messages[0][0]
    assert resp.rs == identity.x25519_public(_hex("init_static"))

    msg2, resp_session = resp.write_message2(messages[1][0])
    assert msg2 == messages[1][1]
    payload, init_session = init.read_message2(msg2)
    assert payload == messages[1][0]
    assert init_session.handshake_hash == resp_session.handshake_hash == _hex("handshake_hash")

    for i, (plaintext, ciphertext) in enumerate(messages[2:]):
        sender, receiver = (init_session, resp_session) if i % 2 == 0 else (resp_session, init_session)
        assert sender.encrypt(plaintext) == ciphertext
        assert receiver.decrypt(ciphertext) == plaintext


def test_prologue_mismatch_fails_the_handshake() -> None:
    s_priv, _ = identity.x25519_generate()
    r_priv, r_pub = identity.x25519_generate()
    init = noise.Initiator(s_priv, r_pub, noise.prologue(1, bytes(16)))
    resp = noise.Responder(r_priv, noise.prologue(2, bytes(16)))
    with pytest.raises(noise.NoiseError):
        resp.read_message1(init.write_message1())


def test_wrong_responder_key_fails() -> None:
    s_priv, _ = identity.x25519_generate()
    _, r_pub = identity.x25519_generate()
    other_priv, _ = identity.x25519_generate()
    init = noise.Initiator(s_priv, r_pub, b"p")
    with pytest.raises(noise.NoiseError):
        noise.Responder(other_priv, b"p").read_message1(init.write_message1())


def test_transport_is_ordered_and_tamper_evident() -> None:
    s_priv, _ = identity.x25519_generate()
    r_priv, r_pub = identity.x25519_generate()
    init = noise.Initiator(s_priv, r_pub, b"p")
    resp = noise.Responder(r_priv, b"p")
    resp.read_message1(init.write_message1())
    msg2, r_sess = resp.write_message2()
    _, i_sess = init.read_message2(msg2)
    a, b = i_sess.encrypt(b"one"), i_sess.encrypt(b"two")
    with pytest.raises(noise.NoiseError):
        r_sess.decrypt(b)  # skipped a message: nonce mismatch
    r2 = noise.Responder(r_priv, b"p")
    i2 = noise.Initiator(s_priv, r_pub, b"p")
    r2.read_message1(i2.write_message1())
    m2, r2_sess = r2.write_message2()
    _, i2_sess = i2.read_message2(m2)
    ct = bytearray(i2_sess.encrypt(b"hello"))
    ct[0] ^= 1
    with pytest.raises(noise.NoiseError):
        r2_sess.decrypt(bytes(ct))
    assert a != b
