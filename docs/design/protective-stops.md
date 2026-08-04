# Protective Stops — Implementation Design

**Repo state:** HEAD is now `276d1d2` (clean). The surveys were written against `a4fcb65` / `0e29157`; line numbers have drifted. Every load-bearing claim below was re-verified against current HEAD. One survey claim is stale and worth correcting up front: `OrderTicket.tif` at `/Users/brentt/projects/new-world-trading/engine/src/nwt_engine/domain/orders.py:61` **already** reads `Literal["day", "ioc", "gtc"]` — the crypto sleeve landed it in `896d8a6`. GTC is a config decision, not a schema change.

---

## 1. The decision

Build a **standalone server-side GTC `stop` sell order, one per (sleeve, symbol) lot, at a fixed 25% below that lot's realized average cost, armed after the entry fills, never trailed, never tightened.**

**The surveys disagree twice and I overrule the Alpaca survey on the first and the policy survey on the second.**

**Disagreement A — OTO leg vs. standalone order. Standalone wins.** The Alpaca survey correctly establishes that `order_class="oto"` with a lone `stop_loss` leg is supported for equities (HIGH confidence), and that's a real finding. But it doesn't survive contact with our execution path. Four reasons, in descending weight:

1. **We must build the standalone re-protect path regardless.** GTC orders die at Alpaca's 90-day cap. `nwt-risk kill` cancels them all (`risk/src/nwt_risk/cli.py` already prints `"POSITIONS UNPROTECTED"`). Positions predating this change have no leg. A restart after a crash finds naked positions. OTO covers only the "fresh entry, fully filled, whole shares, equity" case — a strict subset. Building both means two mechanisms, two test surfaces, two ways to be wrong about the same invariant.
2. **OTO forces our *entries* to GTC.** Leg TIF inheritance is unverified, but the only safe assumption is that legs inherit the parent — which means a DAY entry yields a stop that evaporates at 16:00, defeating the whole exercise. So entries become GTC, and a marketable limit that fails to fill now rests overnight unattended. That directly reverses the deliberate design intent at `orders.py:59-60` and widens exactly the surface we bounded. Standalone stops leave entries on `day`.
3. **Partial fills.** If the forum report is right (LOW confidence, but the failure is severe), an OTO stop leg doesn't arm until the entry fills *completely*, and you can't protect the filled portion in the meantime. A standalone arm sizes to whatever actually filled.
4. **It keeps `_order_body` flat.** `_order_body` at `/Users/brentt/projects/new-world-trading/engine/src/nwt_engine/broker/alpaca/http_broker.py:250` is typed `dict[str, str]`. An OTO body needs a nested `stop_loss` object and a signature widening. A standalone stop needs exactly two more string keys: `type: "stop"`, `stop_price: "<decimal>"`. That is not decisive on its own, but it is a real signal that standalone is the smaller change.

The cost of standalone is a real gap between fill and arm. That gap is bounded by the cycle and covered by the watchdog (§4, phase 6). I'd rather have a measurable, monitored, seconds-to-minutes gap than an unmeasurable dependency on undocumented leg semantics.

**Disagreement B — `stop` vs `stop_limit`. Plain `stop` for equities.** The policy survey chose `stop_limit` with a 10% offset, reasoning from `contracts/src/nwt_contracts/intents.py:67` ("equity/ETF intents require a limit price (no market orders)"). That constraint is real — I verified it — but it is *our* rule about *our* order type, and it does not bind here: we would submit Alpaca `type=stop`, not `type=market`. The rule exists to stop us from firing unpriced entries into a thin book. A catastrophe exit is the opposite case. A stop-limit that gaps through its limit sits unfilled while you hold the entire decline, with a resting sell that will execute on the bounce — the exact failure the control exists to prevent, dressed up as protection. The policy survey names this outcome in its own config comment ("that case must PAGE") and then designs it in anyway.

The counter-argument — flash-crash fills — is weakened by LULD: SPY/QQQ/IWM are Tier-1 NMS securities with price bands that *pause* trading rather than print absurd fills, and Level-3 halts the market entirely at −20%. *(LULD mechanics are from general knowledge, not verified against a primary source; see §9.)* Take the certain fill.

