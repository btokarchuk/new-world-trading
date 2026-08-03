# Operating the stack

Everything that touches the broker runs **inside the container**. The host is
for editing code and running tests; the container is where trading happens.

## Why containers (and why not SSH)

The trading code must never run with the operator's full user permissions — a
bug or a compromised dependency should not be able to read your home directory
or rewrite its own risk limits. Inside the container it is a non-root user
(`nwt`, uid 1000) with:

- `config/` and `secrets/` mounted **read-only** — the code cannot edit the
  limits that govern it, nor rewrite the keys it uses
- `data/` as the **only** writable mount — ledgers, databases, parquet
- no view of the host filesystem
- no inbound ports

We use `docker exec` rather than SSH. Running an sshd inside a container means
a second daemon, key management, and extra attack surface inside something
meant to be minimal; `exec` gives the same interactive access with none of it.
The `make` targets below are thin wrappers around it.

## Daily workflow

```bash
make up                    # start the box (idempotent)
make status                # risk state, latches, alerts, broker account

make ingest-stocks         # top up daily bars (START=YYYY-MM-DD to override)
make cycle                 # reconcile -> decide -> govern -> submit
make poll                  # later: collect fills, re-verify books

make kill                  # PANIC: cancel all orders + HALT, no confirmation
```

Interactive commands (typed confirmation phrases) need a TTY and are wired
accordingly:

```bash
make resume                # arm the state machine ("RESUME paper")
make flatten               # liquidate everything ("FLATTEN paper <count>")
make shell                 # poke around inside the box
```

## After changing code or config

```bash
make restart               # rebuild image, recreate container
```

Config changes matter: the risk config hash is stamped on every governor
verdict, and (from Phase 9) any change de-arms a live deployment back to paper.

## Corporate TLS proxies

If your network intercepts TLS, export `SSL_CERT_FILE` on the host pointing at
the CA bundle. The Makefile mounts that file into the container read-only and
points the container's `SSL_CERT_FILE` at it. On a clean network, nothing is
needed — the image's own CA bundle is used.

## What is NOT yet containerized

- The **watchdog** is designed but unbuilt (Phase 4b). Until it exists, nothing
  is supervising the engine between attended cycles.
- There is **no scheduler**: cycles run only when a human invokes them. The
  container idles (`sleep infinity`) so `exec` has somewhere to land; Phase 4b
  replaces that idle command with the scheduler loop.
- Agents (analyst, reports, Edge Lab) arrive in later phases, each in their own
  container with their own credential scope.
