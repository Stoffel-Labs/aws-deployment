#!/bin/bash
set -e

NAMESPACE="${MPSPDZ_NAMESPACE:-mp-spdz.local}"
PLAYER0_HOST="player0.${NAMESPACE}"
PORT_BASE="${MPSPDZ_PORT_BASE:-5000}"

if [ "${MPSPDZ_PLAYER_ID:-0}" != "0" ]; then
    echo "Waiting for ${PLAYER0_HOST}:${PORT_BASE}..."
    until nc -z "${PLAYER0_HOST}" "${PORT_BASE}" 2>/dev/null; do
        sleep 2
    done
    echo "${PLAYER0_HOST} is reachable."
fi

cd "${MP_SPDZ_HOME:-/usr/src/MP-SPDZ}"

INPUT_SRC="Programs/inputs/Input-P${MPSPDZ_PLAYER_ID}-0"
if [ -f "$INPUT_SRC" ]; then
    mkdir -p Player-Data
    cp "$INPUT_SRC" "Player-Data/Input-P${MPSPDZ_PLAYER_ID}-0"
    echo "Loaded input from $INPUT_SRC"
fi

exec ./"${MPSPDZ_PROTOCOL}" \
    -v \
    -h "${PLAYER0_HOST}" \
    -N "${MPSPDZ_N_PARTIES}" \
    -p "${MPSPDZ_PLAYER_ID}" \
    "${MPSPDZ_PROGRAM}"