Change `intents.py`'s validator to admit a protective intent carrying `stop_price` in place of `limit_price` — narrowly, with its own guard rails — rather than smuggling a stop through `limit_price` (which would also collide with `PriceCollarCheck`, see §4).

---

## 2. What the stop is FOR

**It exists because nothing else in this system can close a position without a living human.** I re-verified the policy survey's finding 0b and it holds:

- `drawdown_halt_pct: 10.0` → HALTED. HALTED blocks *flow*. It does not sell.
- The watchdog cancels orders at the broker and writes one HALT row. It has no flatten path — `watchdog/src/nwt_watchdog/monitor.py:1-19` says so explicitly.
- `close_all_positions` has one caller, `nwt-risk flatten`, behind a typed challenge phrase.

Every breaker in `config/risk.yaml` is a flow control. If the machine suspends at 09:40 holding six ETF lots, every control fires correctly and **nothing happens to the positions**. That already happened once: `config/watchdog.yaml:16-19` documents `heartbeat_grace_s` being raised 180 → 600 after a laptop suspend pushed beats 4–11 minutes late and cost a trading day. The engine wasn't wedged; the host was asleep.

**Explicitly, what it is NOT for:**

- **Not drawdown control.** `drawdown_warn_pct` / `drawdown_halt_pct` own that, at the right altitude — the whole equity curve, not one leg of one sleeve. The policy survey measured the tactical version and it is negative at every distance tested: an 8% stop on `trend_etf` would have realized −8% on trades that averaged **+9%**; momentum's stop victims lost less than the stop would have locked at every distance from 8% to 15%; mean_rev lost 23–32% of per-trade edge to a 5% stop on two of three symbols.
- **Not gap protection.** Our universe is eight of the most liquid, most diversified ETFs in existence. There is no idiosyncratic tail — no fraud, no bankruptcy, no −40% earnings gap. A move big enough to matter is a market-wide event, and in a market-wide event the stop sells into the worst liquidity of the decade.
- **Not a trading rule.** Its design target is **zero triggers while the system is alive**. A stop that fires during normal operation is an untested strategy operating in a regime nobody validated, and it drags the whole system with it via `cooldown_after_stop_h` and the `consecutive_losses` breaker. This is the same principle already written into `watchdog.yaml:6-9`.

**The uncomfortable part, which neither survey states plainly:** the premium is not zero. When this stop fires, by construction it fires in a crash with no operator present, and it sells at the worst possible moment. For an orphaned `mean_rev` lot — bought deliberately into a decline, intended for a ≤10-day hold, with no thesis at all for being held longer — that is unambiguously right. For an orphaned `trend_etf` lot, which is at least still a position the strategy would endorse, it is arguably wrong. **We are buying a bounded-loss guarantee and paying for it in tail expected value.** The trade is worth it only because the distance is wide enough that the premium is near zero over the sample we have. If the distance ever tightens, that reasoning collapses and the entire policy must be re-derived.

Finally: **the unprotected-position detector is probably worth more than the stops.** The 2026-08-04 incident was caught. The scenario the stop covers is the one where nothing catches it — and the detector is what tells us that scenario is happening. Build both; treat the detector as the deliverable, not the garnish.

---

## 3. Per-sleeve policy

