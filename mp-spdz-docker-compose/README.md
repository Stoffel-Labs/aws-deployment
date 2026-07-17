# mp-spdz-docker-compose

Docker Compose equivalent of `../mp-spdz/sig_bench.sh` — reproduces the
[Dalskov et al.](https://eprint.iacr.org/2019/889) paper's Table 1 `Sig(ms)`
methodology (mean over many runs of the max-across-parties time to generate
one fresh tuple and sign with it), but each party runs in its own container
instead of as a local background process. Modeled on the layout of
`../stoffel-docker-compose`.

## Usage

```
./run-sig-bench <binary-prefix> <n_parties> <n_runs> [extra binary flags...]
```

Examples:

```
./run-sig-bench rep 3 50            # Rep3
./run-sig-bench shamir 3 50         # Shamir
./run-sig-bench mal-rep 3 50        # Mal. Rep3
./run-sig-bench mal-shamir 3 50     # Mal. Shamir
./run-sig-bench mascot 2 50         # MASCOT
./run-sig-bench mascot 2 50 -C -B   # MASCOT- (optimistic Open, ECDSA/EcdsaOptions.h)
```

This builds the party image (see `Dockerfile`, which statically links
exactly the six ECDSA binaries behind Table 1's rows — rep4/atlas/sy-rep/
fake-spdz/semi are skipped, since their extra prerequisites aren't wired up
for `ecdsa-static`'s generic link rule upstream), regenerates
`docker-compose.yml` for the requested topology (`gen-docker-compose`),
brings up `n_parties` containers that each loop `n_runs` single-signature
benchmarks against each other (`entrypoint.sh`, `n_tuples=1` — see
`../mp-spdz/ECDSA/sign.hpp`'s `sign_benchmark`), then parses the container
logs (`collect-results`) into the same `Sig(ms) over N runs: mean = X ms`
line `sig_bench.sh` prints.

## Network impairment

To approximate the paper's LAN / continental-WAN / worldwide-WAN settings
(0.08ms / 17ms / 240ms RTT), set `NET_DELAY` (ms), and optionally `NET_LOSS`
(%) or `NET_BANDWIDTH` (mbit/s), applied via `tc netem` in each container
(requires `cap_add: NET_ADMIN`, already set in `gen-docker-compose`):

```
NET_DELAY=17 ./run-sig-bench rep 3 50
```

## Files

| File | Purpose |
| --- | --- |
| `Dockerfile` | Multi-stage build: compiles `ecdsa-static` binaries against `../mp-spdz`, then a slim runtime image with just the binaries + runtime libs |
| `entrypoint.sh` | Runs inside each container: applies `tc netem` if requested, then loops `N_RUNS` times invoking the party binary with `n_tuples=1`, printing `===RUN i===` markers |
| `gen-docker-compose` | Python script that (re)writes `docker-compose.yml` for the requested binary/party-count/run-count/flags |
| `collect-results` | Python script that parses per-party container logs and computes the mean-of-max `Sig(ms)`, same as `sig_bench.sh` |
| `run-sig-bench` | Orchestrates the above: generate compose file → build+run → collect logs → tear down → print result |
| `docker-compose.yml` | Checked-in default (`rep`, 3 parties, 10 runs) — regenerated on every `run-sig-bench` call |

## Caveats

- All containers run on the same Docker bridge network on one host, so
  absolute timings will still be far below real LAN/WAN numbers unless you
  set `NET_DELAY`/`NET_LOSS`/`NET_BANDWIDTH`, or run the containers on
  separate hosts.
- `MASCOT-` (`-C -B`) hits an upstream MP-SPDZ assertion during teardown
  (`MAC_Check.hpp`) because it deliberately skips the final `Check()` call —
  this happens after the benchmark numbers are already printed, so
  `entrypoint.sh` and `collect-results` both tolerate it.
- Only `rep`, `mal-rep`, `shamir`, `mal-shamir`, `mascot` (± `-C -B` for
  MASCOT-) are built — matching Table 1 exactly. `rep4`/`atlas`/`sy-rep`/
  `fake-spdz`/`semi` fail to link under `ecdsa-static` (missing
  `GC::Rep4Prep`/`GC::Semi*` objects that the plain, non-static `%.x`
  pattern rule pulls in via per-binary `Makefile` prerequisites that the
  `static/%.x` rule doesn't inherit) — a pre-existing upstream gap, not
  something this Dockerfile introduces.
