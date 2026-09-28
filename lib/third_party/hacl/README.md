# HACL* (minimal subset) and KaRaMeL headers

The verified primitives under `lib/secure` (`ctag_secure`) and the Noise* IK
implementation in [`../noise_ik`](../noise_ik/README.md): X25519
(`Hacl_Curve25519_51`), Ed25519 verification (grant signatures;
signing only in host tests), ChaCha20-Poly1305 (`Hacl_Chacha20Poly1305_32`),
SHA-256 and HMAC-SHA-256 (HKDF is built on HMAC in `lib/secure/crypto.c`).

| | |
|---|---|
| Upstream | <https://github.com/hacl-star/hacl-star> |
| Commit | `45235f7d452e808d6c4b9ec8bad53e412f5f4a93` (2022-05-20), the HACL* snapshot Noise* was generated against |
| Paths | `dist/gcc-compatible/` (the `Hacl_*`, `Lib_Memzero0.h`, `evercrypt_targetconfig.h` and `internal/` files); `dist/karamel/include/krml/` and `dist/karamel/krmllib/dist/minimal/` (→ `karamel/`) |
| Licences | HACL*: MIT — `LICENSE` is `dist/LICENSE.txt`; KaRaMeL headers: Apache License 2.0 (their file headers) — `karamel/LICENSE` is the Apache 2.0 text |

Every file is byte-identical to its upstream blob (checked with
`git hash-object --no-filters` after normalising line endings to LF); nothing
is patched. The build adapts them from the outside with the force-included
`lib/secure/ctag_krml.h` (allocator, exit, printing, the Curve25519
`_64` → `_51` mapping; see the Noise* README).

## Files