| Sleeve | Stop | Distance | Basis | Rationale |
|---|---|---|---|---|
| **control** | **NONE** | — | — | `buyhold.py` trades once, ever. After day one it has **zero dependence** on the engine, our data, or our logic — the failure mode stops exist for does not apply to a sleeve with no order flow. What remains is market risk, which is the deliverable, bounded and known at $2,500. A stop makes the benchmark not a benchmark. Decisive counterfactual: the no-stop path for every *other* sleeve is computable from a formula; the realized-with-costs benchmark is not. Measure the one you cannot compute. |
| **trend_etf** | Yes | 25% | Lot avg cost, fixed | Worst per-trade adverse excursion in 10.6y: **−10.25%** (n=55). Any stop tighter than ~12% is a measured tax; ≥12% never fires. It is a step function with nothing in between, so the only defensible placement is deep in the free region where it stops being a trading rule. |
| **momentum** | Yes | 25% | Lot avg cost, fixed | Worst holding-month excursion: **−19.86%** (n=211). Harmful at every distance from 8% to 15%, monotonically approaching zero harm as it widens. Note the momentum-stop literature (Han/Zhou/Zhu 2016; Kaminski/Lo 2014) points the other way but is about long-short single-stock books where crash risk lives in the short leg (Daniel/Moskowitz 2016); we are long-only broad ETF. |
| **mean_rev** | Yes — **highest priority** | 25% | Lot avg cost, fixed | Theoretically the worst sleeve for a *tactical* stop (a stop sells after a decline; the strategy buys after a decline — the entries are systematically deeper-drawdown by construction, p05 MAE −9.96% SPY / −11.06% IWM vs. −6.86% / −8.81% unconditional). And simultaneously the **best** sleeve for a *catastrophe* stop: an orphaned mean_rev lot is the most dangerous object in the system. Its `time_stop_days: 10` never fires if the engine is dead. |
| **llm_analyst** | Yes | 25% | Lot avg cost, fixed | Not measured — no trade history. Enable at the same distance for uniformity. If its universe ever includes single names, 25% is *too wide* for idiosyncratic risk and the policy needs re-deriving (owner decision, §8). |
| **crypto_momo** | **NONE (initially)** | — | — | Alpaca crypto supports only `stop_limit`, in a 24/7 market with no halts and no LULD — the gap-through-limit failure is unavoidable and constant. Controlled by size instead (`crypto_sleeve_max_usd: 750`). See §7. |

**The wrinkle neither survey connected: the broker has one position per symbol, not one per sleeve.** `control` holds 3 SPY; `momentum` holds 1 SPY. Alpaca sees 4 SPY. Consequences:

- Protective orders must be **per-(sleeve, symbol) lot**, not per-symbol, or a single stop would liquidate `control`'s shares in violation of the row above. That means multiple resting sell stops on the same symbol at different prices — legal at Alpaca (it does held-for-orders accounting) but it inflates open-order count.
- **Protective orders must never be netted.** `build_net_plans` in `engine/src/nwt_engine/sleeves/allocator.py` must exclude them outright; a netted stop has no owning sleeve and `allocate_fill` would mis-attribute the exit, which raises `LedgerInvariantError` inside `store.apply_entry` and takes down the cycle.
- Broker tax lots and sleeve ledger lots will diverge (broker sells FIFO; our ledger assigns to the owning sleeve). Economically identical for fungible shares; a reporting wrinkle, not a correctness one. Note it and move on.

---

## 4. Implementation plan

### Phase 0 — Prerequisites (must land and bake before a single GTC order rests)

These are not stop features. They are the reasons a resting stop would break us today.

**P0.1 — `run_cycle` reconciles before it polls.** `/Users/brentt/projects/new-world-trading/risk/src/nwt_risk/paper.py` — `run_cycle` calls `reconcile_and_arm(ledgers)` and **early-returns** on failure, then calls `poll_fills(ledgers)`. Verified at HEAD. A stop that fires overnight strands its fill, reconcile HALTs on the position mismatch, and the fill is never applied — so the HALT persists until a human runs `nwt-risk poll`. Swap the order: poll, then reconcile. `Scheduler._collect_and_reconcile` already gets this right; `run_cycle` does not. **This bug is currently unreachable because everything is `tif="day"`. A resting stop is precisely what makes it reachable.**

**P0.2 — Insert a poll between the open and the cycle.** `risk/src/nwt_risk/scheduler.py` `plan_next`, with `config/schedule.yaml` (`cycle 09:35`), returns `("cycle", cycle_at)` when the clock is open and `et < cycle_at`. There is no poll in the 09:30–09:35 window. A GTC stop firing at the open strands its fill and HALTs the 09:35 cycle.

**P0.3 — `cancel_all` must parse its 207.** `http_broker.py:143` is `self._request("DELETE", "/v2/orders").raise_for_status()`. 207 is 2xx, so per-item 500s ("no longer cancelable") pass silently. `close_all_positions` (same file, ~line 226) already does this correctly — mirror it. **The kill switch currently cannot tell you whether the stops actually died.** After this change it can, and it must be surfaced in the `cli.py` "POSITIONS UNPROTECTED" message.

**P0.4 — Make the `paper_orders` INSERT explicit-column.** `paper.py` `record_order` is `INSERT OR REPLACE INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)` against an 11-column table. Adding a column silently corrupts this. Name the columns before touching the schema.

