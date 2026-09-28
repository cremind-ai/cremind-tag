#!/bin/sh
# Build the native_sim gateway interop app twice and drive each build
# (README.md): protocol v1 with the companion's GatewayClient (interop.py),
# protocol v2 (v2.conf) with the v2 driver (interop_v2.py). Runs inside the
# NCS toolchain container:
#
#   MSYS_NO_PATHCONV=1 docker run --rm -v ncs-v3.4.1:/ncs -v ctag-build:/build \
#     -v "$(pwd):/work" ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1 \
#     -c 'sh /work/apps/gateway/tests/interop/run.sh'
#
# CTAG_INTEROP=v1 or v2 runs one of them only.
set -e
if ! command -v make >/dev/null 2>&1; then
	apt-get update -qq >/dev/null && apt-get install -y -qq make >/dev/null
fi
python3 -m pip install -q cbor2 pyserial cryptography
ROOT=${CTAG_BUILD_ROOT:-/build}
cd "${NCS_DIR:-/ncs}"

build() { # build <dir> [extra cmake args]
	dir=$1
	shift
	west build --no-sysbuild -b native_sim -p auto -d "$dir" /work/apps/gateway/tests/interop \
		-- -DZEPHYR_EXTRA_MODULES=/work -UCONFIG_* "$@" >"$dir.log" 2>&1 || {
		tail -40 "$dir.log"
		exit 1
	}
}

status=0
if [ "${CTAG_INTEROP:-all}" != v2 ]; then
	build "$ROOT/gw-interop"
	PYTHONPATH=/work/companion/src python3 /work/apps/gateway/tests/interop/interop.py \
		"$ROOT/gw-interop/zephyr/zephyr.exe" || status=1
fi
if [ "${CTAG_INTEROP:-all}" != v1 ]; then
	build "$ROOT/gw-interop-v2" -DEXTRA_CONF_FILE=v2.conf
	PYTHONPATH=/work/companion/src python3 /work/apps/gateway/tests/interop/interop_v2.py \
		"$ROOT/gw-interop-v2/zephyr/zephyr.exe" || status=1
fi
exit $status
