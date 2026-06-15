FROM python:3.10.3-bullseye AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    automake build-essential clang-11 cmake git \
    libboost-dev libboost-thread-dev libclang-dev \
    libgmp-dev libntl-dev libsodium-dev libssl-dev libtool \
    && rm -rf /var/lib/apt/lists/*

ENV MP_SPDZ_HOME=/usr/src/MP-SPDZ
WORKDIR $MP_SPDZ_HOME

RUN pip install --upgrade pip

COPY mp-spdz/ .

ARG cxx=clang++-11
RUN echo "CXX = ${cxx}" >> CONFIG.mine \
    && echo "USE_NTL = 0" >> CONFIG.mine \
    && echo "MY_CFLAGS += -I/usr/local/include" >> CONFIG.mine \
    && echo "MY_LDLIBS += -Wl,-rpath -Wl,/usr/local/lib -L/usr/local/lib" >> CONFIG.mine

RUN make clean-deps boost libote

ARG RING_SIZE=32
RUN echo "MOD = -DRING_SIZE=${RING_SIZE}" >> CONFIG.mine

RUN make -j"$(nproc)" \
    mascot-party.x \
    semi-party.x \
    semi2k-party.x \
    shamir-party.x \
    malicious-shamir-party.x

ARG N_MAX=5
RUN mkdir -p Player-Data && ./Scripts/setup-ssl.sh "${N_MAX}"

# Runtime stage: only binaries + runtime libs, no build tools
FROM debian:bullseye-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgmp10 \
    libgmpxx4ldbl \
    libsodium23 \
    libssl1.1 \
    libboost-thread1.74.0 \
    libboost-atomic1.74.0 \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

ENV MP_SPDZ_HOME=/usr/src/MP-SPDZ
WORKDIR $MP_SPDZ_HOME

COPY --from=builder /usr/src/MP-SPDZ/*.x ./
COPY --from=builder /usr/src/MP-SPDZ/libSPDZ.so /usr/local/lib/
COPY --from=builder /usr/src/MP-SPDZ/Player-Data ./Player-Data/
COPY --from=builder /usr/src/MP-SPDZ/local/lib/ /usr/local/lib/
RUN ldconfig

COPY mp-spdz-fargate-deployment/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
