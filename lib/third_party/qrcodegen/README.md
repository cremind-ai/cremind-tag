# Nayuki QR Code generator (C), v1.8.0

Vendored verbatim from <https://github.com/nayuki/QR-Code-generator>, tag
`v1.8.0`, directory `c/`. The companion vendors the Python port of the same
release (`companion/src/cremind_tag/third_party/qrcodegen.py`); docs/protocol.md
§4.4 requires both to be this exact version. Licence: MIT, see `LICENSE` (the
text of the release's `Readme.markdown`, also in each file's header).

| File | SHA-256 |
|---|---|
| `qrcodegen.c` | `300eff07ee25baaa7578f20284411638154716379437391e7e689c0e6ce81403` |
| `qrcodegen.h` | `e82df4bff37d18b5863b9e7486fe6bda1b6cda8c3b9ecebfec473907265cb589` |

Do not edit these files; compile them with the library's own warning set
(`lib/CMakeLists.txt`). Only `ctag_render` uses them.