**P0.5 — Ingest sanity filter.** `engine/src/nwt_engine/data/ingest/alpaca_stocks.py`. `data/parquet/ohlcv/alpaca/1d/SPY.parquet` bar 2026-02-03 carries `low=69.005` against `open=689.58 / close=695.41` — a decimal shift. IWM 2020-03-17 is a second suspect. This is latent today because all four strategies are close-based. **It stops being latent the moment SimBroker evaluates stops against bar lows: that one print triggers a 25% stop on every SPY lot in every backtest.** Reject bars where `low < min(open, close) × 0.7` or `high > max(open, close) × 1.3`; fail the ingest loudly rather than dropping silently.

**P0.6 — The paper experiment.** Before writing arming code, settle empirically, in the paper account: (a) does a standalone GTC sell `stop` on an equity survive the close and appear `open`/`held` the next morning; (b) does `type=stop` (no limit) get accepted at all given our submission path; (c) does Alpaca reject `order_class` != simple and `type=stop` for crypto; (d) do multiple resting sell stops on one symbol coexist up to the position size. Half a day of work; all four are load-bearing.

### Phase 1 — Types (every one is `frozen=True`; all additions are optional-with-default, so no call site breaks)

| File | Change |
|---|---|
| `contracts/src/nwt_contracts/intents.py:37` `OrderIntent` | Add `stop_price: Decimal \| None = None`. Amend `_qty_xor_notional` (`:53`): a protective intent may carry `stop_price` **in place of** `limit_price`; and add hard invariants — `is_protective ⇒ side is SELL`, `⇒ reduces_position is True`, `⇒ stop_price > 0`. This is the seam. Get it right here and the rest is plumbing. |
| `intents.py:71` `ApprovedOrder` | Add `_never_widen_stop` alongside `_never_size_up`. Today `_never_size_up` clamps qty/notional only; **nothing prevents a governor from loosening a stop.** |
| `intents.py:103` `OrderRef` | Add `stop_price` and `is_protective`. Without these, `ExposureCheck`/`DuplicateCheck` cannot distinguish a resting stop from a working entry. |
| `engine/src/nwt_engine/domain/orders.py:52` `OrderTicket` | Add `stop_price: Decimal \| None = None` and `order_type: Literal["limit","market","stop"] = "limit"`. `tif` already admits `gtc` — no change. |
| `orders.py:70` `Fill` | Add `protective: bool = False`. Needed for the `stop_out` wiring below. |
| `engine/src/nwt_engine/sleeves/allocator.py:30/:45` `Leg` / `NetPlan` | Protective legs are **excluded from netting**, not represented in it. Assert this rather than encoding it. |
| `risk/src/nwt_risk/context.py:30` `RecentOrder` | Add `is_protective`. |

### Phase 2 — Broker wire

`http_broker.py` `_order_body`: when `ticket.order_type == "stop"`, emit `type: "stop"` and `stop_price: str(ticket.stop_price)`, omit `limit_price`. Signature stays `dict[str, str]`. Add an explicit guard rejecting `order_type == "stop"` for crypto assets until P0.6(c) says otherwise.

### Phase 3 — Arming

New module `risk/src/nwt_risk/protect.py`, one function: `plan_protection(ledgers, live_protective_orders, cfg) -> (arms, cancels)`. It is a **declarative reconciler**, not an event handler — it computes desired coverage per (sleeve, symbol) from the folded ledger, diffs against what is actually resting at the broker, and emits the difference. This is the property that makes it correct after a crash, after a kill, after a 90-day expiry, and for positions that predate the feature, with no special-case code for any of them.

- Call it in `run_cycle` **after** `poll_fills`, **before** strategy proposals.
- Stop price = `lot_avg_cost × (1 - distance_pct/100)`, quantized to the tick, from `SleeveLedger`, never from bar data.
- `client_order_id` scheme `prot-{sleeve}-{symbol}-{seq}` so `poll_fills` attribution works and `reconcile`'s `external_order_ids` check recognizes them as ours.
- `paper_orders` gains `is_protective`, `stop_price`, `protects_sleeve` (after P0.4).
- Wire the `stop_out` flag: `paper.py` hardcodes `"stop_out": False` in the round-trip `BreakerEvent`. Set it from `Fill.protective`. Today a real stop-out would receive `cooldown_after_exit_h` (4h) instead of `cooldown_after_stop_h` (24h).

