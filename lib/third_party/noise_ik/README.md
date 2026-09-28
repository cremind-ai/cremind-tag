# Noise* — Noise_IK_25519_ChaChaPoly_SHA256

The verified Noise IK implementation that `lib/secure` (`ctag_secure`,
docs/firmware-libs.md) runs for protocol v2 secure sessions
(docs/connect-setup.md §3.2). Its primitives come from the HACL* subset in
[`../hacl`](../hacl/README.md).

| | |
|---|---|
| Upstream | <https://github.com/Inria-Prosecco/noise-star> |
| Commit | `ca2a3643821cb1433caf1d104f4020534b0d52c6` |
| Path | `noise-all/api-IK/IK_25519_ChaChaPoly_SHA256/` (generated 2022-05-13) |
| Licence | Apache License 2.0 — `LICENSE` is the repository's `LICENSE.md` |

## Files

Line endings normalised to LF; otherwise as upstream except for the one patch
below.

| File | SHA-256 (as vendored) |
|---|---|
| `Hacl.h` | `6cca2e0924a5221a9b0221bb49a8a0fd8b7618589dbfa76011099f7b773be69e` |
| `IK.c` (patched) | `9b0e13076ac2a0f28a0d209e7677ca4951988596604eb9200b52eec6d333bbf5` |
| `IK.h` | `00dad6bf4fee2eae2a84ab4b0db04c28771dc0b3e32fc796a031b198309e2d14` |
| `Noise_IK.c` | `666557e4eb5c7554bf8e2ed625ad146e2f0631972a65c24d334d0e69e1738a3d` |
| `Noise_IK.h` | `20a9eb2b9bf7978fbcad07dd394eb9a012ab2abb2b994734096b284daefc2cf3` |
| `LICENSE` | `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4` |

Upstream `IK.c` (LF) is `85929279735ada842fde8078e17f52c73af70489529fe851db47c0d0340d9548`.
Not vendored: the directory's Makefiles and `libnoiseapi.def`.

## Patches

1. **`IK.c`, `Noise_IK_device_free`** — one added line,
   `KRML_HOST_FREE(dv.dv_sk);`. The device keeps its own copy of the static
   private key (`Noise_IK_device_create` allocates and copies it) and upstream
   never frees it: 32 bytes of key material leaked per device object (40 with
   the allocator's header), found by the host test's heap accounting
   (`ctag_secure_heap_stats().used` must be 0 after every free). The copy is
   wiped on free like every other allocation (below).

Nothing else in the Noise* or HACL* sources is edited. The build adapts them
from the outside (lib/CMakeLists.txt):

2. **KaRaMeL configuration, force-included** — every vendored translation unit
   is compiled with `-include lib/secure/ctag_krml.h`, which maps
   `KRML_HOST_MALLOC` / `CALLOC` / `FREE` / `EXIT` to the secure heap and its
   abort guard (`lib/secure/hacl_glue.c`), silences `KRML_HOST_PRINTF` /
   `EPRINTF`, and maps `Hacl_Curve25519_64_*` (the x86-64 assembly backend the
   generated code names) to the portable `Hacl_Curve25519_51_*`.
3. **`Lib_Memzero0` and `Lib_RandomBuffer_System`** (HACL*'s wipe and
   system-randomness glue) are not vendored; `hacl_glue.c` implements
   `Lib_Memzero0_memzero` (a volatile loop) and
   `Lib_RandomBuffer_System_crypto_random` (the platform RNG: `sys_csrand_get`
   on Zephyr, a provider set with `ctag_secure_rng_set()` in host tests).
4. The warnings the generated code raises are disabled for these files only,
   as Noise*'s own `Makefile.basic` does (`-Wno-unused-variable`,
   `-Wno-unused-but-set-variable`, `-Wno-unused-parameter`,
   `-Wno-unused-function`, `-Wno-infinite-recursion`).

## How lib/secure uses it

- **Responder with an unknown initiator.** This instantiation's responder only
  accepts initiators whose static key is already in its peer table (the
  "known peers" policy). A v2 device does not know the worker in advance: the
  worker is authenticated by the Noise handshake and authorised afterwards by
  a grant. `lib/secure/noise.c` therefore peeks at message 1 with the
  exported symmetric-state functions (`Noise_IK_mix_hash`, `Noise_IK_mix_dh`,
  `Noise_IK_decrypt_and_hash`) to recover the initiator's static key, adds it
  as the only peer, and then runs the verified `Noise_IK_session_read` on the
  same message, which authenticates it as usual. Cost: one extra X25519 per
  handshake. A message 1 that does not decrypt never reaches the session.
- **Handshake hash.** `h` for the setup, root and maintenance proofs is
  `Noise_IK_session_get_hash` once the session reaches the transport state.
- **Confidentiality level.** Transport messages the responder sends before it
  has received one of the initiator's are at level 4 in Noise* terms (IK
  message 2 does not yet give the responder the initiator's "strong forward
  secrecy" acknowledgement); every seal asks for level 4 (`CONF_LEVEL` in
  noise.c), which both sides reach after the handshake.
- **Allocation failures.** The generated code does not check allocation
  results. Every lib/secure call into Noise* runs under a `setjmp` guard: an
  allocation failure, an RNG failure or `KRML_HOST_EXIT` long-jumps back,
  the whole secure heap is wiped and re-initialised, and every Noise* object
  of the previous heap epoch is dropped (the session ends; the call returns
  `-ENOMEM`, `-EIO` or `-EFAULT`). A failure never reaches the rest of the
  firmware. Outside a guarded call such a failure panics (`k_panic`).
- **Test hook.** `ctag_secure_test_ephemeral()` makes the next ephemeral key
  a given one, so the host tests reproduce the fixture conversation
  (protocol/fixtures/v2_secure.json) byte for byte.
