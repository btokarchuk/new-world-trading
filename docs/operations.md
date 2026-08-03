# Operating the stack

Everything that touches the broker runs **inside the container**. The host is
for editing code and running tests; the container is where trading happens.

Two services, one image:

| service | command | job |
| --- | --- | --- |
| `engine` | `nwt-risk run` | the scheduler loop: reconcile, decide, govern, submit, poll — and write a heartbeat after each real step |
| `watchdog` | `nwt-watchdog` | the dead-man's switch: read those heartbeats, cancel at the broker when one is broken |

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
make up                    # start both services (idempotent)
make beat                  # is the engine alive? last heartbeat + commands
make status                # risk state, latches, alerts, broker account

make ingest-stocks         # top up daily bars (START=YYYY-MM-DD to override)
make cycle                 # attended: reconcile -> decide -> govern -> submit
make poll                  # attended: collect fills, re-verify books

make kill                  # PANIC: cancel all orders + HALT, no confirmation
```

Interactive commands (typed confirmation phrases) need a TTY and are wired
accordingly:

```bash
make resume                # arm the state machine ("RESUME paper")
make flatten               # liquidate everything ("FLATTEN paper <count>")
make shell                 # poke around inside the box
```

## The heartbeat, and reading it

A heartbeat is a **promise, not a pulse**: every beat carries `next_due` —
"I will be back by this time". An overnight sleep and a 30-second poll are the
same shape to a supervisor, so the watchdog never has to know the trading
calendar to spot a wedged engine. Beats are written from inside the loop after
real work completes, never from a side thread, because a thread that keeps
beating while the loop is stuck manufactures false confidence.

```bash
make beat                  # last beat, whether it is overdue, pending commands
make watchdog-logs         # the supervisor's point of view
```

`make beat` runs inside the **watchdog** container on purpose: it earns its keep
exactly when the engine is dead or crash-looping, and `exec` into a dead
container answers nothing. If both containers are down, `make ps` is the answer.

Pending control commands are the watchdog talking back: it cancels orders at the
broker directly and *then* writes the command row telling the engine why its
orders vanished. A pending `HALT` means the supervisor already acted.

## Rehearsing the safety nets

```bash
make drill                 # nonzero exit == a safety net has a hole
```

`nwt-risk drill --scenario insanity` is paper-only and refuses live with a hard
error. It runs four scenarios and prints every assertion with PASS/FAIL:

1. **hostile-intent flood** — ~17 intents, each violating a different limit,
   through the real governor loaded with the real `config/risk.yaml`. Every one
   must be rejected with the expected reason code, and zero may be approved.
2. **heartbeat starvation** — a beat whose `next_due` has already passed is
   written and asserted overdue past the grace, and a kept promise is asserted
   *not* to be a breach.
3. **kill-switch** — a real `cancel_all` at the broker, a real trip to HALTED
   with a latch, then the prior state restored by a recorded operator
   transition.
4. **resume-requires-acks** — a resume without acking the latch, and one with a
   mistyped confirmation phrase, are both refused.

Every run writes a `drill` row to the alerts outbox with the full transcript.
The live-arming checklist reads those rows as evidence: **a drill that was not
logged did not happen.**

Two things to know before running it:

- Run it from a **clean latch state** (typically right after arming paper). The
  drill refuses to run scenarios 3 and 4 when un-acked latches are outstanding,
  because restoring state would silently ack latches a human has not reviewed.
- It really does trip the kill switch, and cancels for real at the broker. An
  in-flight cycle will find itself HALTED mid-drill — safe, but noisy. Restoring
  from HALTED also has to climb one rung through REDUCING (the lowest state that
  can ack a latch at all), so a concurrent cycle could briefly see REDUCING and
  pass a protective exit. Prefer running the drill while the scheduler is idle.

## After changing code or config

```bash
make restart               # rebuild the image, recreate both containers
```

Config changes matter: the risk config hash is stamped on every governor
verdict, and (from Phase 9) any change de-arms a live deployment back to paper.

## Corporate TLS proxies

If your network intercepts TLS, export `SSL_CERT_FILE` on the host pointing at
the CA bundle. The Makefile mounts that file into the container read-only and
points the container's `SSL_CERT_FILE` at it. On a clean network, nothing is
needed — the image's own CA bundle is used.

## Keys: why the watchdog has its own

`secrets/watchdog-paper.env` is a **separate Alpaca key pair**, loaded only into
the watchdog container. The supervisor's whole job is to act when the engine is
the problem, so revoking or rate-limiting a runaway engine's credentials must
not disarm it, and the broker's order log has to show which of the two cancelled
what.

Compose treats that file as **required**: if `secrets/watchdog-paper.env` is
missing, `make up` starts *nothing*, engine included. That is the intended
posture — no supervisor, no trading. Copy
`secrets/watchdog-paper.env.example` and fill in a second Alpaca paper key pair
before the first `make up`.

Honest limit: `secrets/` is mounted into both containers, so this separates
blast radius and attribution, **not** file access. A compromised engine could
read the watchdog's key file today. Per-file mounts are the Phase 5 hardening.

For the same reason the watchdog has **no `depends_on`** and its own
`restart: unless-stopped`: it must be running precisely when the engine will not
start. Gating a supervisor on the health of the thing it supervises is how you
end up unsupervised on the worst day.

## What is still NOT covered

Read this section as the list of things that will bite first.

- **The drill proves the mechanisms, not the deployment.** Scenario 2 asserts
  the DB state a watchdog reads — an overdue heartbeat past the grace — using a
  throwaway db, because writing a fake starved beat into the live supervision
  store would make the real watchdog cancel real orders. That the watchdog
  *process* wakes up, decides, and cancels is only proven by running `make
  drill` with the stack up and watching `make watchdog-logs`. Nothing verifies
  it automatically yet.
- **The grace values are joined by a Makefile, not by code.** The risk package
  deliberately cannot read `config/watchdog.yaml`, so `make drill` greps
  `heartbeat_grace_s` out of it and passes `--grace-s`. Invoke `nwt-risk drill`
  directly and you get the built-in default (120s) instead. The drill prints the
  grace it asserted against — read it, do not assume it.
- **The scheduler and attended commands share no mutex.** `make cycle` while
  `nwt-risk run` is mid-cycle can produce two decision passes over the same
  books. The duplicate window, the one-open-entry rule, and the rate limits are
  the only things standing between that and a double submission.
- **Nobody watches the watchdog — yet.** Docker restarts it if the container
  dies, but a process that stays up while wedged looks identical to a healthy
  one from outside. `healthcheck_url` in `config/watchdog.yaml` is the intended
  external eye (a dead-man ping per clean cycle) and it ships as `null`. Until
  you point it somewhere, a dead watchdog is silent.
- **Engine alert delivery is stderr + JSONL only.** An EMERGENCY at 03:00 sits
  in `data/alerts.jsonl` until a human looks. The outbox is durable and
  at-least-once; the *delivery* is not yet anywhere you would see at night. The
  watchdog has its own `webhook_url`, also `null` by default.
- **Agents** (analyst, reports, Edge Lab) arrive in later phases, each in their
  own container with their own credential scope. None of them exist yet.
