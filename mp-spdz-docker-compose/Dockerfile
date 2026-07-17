# Build context is expected to be the mp-spdz repo root (see gen-docker-compose,
# which sets `context: ../mp-spdz`).
#
#   docker build -f Dockerfile ../mp-spdz
#
# Builds the statically-linked ECDSA party binaries (ECDSA/README.md's
# "ecdsa-static" target) so the runtime image doesn't need to chase down
# libOTe/boost/etc at the exact version used to compile.

FROM python:3.10.3-bullseye AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    automake build-essential clang-11 cmake git \
    libboost-dev libboost-thread-dev libclang-dev \
    libgmp-dev libntl-dev libsodium-dev libssl-dev libtool \
    && rm -rf /var/lib/apt/lists/*

ENV MP_SPDZ_HOME=/usr/src/MP-SPDZ
WORKDIR $MP_SPDZ_HOME

RUN pip install --upgrade pip

COPY . .

# deps/SimplestOT_C/ref10 may carry a host-built (e.g. macOS) libSimplestOT.a
# plus its CMake cache/Makefile from a local build outside Docker. Those
# reference the host's compiler/paths and, worse, the .a is the wrong object
# format for this container — Make sees the files already exist and skips
# rebuilding, so linking fails with "undefined reference to ref10_*".
# Re-cmake it directly for this container (rather than deleting the
# Makefile and letting `make` fall back to `git submodule update` /
# `git clone`, which both fail: .dockerignore excludes .git, and the
# destination directory already exists and isn't empty).
RUN rm -rf deps/SimplestOT_C/ref10/CMakeCache.txt \
           deps/SimplestOT_C/ref10/CMakeFiles \
           deps/SimplestOT_C/ref10/cmake_install.cmake \
           deps/SimplestOT_C/ref10/libSimplestOT.a \
    && (cd deps/SimplestOT_C/ref10 && cmake .)

# CONFIG's "-Wno-error=unused-parameter" workaround for deps/sse2neon.h's
# warning-as-error is gated on `ifeq ($(OS), Darwin)`, but the underlying
# clang-11 warning also fires on Linux/arm64 (e.g. Docker Desktop on Apple
# Silicon). Insert an unconditional copy of that flag right after CFLAGS'
# "-Werror" is set, so it applies regardless of host OS/arch. This only
# patches the image's build; it does not touch the mounted repo.
RUN sed -i '/-std=c++17 -Werror$/a CFLAGS += -Wno-error=unused-parameter' CONFIG

ARG cxx=clang++-11
RUN echo "CXX = ${cxx}" >> CONFIG.mine \
    && echo "USE_NTL = 0" >> CONFIG.mine \
    && echo "MY_CFLAGS += -I/usr/local/include" >> CONFIG.mine \
    && echo "MY_LDLIBS += -Wl,-rpath -Wl,/usr/local/lib -L/usr/local/lib" >> CONFIG.mine

# Note: deliberately not "make clean-deps" — that also deletes
# deps/SimplestOT_C's vendored source, forcing a network git-clone during
# the build. .dockerignore already excludes the stale host-arch build
# caches (deps/libOTe/out, /local) that clean-deps exists to remove.
RUN make boost libote

# Build only the binaries sig_bench.sh / run-sig-bench actually drive —
# exactly Table 1's six rows (Rep3, Shamir, Mal.Rep3, Mal.Shamir, MASCOT,
# MASCOT- [mascot + -C -B flags]). Skips rep4/atlas/sy-rep/fake-spdz/semi:
# their extra prerequisites (e.g. GC::Rep4Prep, GC::Semi/SemiPrep) are only
# wired up for the plain (non-static) "%.x" pattern rule in the Makefile,
# not "static/%.x", so they fail to link under ecdsa-static — an upstream
# gap unrelated to Docker, and none of those binaries are in Table 1 anyway.
RUN mkdir -p static
RUN make -j"$(nproc)" \
    static/rep-ecdsa-party.x \
    static/mal-rep-ecdsa-party.x \
    static/shamir-ecdsa-party.x \
    static/mal-shamir-ecdsa-party.x \
    static/mascot-ecdsa-party.x

ARG N_MAX=16
RUN mkdir -p Player-Data && ./Scripts/setup-ssl.sh "${N_MAX}"

# --- Runtime stage: binaries + runtime libs only, no build tools -----------
FROM debian:bullseye-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgmp10 \
    libgmpxx4ldbl \
    libsodium23 \
    libssl1.1 \
    libboost-thread1.74.0 \
    libboost-filesystem1.74.0 \
    libboost-iostreams1.74.0 \
    iproute2 \
    iputils-ping \
    bash \
    && rm -rf /var/lib/apt/lists/*

ENV MP_SPDZ_HOME=/usr/src/MP-SPDZ
WORKDIR $MP_SPDZ_HOME

COPY --from=builder /usr/src/MP-SPDZ/static/*-ecdsa-party.x ./
COPY --from=builder /usr/src/MP-SPDZ/Player-Data ./Player-Data/

COPY --from=scripts entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
