#!/bin/sh
# Build the native_sim gateway interop app and drive it with the companion's
# GatewayClient (README.md). Runs inside the NCS toolchain container:
#
#   MSYS_NO_PATHCONV=1 docker run --rm -v ncs-v3.4.1:/ncs -v ctag-build:/build \
#     -v "$(pwd):/work" ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1 \
#     -c 'sh /work/apps/gateway/tests/interop/run.sh'
set -e
if ! command -v make >/dev/null 2>&1; then
	apt-get update -qq >/dev/null && apt-get install -y -qq make >/dev/null
fi
python3 -m pip install -q cbor2 pyserial
BUILD=${CTAG_BUILD_ROOT:-/build}/gw-interop
cd "${NCS_DIR:-/ncs}"
west build --no-sysbuild -b native_sim -p auto -d "$BUILD" /work/apps/gateway/tests/interop \
	-- -DZEPHYR_EXTRA_MODULES=/work -UCONFIG_* >"$BUILD.log" 2>&1 || {
	tail -40 "$BUILD.log"
	exit 1
}
PYTHONPATH=/work/companion/src python3 /work/apps/gateway/tests/interop/interop.py "$BUILD/zephyr/zephyr.exe"