| File | SHA-256 |
|---|---|
| `Hacl_Bignum25519_51.h` | `ee9223bf859a99459eb8b1a7122f8a1fe79b0b2f3713cace846cec848dd7f035` |
| `Hacl_Chacha20.c` | `5e2dd6aa8f53a586808b830aebdab082f7fbe367ce27600b841c76c81407a330` |
| `Hacl_Chacha20.h` | `0b3ada365c789d500914361de29ad5268ee2dd087e9369c4e973f3e88e110455` |
| `Hacl_Chacha20Poly1305_32.c` | `e26db3b9f09c528828eb47b70431956d9ff53da5741b6e60dd23afdfbadb0bff` |
| `Hacl_Chacha20Poly1305_32.h` | `69d2d25fc504a6d89ba16d2ec25421fe43185b5e2a150bd4c066807b87c0a67d` |
| `Hacl_Curve25519_51.c` | `8833a901a291dd0297bca9124ed2503632f9f66fbef073eeb1b8833340852379` |
| `Hacl_Curve25519_51.h` | `36f063948c76acc077f4a5cbd9cfb04e064bd8e07a588a9540d9b5e84480784a` |
| `Hacl_Ed25519.c` | `2a06e7438bfe1ed98a6dee4e0d8e225ca1cc283ac810a6c1e8ac1607d5d59e4c` |
| `Hacl_Ed25519.h` | `83ecc20fbd8d5116ed373e09ab3c0d60ce52a5a3d977a513b4fadbfa4dfaa69e` |
| `Hacl_HMAC.c` | `b5a40a89ec9586c914ae7df9e1a1147cde9b64f457e5298aa689e6a2c0e94ccd` |
| `Hacl_HMAC.h` | `1978cca4e14c05fd0c218344a27b0fe1ae4d9bd341c85dc7d0710994e9e10a85` |
| `Hacl_Hash_Blake2.c` | `4c21791f7fff57ef8f1ff692b0ac0acd653d98f404eb2fe6cbd3979c731c9d36` |
| `Hacl_Hash_Blake2.h` | `c725c7e09b6f1447ef72e75d0d65a473b0131f6bf3d6bdeb40d2b439011151ca` |
| `Hacl_Hash_SHA1.c` | `9d85222664824501c0cb7ced3e8460234a604f6fa3c30b7f79415e4a27194fd8` |
| `Hacl_Hash_SHA1.h` | `8001c0b890dcd337084e097febed7b1dd4314a8c42ddff6d392a29a6523eedb5` |
| `Hacl_Hash_SHA2.c` | `b7742f215ac1096497139b495ff228f577d71bde6e9b87f04f1462ee2042756a` |
| `Hacl_Hash_SHA2.h` | `5e70d2c5bf01204e8458f1076b4a3d92e7e52ff1aa79c4f9576da8963428ba24` |
| `Hacl_Impl_Blake2_Constants.h` | `deb12f88aaf3f0ccb000ca66b397afb8c2cf779d62eaf405b1d31dcdaeba7cd1` |
| `Hacl_Krmllib.h` | `89f4cc8ab76041f75aa7d54fd46bbee459806085b89177e795205ae2bccc1fb0` |
| `Hacl_Poly1305_32.c` | `f1519d3fe6337b8b8d00671a67bdf47896e238829f7ce2ce64a0cd3616afe360` |
| `Hacl_Poly1305_32.h` | `793c62374e26ba4c5fbd9ff1962fcbf79a4b688d0c503ea97f9bd8a3e43a13eb` |
| `Hacl_Streaming_SHA2.c` | `ab620c53978ba95e2a5ef45fed7c344ca65ff08a87c96b9de9e7175fdddd1421` |
| `Hacl_Streaming_SHA2.h` | `906d80895f3873488ce3cadd34a87a93df8f2df616628ecbc353bd4cb53d4422` |
| `Lib_Memzero0.h` | `0e650e7fd0a4d5255943df8ad701964bfddfbf5ce59caaa4aa6c581293874c2c` |
| `evercrypt_targetconfig.h` | `5e2e94e02f758d7a399d58c92cd825332f266576b994f25719fa7c32ef95791b` |
| `internal/Hacl_Chacha20.h` | `49db1cbbd42c3b6e54dcb3fc8b4861b54dab44f246bddf28de180eb04ab48674` |
| `internal/Hacl_Curve25519_51.h` | `16d7ac2f4dfbce937e23d5c32ba1e8a0b96c229e55f051cb30951b63fd63acee` |
| `internal/Hacl_Ed25519.h` | `933640da88ffa5451eee197a92dc7fb5d9b5a05bbe098180059d9f4153a2ba78` |
| `internal/Hacl_HMAC.h` | `1f9ad9a7b57b17637273812cfbef6ee7687dbd8e621bbcf39649be9c18f25f33` |
| `internal/Hacl_Hash_Blake2.h` | `a068641c2df58e508835c276c9594035dd3b520d4712f9d6e82ce1823a986bc3` |
| `internal/Hacl_Hash_SHA1.h` | `21a6ad79add14bb98db2e48a5ae8c8ec4fd51cfcfe26e10f5c0b69a80a31bc27` |
| `internal/Hacl_Hash_SHA2.h` | `bfd57175fd54faf400142acd4663d3a5e1819375ca7e425fc6b4e5efc9fe1326` |
| `internal/Hacl_Krmllib.h` | `363304de43781467b2fac1e9413ebff6f1176950877deb3dbe9c9cf2d7babd63` |
| `karamel/include/krml/internal/callconv.h` | `4845cbbf8e290786a4631b69d2571266b39072558077e22e0f51bad427717183` |
| `karamel/include/krml/internal/compat.h` | `94f968c2313c36499cdf1712378b465b13f60e2ac6a59bc041a865658b2ced83` |
| `karamel/include/krml/internal/target.h` | `9e29ec10dd654b081657db8266ea289e533bbe96152a58e359272a637d0558a4` |
| `karamel/include/krml/internal/types.h` | `b21a6cbc542104cce3afa88d8131ebde1071d69c8a0cd7ab85db45f0cfc683b9` |
| `karamel/include/krml/lowstar_endianness.h` | `a5271093b50cf0db3e776262b26ae852acae141d49505a2a41a863987634c9a3` |
| `karamel/krmllib/dist/minimal/FStar_UInt128.h` | `c32c0637d0425a662577392d2fcd2d90bb4968d7c3a349bc664457b245e47565` |
| `karamel/krmllib/dist/minimal/FStar_UInt128_Verified.h` | `4b967374fa3b7142f822d684cb010daff74056855f00e810a3b716eea1b9c3c8` |
| `karamel/krmllib/dist/minimal/FStar_UInt_8_16_32_64.h` | `50594e097aa1e35e2adf4f3aa779738db244406366e3d2262abbf3eb29feac05` |
| `karamel/krmllib/dist/minimal/LowStar_Endianness.h` | `ab83889e7cab0fcb2a12ce9d191afd0f941d53fed5f1ab458d697f2d68aa25cf` |
| `karamel/krmllib/dist/minimal/fstar_uint128_gcc64.h` | `f5885a6cd1095a8351b99bf0b08a05836066611a445c3bbee1f5561af51155f7` |
| `karamel/krmllib/dist/minimal/fstar_uint128_msvc.h` | `f44141113befced9b17a4dbc7926aa30433f643be7135196c0ce2cf607b6fece` |
| `karamel/krmllib/dist/minimal/fstar_uint128_struct_endianness.h` | `fe57e1bc5ce3224d106e36cb8829b5399c63a68a70b0ccd0c91d82a4565c8869` |
| `LICENSE` | `e33b48ee2bfc1a2da0fb81a7a003f407692fc5497057eb34df9eee57b923fd32` |
| `karamel/LICENSE` | `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4` |

## Notes

- **SHA-1 and BLAKE2** (`Hacl_Hash_SHA1.*`, `Hacl_Hash_Blake2.*`,
  `Hacl_Impl_Blake2_Constants.h`) are here only because `Hacl_HMAC.c`
  defines HMAC for every hash of the snapshot and references them. lib/secure
  calls `Hacl_HMAC_compute_sha2_256` only; on the targets `--gc-sections`
  drops the unused code.
- **128-bit arithmetic.** `Hacl_Curve25519_51` multiplies 64-bit limbs into
  `FStar_UInt128`. `krml/internal/types.h` selects the verified portable
  implementation (`FStar_UInt128_Verified.h`, a struct of two `uint64_t`)
  where the compiler has no 128-bit integer — the nRF52 Cortex-M4 and 32-bit
  native_sim — and `unsigned __int128` on 64-bit hosts
  (`native_sim/native/64`); the host tests define `KRML_VERIFIED_UINT128` so
  they run the portable code the targets run.
- **Not vendored:** `Lib_Memzero0.c` and `Lib_RandomBuffer_System.c`
  (replaced by `lib/secure/hacl_glue.c`, see the Noise* README),
  `Hacl_Krmllib.c` (nothing used needs its code), and everything else of
  `dist/`.