### Phase 4 — Governor

**Note first: `is_protective` is dead code today.** Grep confirms `PositionSizer` never sets it; nothing else constructs an `OrderIntent`. The three existing relaxations (`checks/rate_limit.py:44`, `checks/session.py:51`, `checks/long_only.py:36`) have never fired outside the test suite. Phase 3 is their first production setter — expect surprises there, not just in the new code.

Five checks would block or damage a protective arm. None consults `is_protective`:

1. **`checks/price_collar.py`** — verified: it early-returns `allow` when `limit_price is None`. So a stop threaded as `stop_price` **bypasses the collar entirely** and an unvalidated price reaches the broker; threaded as `limit_price` it is auto-rejected for being >2% away. Both routes are wrong. Fix: add an explicit protective branch that validates `stop_price` as a **two-sided band around the intended distance measured from the lot's avg cost** — reject unless it lands in `[avg_cost × (1−0.30), avg_cost × (1−0.20)]`. A one-sided `price_collar_pct_protective: 40.0` (the policy survey's proposal) would happily accept a stop 1% away, which is a fat-finger away from a tactical stop nobody chose.
2. **`checks/staleness.py`** — no bypass. Exempt protective arms from the **quote-age and missing-quote** branches only: a protective arm's price comes from the ledger, not the market, so it needs no fresh quote. Do **not** exempt clock skew or `last_reconcile_age_s` — those bear on whether the ledger itself is trustworthy.
3. **`checks/order_size.py`** — `min_notional_usd: 25` rejects; `max_notional_usd: 1000` clamps. Exempt protective from both. A stop that covers less than the full lot is not protection, and a $20 residual must still be stoppable.
4. **`checks/session.py:39-40`** — `regular_hours_only: true` rejects any equity intent outside RTH; the crypto branch has a bypass, the equity branch does not. Needed so the 16:05 EOD poll can arm same-day rather than leaving every late fill unprotected overnight. Scope the exemption to protective SELL stops.
5. **`checks/duplicates.py:19-33`** — `rolling_window_s: 90` rejects a repeat `(symbol, side, sleeve)` with no protective exemption. A re-arm inside 90s rejects. Exempt.

Plus one that is a policy decision, not a fix: **`checks/state_gate.py` + the structural gate at `governor.py:75`**. In HALTED, `TradingState.allows` returns `False` unconditionally, so a protective arm is dead. Recommend allowing protective *arming* (never widening, never cancelling) in HALTED — see §8.

### Phase 5 — The exit interlock (highest-risk piece)

A resting sell stop reduces Alpaca's sellable quantity. The strategy's own SELL limit is then rejected for insufficient qty. Five rejections in ten minutes → `rejection_count: 5` → **HALT**. A naive implementation halts the engine on the first normal `trend_etf` Friday rebalance.

In `paper.py:_submit`, before any reducing order for a (sleeve, symbol) with a live protective order: cancel the protective order → **poll until the cancel is confirmed terminal** → then submit the exit. If not confirmed within a bounded window, **skip the exit this cycle and audit** — never submit on an unconfirmed cancel. The interlock creates a seconds-long unprotected window that must be audited, and it adds a round trip to every strategy exit. Both are real costs, both are unmodeled in the backtests (§6).

This deserves its own integration test against the paper account before anything else ships.

### Phase 6 — Watchdog (the actual deliverable)

Land `monitor.py`'s deliberately-missing invariant. **But it cannot be computed from broker state alone** — the broker sees one SPY position, not `control`'s 3 and `momentum`'s 1, and the watchdog must stay independent of the engine's database. Resolution: the watchdog checks, per symbol, `sum(open sell-stop qty) >= position_qty - allowance[symbol]`, where `allowance` is a **static, human-maintained map in `config/watchdog.yaml`** (`SPY: 3` for control's lot, crypto symbols fully exempt, fractional remainders exempt). Static config preserves independence; the cost is that a human must update it when the control sleeve is resized, and a stale allowance makes the check quietly toothless. Add a companion check that the allowance is not larger than the position.

Also: raise `max_open_orders`. `watchdog/src/nwt_watchdog/invariants.py:90` `open_order_count` counts every resting stop against a cap sized for working entries; ~12 positions with per-sleeve stops is a permanent floor of open orders. Land the coverage check in WARN for two weeks, then promote to CRITICAL at `max_unprotected_h: 26` (one overnight plus margin).

### Phase 7 — SimBroker

See §5.

---

## 5. Backtest parity

`SimBroker` has no stop modeling at all. Verified in `engine/src/nwt_engine/broker/sim/broker.py`: `_try_fill` fills once per bar anchored at `bar.open`, using `bar.low`/`bar.high` only as a *touch test* for limits — the sell branch is `bar.open >= limit or bar.high >= limit`, which is exactly backwards from stop geometry. `_OpenOrder.__slots__` is `("ticket","state","accepted_ts")` — no trigger state.

**Required for parity:**

1. **Trigger geometry.** A protective sell stop at S triggers when `bar.low <= S`. This is a separate code path, not a tweak to the limit branch.
2. **Fill price.** `min(bar.open, S)` — a gap-down opens *through* the stop and fills at the open, materially worse than S. Then apply an explicit catastrophe-slippage haircut (default 100 bps, configurable, distinct from the normal `slip_price`).
3. **Two-phase state.** Add a triggered flag to `_OpenOrder`; a stop is arm → trigger → market.
4. **GTC persistence.** `expire_day_orders` already keeps `tif != "day"`. But `paper.py` and `experiments/runner.py` both hardcode `tif="day"` at their `OrderTicket` construction sites — both must pass through the ticket's TIF.
5. **Two independent submit sites.** There is no chokepoint: `paper.py` (`OrderTicket(...)` then `broker.submit`) and `engine/src/nwt_engine/experiments/runner.py` (~line 470) are parallel implementations. The arming logic must be shared or it will diverge.
6. **Intrabar ordering is a modeling choice, not data.** If a sleeve's limit buy and another's stop sell land on the same daily bar, OHLC cannot say which came first. Pick a conservative rule (stops resolve first), document it, and stop claiming parity for that case.

**How wrong the daily-bar approximation is — plainly:**

Very wrong, and wrong in the direction that flatters us, on exactly the days that matter. MAE from daily lows tells you a level was *touched*, not that you *filled* there. A 25% stop only triggers on a crash day, and crash days are precisely the days with the largest intrabar range: SPY's worst single-day gap in the tape is −10.7%, EFA −11.6%. **A backtest reporting a fill at −25% could correspond to a real fill at −32% or worse.** On an overnight gap the model's `min(bar.open, S)` is directionally right but still optimistic — real fills come at the first print after the open, not the official open, and in a −20% gap that spread is wide. There is no daily-bar convention that fixes this; the honest move is to be deliberately pessimistic and to amend the parity claim in `engine/src/nwt_engine/broker/base.py:37-40` to explicitly exclude stop fills.

This asymmetry is convenient: it makes the no-tactical-stop conclusion *stronger* (every tactical-stop return in the policy survey is optimistic), and it makes the catastrophe stop's modeled cost a floor rather than an estimate.

---

## 6. What this invalidates

**Short answer: nothing, if the distance stays at 25% — and that is exactly why the backtests must be re-run.**

The 25% distance was chosen to be a no-op over the sample: trend's worst per-trade excursion is −10.25%, momentum's worst holding-month −19.86%, mean_rev's 1st-percentile 10-day excursion −11.06%. If the implementation is correct, re-running the decade backtests with stops enabled should produce an **identical** equity curve. **The re-run is a test of the implementation, not a re-estimation of the strategies.** A non-zero delta is a bug signal, and that is the highest-value thing the re-run produces.

Four caveats that make it non-optional:

1. **The per-sleeve analysis was a pandas proxy, not our engine.** It excluded whole-share rounding, the marketable-limit buffer, `SimBroker`'s next-open fill model, costs, `min_notional`, and every governor rejection. The 12% cliff for trend and the 20% cliff for momentum are the numbers to reproduce through `nwt backtest`.
2. **P0.5 is a hard prerequisite.** Until the ingest filter lands, `SimBroker` evaluating stops against `SPY.parquet` 2026-02-03 (`low=69.005`) will stop out every SPY lot in every backtest. Any re-run before that is garbage.
3. **The machinery is unmodeled.** Cancel-before-exit round trips add latency to every strategy exit; re-arm orders consume `rate.global_per_day: 40`. Neither appears in any existing backtest. These are the stop's actual cost and they have never been measured.
4. **The tape contains one bear market.** Data starts 2016. A 25% stop that "never fired in-sample" has never seen a −49% or −56% drawdown. It would have fired in 2008 and in 2000-02. That is the intended behavior — **but you should watch it fire in a synthetic or extended-history stress run before trusting it live.** "Never fired" is a fact about 2016–2026, not a property of the strategies.

**Any future change to `distance_pct` invalidates everything above and requires a full re-run.** Encode that in the config comment.

---

## 7. Crypto

**What we can do:** our standalone design is actually better-positioned for crypto than OTO would have been — it needs no `order_class` at all, and crypto's TIF set (`gtc`, `ioc`) natively includes `gtc`. So the *shape* fits.

**What we cannot do:** Alpaca crypto appears to support only `stop_limit`, not plain `stop` (MEDIUM confidence — from a reference table, never stated as a prohibition). That forces the exact instrument we rejected in §1, in a 24/7 market with no circuit breakers, no LULD, and no closing auction to reset anything. A gap through the limit leaves an unprotected position with a resting sell that will fill on the bounce. There is no mitigation available at the broker.

**Recommendation:** `crypto_momo: { enabled: false }`. Control by size — `crypto_sleeve_max_usd: 750` is the actual risk control there. Revisit only if P0.6(c) shows plain `stop` is accepted.

**Fractional equities are structurally unprotectable, and this conflicts with a live recommendation.** Alpaca requires `time_in_force=day` for all fractional orders (HIGH confidence). A `day` stop dies at the close. **Therefore a fractional equity position cannot carry a stop that survives the close, by any mechanism.** Partial mitigation: on a holding of 3.4 shares, place a GTC stop for the 3 whole shares and accept that 0.4 is naked. This directly collides with the policy survey's proposed fix for the `trend_etf` / `mean_rev` sizing defect (`trend_etf` allocates $500/symbol against SPY at $769 → 0 shares; `mean_rev` can only ever hold 1 share of IWM) — one of its three options was "adopt fractional shares." **Adopting fractional equities and having universal server-side stops are mutually exclusive.** Owner decision, §8.

**Consequence for the watchdog invariant:** it can never read "every open position has a live protective stop." It must read "every open position has a live protective stop **except** the enumerated allowance," with crypto symbols, `control`'s SPY lot, and fractional remainders listed as explicit numbers in `config/watchdog.yaml`. An invariant with implicit exemptions is a lie; one with a static, human-auditable exemption table is a control.

---

## 8. Open decisions for the owner

| # | Decision | Recommendation | Trade-off in one line |
|---|---|---|---|
| 1 | Does `control` get a stop? | **No** | Benchmark integrity vs. an unprotected $2,300 SPY lot and a watchdog invariant that needs a hand-maintained exemption. |
| 2 | Distance: 25% fixed | **25%, fixed, from lot avg cost** | It never fires in-sample — which is the point, and also means it has never been tested by anything real. |
| 3 | `stop` vs `stop_limit` for equities | **Plain `stop`** | Certain fill at an uncertain price, vs. a certain price you may never get while holding the whole decline. |
| 4 | Allow protective *arming* in HALTED? | **Yes** — arm only; never widen, never cancel | Weakens the clean "nothing flows in HALTED" invariant, vs. leaving positions naked exactly when things are already wrong. |
| 5 | What happens when a stop fires? | **HALT the system**, not just `cooldown_after_stop_h: 24` | A catastrophe stop firing is by construction an event outside eleven years of data and deserves a human; the cost is a manual restart. |
| 6 | Fractional equities vs. universal stops | **Stay whole-share**; fix the sizing defect by raising sleeve budgets or cutting symbol counts | Realistic sleeve sizing vs. protection coverage — you cannot have both, and `trend_etf` is currently a four-ETF strategy pretending to be six. |
| 7 | Crypto stops | **Off** until P0.6 proves otherwise | The crypto sleeve stays unprotected, vs. shipping a stop-limit we already know fails in the case it exists for. |
| 8 | `llm_analyst` at 25% | **Yes for now**, revisit if its universe includes single names | Uniformity and simplicity, vs. a distance calibrated for broad ETFs applied to instruments with a real idiosyncratic tail. |
| 9 | Ship order | **Detector before stops** — land the watchdog coverage check in WARN mode first, with allowance = full position | You get the visibility (the actually valuable part) two weeks earlier and calibrate the alert before it can page; the cost is a period where you can see the gap and not close it. |

---

## 9. Risks and unknowns

**Unverified at Alpaca — must be settled by P0.6, not by argument:**

1. **Does a standalone GTC equity sell `stop` actually rest across the close and remain live the next morning?** The docs say GTC means GTC and I have no reason to doubt it, but the entire design depends on it and it costs one paper order to prove. Also unproven: whether multiple resting sell stops on one symbol coexist up to the position size, which the per-sleeve-lot design requires.
2. **Whether OTO/bracket legs inherit the parent's TIF.** Not stated anywhere in Alpaca's docs. Only evidence is an unverified community post plus the existence of a feature request for per-leg TIF. **This is moot for our chosen design** — which is part of why I chose it — but if we ever revisit OTO it is the first question.
3. **Whether crypto rejects `order_class != simple` and `type=stop`.** Implied by a reference table cell, never stated as a prohibition anywhere in the docs. Our crypto policy (stops off) is safe under either answer, but the guard in Phase 2 is written on an assumption.
4. **The OTO partial-fill protection gap.** Forum-only, staff attribution claimed but unconfirmed. Moot for our design; noted so nobody re-derives OTO later without re-checking it.
5. **Whether Alpaca's minimum-distance rule ($0.01 from base price, HIGH confidence) has any percentage-based companion.** No percentage constraint found, but that is absence of evidence. A 25% stop is far from any plausible minimum, so low risk.

**Unverified in our own analysis:**

6. **Sample size.** 55 trend trades over 10.6 years; the 8%-stop row that carries the "tactical stops are a tax" argument rests on **4 trades**. The *shape* is robust (the "free above 12%" side is a hard fact about max MAE, not an estimate); the point estimates are not. Momentum's 211 holding-months are better but overlapping.
7. **One bear market.** Everything about "never fires" is a statement about 2016–2026. No 2000-02, no 2007-09.
8. **The IWM mean-rev anomaly** — a 5% stop *improves* IWM per-trade return from +0.226% to +0.517%, the one result contradicting the policy. Reads as noise (one symbol of three, non-monotonic across distances, overlapping windows) but was not bootstrapped. `research/src/nwt_research/bootstrap.py` exists for this.
9. **LULD mechanics.** My §1 argument that price bands pause rather than print absurd fills, and that Level 3 halts at −20%, is from general market-structure knowledge and was **not** verified against a primary source. It is the main mitigant for choosing plain `stop` over `stop_limit`. Verify before treating it as load-bearing.
10. **The momentum stop-loss literature** (Han/Zhou/Zhu 2016, Kaminski/Lo 2014, Daniel/Moskowitz 2016, Faber 2007, Clare et al. 2013) was cited from memory in the policy survey and not checked. My reading — that it concerns long-short single-stock books where crash risk lives in the short leg, and therefore does not apply to a long-only broad-ETF sleeve — is a first-principles mechanism argument. It is the place this whole document is most likely to be wrong.

**Known-unknown in the implementation:**

11. **`is_protective` has never executed in production.** The three existing relaxations in `rate_limit.py`, `session.py`, and `long_only.py` are real code with real tests that have never fired against a live broker. Phase 3 turns them all on at once.
12. **The cost of the machinery is asserted, not measured.** Cancel-before-exit latency on every strategy exit, re-arm orders against `rate.global_per_day: 40`, and the unprotected window between cancel-confirmed and exit-filled. All small on paper; none measured.
13. **A stop firing into a crash with no operator present may be worse than holding**, for `trend_etf` specifically. That is the premium we are paying and it is not zero. It is defensible only while the distance stays wide enough that it never fires — which is an argument that dissolves the moment anyone tightens it.
