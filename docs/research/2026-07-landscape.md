# Landscape Research Snapshot — July 2026

Six-agent research pass grounding the platform's architecture decisions.
Researched 2026-07-14 → 2026-07-18. Verify time-sensitive facts (pricing, API fields, rules) before relying on them later.



---

# Broker APIs

All research complete. Compiling findings.

# US Retail Broker API Comparison for Automated Trading (researched 2026-07-14)

## 1. ALPACA (alpaca.markets)

- **API style**: REST + WebSocket streaming (trade updates + market data). Developer-first; API *is* the product. https://docs.alpaca.markets/us/docs/trading-api
- **Python SDK**: Official, actively maintained: `alpaca-py` (https://github.com/alpacahq/alpaca-py). Legacy `alpaca-trade-api-python` is deprecated in favor of alpaca-py (primary SDK since 2023).
- **Paper trading**: Best-in-class for this use case. Same API spec, switch by changing base URL to `https://paper-api.alpaca.markets` + separate keys. Real-time simulation using IEX real-time data; simulated fills when order becomes marketable; injects partial fills ~10% of the time; resettable balance (default $100k); supports margin/shorting in paper. Explicit documented caveats: no simulation of dividends, borrow fees, market impact, queue position, slippage, or regulatory fees (https://docs.alpaca.markets/docs/paper-trading). Options up to Level 3 (multi-leg) and crypto both enabled by default in paper (https://docs.alpaca.markets/changelog/multi-leg-level-3-options-trading-in-paper, https://alpaca.markets/blog/level-3-options-trading-now-available-with-alpacas-trading-api/).
- **Commissions**: $0 for US-listed stocks/ETFs/options via API for self-directed accounts (https://alpaca.markets/support/commission-clearing-fees). Short-sale fee ~$0.65/trade + borrow fees on HTB names.
- **Fractional shares**: Yes, notional orders from $1 on 2,000+ equities; no fractional short sales (https://docs.alpaca.markets/us/docs/fractional-trading).
- **Crypto**: Yes, native, 49 US states; maker/taker 0.15%/0.25% at lowest volume tier (https://docs.alpaca.markets/us/docs/crypto-fees, https://alpaca.markets/support/crypto-maker-taker-gmt-faq).
- **Market data**: Free tier = IEX-only real-time (~2-3% of consolidated volume — thin quotes), 200 data calls/min, 30 websocket symbols, 7+ yrs history, indicative OPRA options data. "Algo Trader Plus" $99/mo = full SIP (all US exchanges), up to 10,000 calls/min, unlimited websocket symbols, real-time OPRA (https://alpaca.markets/data).
- **Minimums / margin**: $0 account minimum; Reg-T: $2,000+ equity required for margin/shorting, else 1x cash buying power (https://docs.alpaca.markets/us/docs/margin-and-short-selling). Margin rate 6.5% (5% Elite $100k+) per brokerchooser review.
- **Auth**: API key/secret pair per environment; OAuth2 exists but is for third-party apps connecting users' accounts (https://alpaca.markets/support/usage-limit-api-calls).
- **Rate limits**: Trading API 200 req/min per account (can request increase; 1,000/min reported on paid/Elite tiers — secondary sources: https://forum.alpaca.markets/t/is-there-a-way-to-increase-the-200-min-api-call-limit-for-the-trading-endpoint/18110, https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading-in-the-united-states).
- **ToS on personal automation**: Explicitly the intended use ("Algorithmic Trading API" — https://alpaca.markets/algotrading). No issues.
- **Caveats**: PFOF-based execution (execution quality below IBKR); free IEX feed can mislead paper fills for illiquid names; broker is fintech (Alpaca Securities LLC, FINRA member, SIPC).

## 2. INTERACTIVE BROKERS (IBKR)

- **API style**: Three official routes: (a) TWS API — socket protocol against locally running TWS/IB Gateway software, most complete; (b) Web API / Client Portal API (CPAPI) — REST + websocket, routed through a small local Java "Client Portal Gateway" for retail, or OAuth 1.0a/2.0 direct (OAuth historically gated to institutional/third-party; retail self-service OAuth expanding but the documented retail path is still the CP Gateway — https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/, https://interactivebrokers.github.io/cpwebapi/ [github pages version now deprecated pointer]); (c) FIX (institutional).
- **Python SDK**: Official `ibapi` (TWS API) — functional but low-level/callback-based; October 2025 release v10.37 added a Synchronous Wrapper + faster websocket polling (https://blog.pickmytrade.io/ib-api-python-2026-automated-trading-setup-ibkr-integration/ — secondary source). De-facto standard community lib `ib_insync` is UNMAINTAINED (author Ewald de Wit died early 2024); actively maintained fork is `ib_async` under ib-api-reloaded org, merges as of Jan 2026 (https://github.com/ib-api-reloaded/ib_async).
- **Paper trading**: Full simulated paper account linked to live account; works with both TWS API and Web API "with minimal differences" (https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm). Documented divergences: fills simulated from top-of-book only, no depth; stops/complex orders always simulated; some order types unsupported (VWAP, Auction, Pegged-to-Market); partial-execution handling may not match exchanges; occasional better-than-limit fills reported via API (https://www.interactivebrokers.com/campus/trading-lessons/paper-trading-vs-live-trading-whats-the-difference/).
- **Commissions**: IBKR Pro Fixed: US stocks $0.005/share, $1 min; Tiered from $0.0035/share; options $0.65/contract fixed ($0.15–$0.65 tiered); crypto 0.12%–0.18% of trade value, $1.75 min (https://www.interactivebrokers.com/en/pricing/commissions-home.php). **IBKR Lite ($0 commission) accounts CANNOT use the APIs** — API requires IBKR Pro, account "fully open and funded" (https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/, confirmed in market-data pages: "Service not available for IBKR Lite clients").
- **Fractional shares**: Supported (requires enabling fractional permission; supported via API). [Moderate confidence; verify current API order support in docs.]
- **Crypto**: Yes (via Paxos/Zerohash arrangement), tradeable via API.
- **Market data**: NOT free. Real-time US data requires paid subscriptions per user (e.g., non-pro US securities snapshot bundles ~$10/mo class of fees, often waived with ~$30/mo commissions — could not re-verify exact 2026 waiver numbers; see https://www.interactivebrokers.com/en/pricing/market-data-pricing.php). Delayed data free. 100 concurrent market data lines baseline (https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/).
- **Minimums**: $0 account minimum, no inactivity fees (https://www.nerdwallet.com/reviews/investing/brokers/interactive-brokers). Margin per Reg-T ($2k); IBKR margin rates are among the lowest.
- **Auth**: TWS API = local session (TWS/Gateway must run, daily restart/re-auth is a real operational burden); CPAPI = session-based via local gateway with periodic `/tickle` keepalive; OAuth 1.0a "Extended" for approved third parties (https://www.interactivebrokers.com/campus/ibkr-api-page/oauth-1-0a-extended/).
- **Rate limits**: Web API global ~50 req/s authenticated (per hexdocs summary of IBKR docs — secondary); older CPAPI guidance 10 req/s per session on many endpoints; TWS API 50 messages/s; historical-data pacing rules (no more than ~60 hist requests/10 min) (https://interactivebrokers.github.io/tws-api/historical_limitations.html, https://interactivebrokers.github.io/tws-api/order_limitations.html). Note: IBKR Campus pages returned HTTP 403 to fetchers, so some numbers rest on secondary sources.
- **ToS on automation**: Fully supported/officially documented (https://www.interactivebrokers.com/en/trading/ib-api.php).

## 3. TRADIER

- **API style**: Clean REST + streaming (HTTP/websocket) for market data; brokerage-as-a-platform, API-first (https://docs.tradier.com/).
- **Python SDK**: No official first-party Python SDK; community wrappers only. [Verified absence as of research date via docs — docs emphasize raw HTTP.]
- **Paper trading**: Sandbox environment (`sandbox.tradier.com/v1`) with paper trading — full trading API, but market data is 15-minute DELAYED and there is NO streaming endpoint in sandbox (https://docs.tradier.com/docs/faq). This materially weakens paper/live parity for intraday strategies.
- **Commissions**: Lite $0/mo: $0.35/trade stocks, $0.35/contract options; Pro $10/mo: commission-free stocks + equity/ETF options ($0.35 index options); Pro Plus $35/mo ($0.10 index options). Assignment/exercise $9 ($5 Pro Plus). Margin rate ~9.5% (https://tradier.com/individuals/pricing).
- **Fractional shares**: Not offered (pricing/docs silent; equities are whole-share) — [high confidence, could not find any fractional support].
- **Crypto**: Not offered via brokerage API (US equities/options/ETFs only, Level 1 data only — https://docs.tradier.com/docs/faq).
- **Market data**: Included free with brokerage account (real-time L1 in production); API access free for account holders.
- **Minimums**: $0 brokerage minimum ($500 futures) (https://tradier.com/individuals/pricing).
- **Auth**: Personal API tokens that never expire for individual account holders (simple); OAuth flow reserved for approved partners (24h access tokens). "Personal use only unless you're a Tradier Partner" (https://docs.tradier.com/docs/faq).
- **Rate limits**: Production: 120/min standard + market data, 60/min trading; sandbox: 60/min; per-token per-minute with X-Ratelimit headers (https://docs.tradier.com/docs/rate-limiting).
- **ToS on automation**: Explicitly welcomed for personal use.

## 4. CHARLES SCHWAB (Trader API — Individual)

- **API style**: REST + WebSocket streaming (real-time quotes/level data); successor to TD Ameritrade API after acquisition; production since early 2024 (https://developer.schwab.com/products/trader-api--individual, https://grokipedia.com/page/Schwab_Trader_API).
- **Python SDK**: No official SDK. Strong unofficial: `schwab-py` (https://schwab-py.readthedocs.io/en/latest/auth.html), plus others (schwabdev etc.).
- **Paper trading**: **None via API.** The Trader API is live-only; Schwab's paperMoney exists only inside thinkorswim, not via API. Developer-portal "sandbox" is limited method-testing, not a simulated brokerage (https://blog.traderspost.io/article/does-schwab-have-paper-trading, https://developer.schwab.com/user-guides/apis-and-apps/test-in-sandbox, https://usethinkscript.com/threads/schwab-api-paper-trading-for-automated-trading-systems.22197/). This alone disqualifies it for "paper AND live behind same API."
- **Commissions**: $0 stocks/ETFs, $0.65/contract options (standard Schwab retail pricing); API access free with any brokerage account.
- **Fractional shares**: NOT supported via API (Stock Slices is app-only) (https://medium.com/@carstensavage/the-unofficial-guide-to-charles-schwabs-trader-apis-14c1f5bc1d57 — secondary; consistent with legacy TDA limitation).
- **Crypto**: No spot crypto.
- **Market data**: Real-time quotes, option chains, historical data included free — one of the best free-data deals (https://developer.schwab.com/products/trader-api--individual).
- **Minimums**: $0; margin per Reg-T $2k.
- **Auth**: OAuth2 (three-legged) with a notorious operational wart: refresh tokens hard-expire every ~7 days, requiring a manual browser re-login/re-grant weekly — bad for unattended automation (https://developer.schwab.com/user-guides/apis-and-apps/oauth-restart-vs-refresh-token, https://schwab-py.readthedocs.io/en/latest/auth.html).
- **Rate limits**: ~120 req/min for data; order throughput throttled (~2–4 trade req/s reported) (secondary: https://www.npmjs.com/package/@sudowealth/schwab-api, schwab-py docs).
- **ToS**: Individual Developer role = personal, non-commercial use, self-directed account; app registration + approval (days to weeks reported) (https://developer.schwab.com/user-guides/individual-developer/about-individual-developer-role).

## 5. ROBINHOOD

- **Official API = CRYPTO ONLY.** Robinhood Crypto Trading API (launched 2024): market data, account info, crypto orders programmatically; US Robinhood Crypto customers (https://robinhood.com/us/en/newsroom/robinhood-crypto-trading-api/, https://docs.robinhood.com/). v1/v2, v2 adds volume-based fee tiers (https://robinhood.com/us/en/support/articles/crypto-api/).
- **No official stocks/options API** as of research date; no announced plans (https://www.bitget.com/wiki/does-robinhood-have-an-api-for-stocks — secondary, consistent with official docs scope). Unofficial libraries (robin_stocks, sanko/robinhood reverse-engineered docs — https://github.com/sanko/robinhood) hit private endpoints; ToS-violating, account-restriction risk. Not suitable for this project.
- **Auth (crypto API)**: API key + Ed25519 keypair request signing (x-api-key, x-signature, x-timestamp headers) — well-designed. No sandbox. Rate limits not publicly documented in fetched pages [unverified].
- **Fees**: crypto fee tiers by 30-day volume (v2); no commissions on equities but irrelevant (no API).

## 6. PUBLIC.COM (Individual API Program) — newer entrant, launched June 17, 2025

- **API style**: REST; no websocket streaming as of the July 2026 changelog (https://public.com/api/docs/changelog). Hosted MCP server for Claude/ChatGPT added June 25, 2026 (interesting for LLM-in-the-loop, also a risk vector for a "hallucinating automation" failsafe design).
- **Python SDK**: Official, on GitHub since Oct 17, 2025; also official CLI (https://public.com/api/docs/sdks-and-tools).
- **Paper trading**: **None.** Confirmed absent from docs/changelog (https://public.com/api/docs, https://public.com/api/docs/changelog). Live-only.
- **Assets/fees**: Stocks, ETFs, options, index options, crypto (Nov 2025, ~0.6% each way), corporate bonds + treasuries (Mar 2026), short selling (Apr 2026), margin toggle per-order via `useMargin` (Jun 2026), 24/5 trading (Jul 2026) (https://public.com/api/docs/changelog). API free; commission-free stocks/options; options REBATES paid, $0.06–$0.10/contract, with adjusted rebate on API-traded contracts (https://public.com/api, https://public.com/invest/options-trading).
- **Fractional shares**: Yes via notional orders [high confidence — Public is fractional-native; notional crypto orders documented Jan 2026].
- **Market data**: Quotes, historical bars, option chains w/ Greeks included via API (https://public.com/api/docs).
- **Auth**: Personal access token (generated in Account Settings > Security > API) exchanged via endpoint; simple key model (https://public.com/api/docs).
- **Rate limits**: 10 req/s global (doubled from 5 on Feb 2, 2026); developer agreement reserves per-app/account "Throttles" (https://public.com/api/docs/changelog, https://public.com/disclosures/individual-api-program).
- **ToS**: Dedicated "Individual API Program" agreement — personal automated trading explicitly the product.

## 7. NEWER/OTHER ENTRANTS

- **Webull OpenAPI** (US): official; stocks, options, futures, crypto, event contracts; HTTP + MQTT (market data streaming) + gRPC (order events); official Python SDK (3.8–3.13, https://github.com/webull-inc/openapi-python-sdk); apply via "OpenAPI Management," review 1–2 business days; API free, standard app trading fees ($0 stocks/options); test environment available while awaiting approval (paper-trading fidelity undocumented); market data subscriptions sold separately (https://developer.webull.com/apis/docs/, https://www.webull.com/open-api, https://developer.webull.com/apis/docs/faq/). Credible but young; docs thinner than Alpaca/IBKR.
- **Moomoo OpenAPI**: official; requires running local "OpenD" gateway daemon; SDKs in Python/Java/C#/C++/JS; paper trading supported; marketed hard for US-stock algo speed as of Mar 2026 (https://openapi.moomoo.com/moomoo-api-doc/en/, https://www.moomoo.com/download/OpenAPI). Gateway dependency = same ops burden class as IBKR.
- **TradeStation API**: mature REST + streaming, OAuth2, and a true SIM environment identical to live except simulated fills — switch base URL api.tradestation.com/v3 → sim URL (https://api.tradestation.com/docs/fundamentals/sim-vs-live/). No official Python SDK (community: tastyware/tradestation, areed1192). API key access requires request/approval via developer portal (https://developer.tradestation.com/trading-api/). Viable but onboarding friction and equities pricing less attractive for tiny accounts.
- **E*TRADE (Morgan Stanley)**: legacy OAuth 1.0a API + sandbox still operational; no deprecation announcement found, but stagnant, no official Python SDK; not recommended for new builds (https://developer.etrade.com/home).

## RANKED RECOMMENDATION (small $5–10k account, Python, paper+live behind same API, unattended automation)

1. **Alpaca — primary recommendation.** Only broker that fully satisfies every stated requirement: identical REST/WS API for paper and live (URL+key swap), official maintained Python SDK, $0 commissions, $0 minimum, fractional, options L3 + crypto in both paper and live, never-expiring key auth suitable for unattended headless operation, automation-native ToS. Known tradeoffs: IEX-only free data (upgrade $99/mo SIP if fills/quotes matter), PFOF execution, paper fills optimistic (no slippage/queue modeling — build conservative fill assumptions into the research layer).
2. **Interactive Brokers — co-recommendation / graduation path.** Best execution, most assets, paper account with good API parity, lowest margin rates; ib_async maintained. Costs: requires IBKR Pro (per-trade commissions), paid market data, and a running gateway (TWS/IB Gateway or CP Gateway) with session-keepalive/daily-restart headaches — meaningful ops burden for a firewalled unattended box. Recommended as the LIVE execution venue later if execution quality starts to matter, keeping Alpaca for research/paper.
3. **Tradier — niche third.** Choose only if options-centric: $10/mo for commission-free options beats $0.65/contract elsewhere at volume. Sandbox's 15-min delayed data + no sandbox streaming breaks intraday paper/live parity.
4. **TradeStation** — real SIM/live parity and solid API, but approval friction, no official Python SDK.
5. **Public.com** — most interesting new entrant (free, options rebates, bonds, official SDK, MCP), but no paper environment = fails a core requirement; usable as a cheap live venue for options later.
6. **Schwab** — excellent free real-time data (worth using as a supplementary free data source even if not trading there), but no API paper trading, 7-day OAuth re-auth kills unattended operation, no official SDK, no fractional via API.
7. **Webull / Moomoo** — official but young/opaque docs, approval gates, data costs unclear; monitor, don't build on yet.
8. **Robinhood** — crypto-only official API; anything for stocks is unofficial and ToS-hostile. Avoid.

Verification notes: IBKR Campus pages 403'd (rate-limit and OAuth details rest partly on secondary sources); Schwab developer portal specifics (rate limits, sandbox scope) are login-gated — numbers cited from schwab-py/community docs; Alpaca 1,000/min elevated rate limit, IBKR fractional-via-API, Tradier no-fractional, and Robinhood crypto rate limits are moderate-confidence/unverified against primary docs as of 2026-07-14.


---

# Market Data

RESEARCH OUTPUT: MARKET DATA OPTIONS FOR PROSUMER ALGO TRADER (verified July 2026 unless flagged)

=== 1. PROVIDER-BY-PROVIDER FINDINGS ===

**ALPACA MARKET DATA** (https://alpaca.markets/data, https://docs.alpaca.markets/us/docs/about-market-data-api)
- Free "Basic" plan: real-time equities from IEX exchange only (~2-3% of consolidated market volume per https://alpaca.markets/support/data-provider-alpaca); websocket capped at 30 symbols; 200 REST calls/min; options limited to indicative pricing feed; historical stock data "since 2016" / "7+ years" (so ~10 yrs of daily bars by mid-2026). Critical nuance: free tier CAN query full-SIP historical trades/quotes/bars, just not the most recent 15 minutes (https://docs.alpaca.markets/us/docs/market-data-faq, https://forum.alpaca.markets/t/iex-or-sip-with-a-free-account/17141).
- Algo Trader Plus, $99/mo: full SIP (CTA+UTP, 100% market volume) real-time, 10,000 calls/min (marketing page says "unlimited"), unlimited websocket symbols, real-time OPRA options feed, no 15-min historical restriction (https://docs.alpaca.markets/us/docs/about-market-data-api).
- Corporate actions: historical bars endpoint takes `adjustment=raw|split|dividend|all`, default raw (https://docs.alpaca.markets/us/reference/stockbars). Community forum has recurring reports of the adjustment flag misbehaving (https://forum.alpaca.markets/t/data-is-not-adjusted-for-splits-despite-adjustment-split-flag/7753) — validate against a second source before trusting for backtests.
- Crypto data: free, no subscription; historical crypto endpoints require no API keys; 5+ yrs history; websocket streaming included (https://docs.alpaca.markets/us/docs/historical-crypto-data-1, https://alpaca.markets/sdks/python/market_data.html).
- News API: FREE, sourced from Benzinga, historical back to 2015, ~130+ articles/day, REST + websocket (wss://stream.data.alpaca.markets/v1beta1/news), fields include headline/summary/full content/symbols/timestamps; rate limit follows data plan (200/min free) (https://docs.alpaca.markets/docs/historical-news-data, https://docs.alpaca.markets/us/docs/streaming-real-time-news, https://alpaca.markets/blog/introducing-news-api-for-real-time-fiancial-news/).

**POLYGON.IO → NOW "MASSIVE"** (rebranded effective Oct 30, 2025; polygon.io 301-redirects to massive.com; all APIs/accounts unchanged — https://massive.com/blog/polygon-is-now-massive, confirmed via 301 on https://polygon.io/pricing)
- Stocks tiers per https://massive.com/pricing (fetched July 2026): Basic free (5 calls/min, EOD only, 2 yrs history); Starter $29/mo (unlimited calls, 15-min delayed, 5 yrs, websocket); Developer $79/mo (unlimited calls, 15-min delayed, 10 yrs); Advanced $199/mo (real-time full SIP, 20+ yrs, unlimited calls). Note: some third-party reviews list Developer at $99 with real-time — the vendor page (15-min delayed, $79) is authoritative; treat third-party tier descriptions as stale.
- Flat Files (S3 bulk download of daily trades/quotes/aggregates files) included in ALL paid plans at the subscription's data level (https://massive.com/blog/flat-files, https://massive.com/docs/flat-files/stocks/day-aggregates).
- Unverified/prior knowledge: aggregates endpoint's `adjusted=true` is split-adjusted only (not dividend-adjusted); reference tickers API includes delisted symbols but there is no historical index-constituent product. Could not re-verify these two points from primary pages this session.

**DATABENTO** (https://databento.com/pricing)
- Model: usage-based pay-as-you-go for historical data ($/GB of uncompressed binary), no subscription required; $125 free signup credit (6-month expiry).
- Subscriptions: "Standard" $179/mo figure on the generic pricing page corresponds to CME futures; the US Equities Standard plan is $199/mo — unlimited live data, unlimited full-history OHLCV-1s/-1m/definitions/imbalances/statistics, 12 mo of L0/L1 history, 1 mo of L2 (https://databento.com/blog/introducing-us-equities-pricing via search snippets of https://databento.com/equities and https://databento.com/blog/upcoming-changes-to-pricing-plans-in-january-2025 — exact plan contents moderately verified, re-check before purchase). Flat-rate unlimited historical for US equities quoted at $825/mo (https://databento.com/blog/dbeq-basic).
- Key datasets: EQUS.SUMMARY = consolidated EOD OHLCV across all NMS exchanges/ATSs + 100% intraday volume on delayed basis (https://databento.com/docs/venues-and-datasets/equs-summary); EQUS.MINI = real-time feed with zero exchange license fees and free redistribution, included with any active plan (https://databento.com/equities). Old DBEQ.BASIC bundle deprecated Jan 13, 2025 (https://databento.com/blog/dbeq-basic).
- Unverified/prior knowledge: Databento delivers raw/unadjusted venue data; corporate-action adjustment is on the user (they sell a separate corporate-actions/security-master reference service). Overkill-but-clean choice for tick/L1 needs; not the cheapest for plain daily bars.

**TIINGO** (https://www.tiingo.com/pricing)
- Free: 50 req/hr, 1,000 req/day, 500 unique symbols/mo, 1 GB/mo bandwidth.
- Power: $30/mo — 10,000 req/hr, 100,000 req/day, ~108,950 unique symbols/mo, 40 GB/mo.
- EOD data: 60+ years (back to 1962), 80,000+ tickers (US equities/ETFs/mutual funds), BOTH raw and adjusted prices with split+dividend fields, multi-source cross-validated ("at least 3 data sources on average") (https://www.tiingo.com/products/end-of-day-stock-price-data).
- Also includes: IEX intraday feed, Tiingo Crypto, Tiingo News (3-month queryable history on standard plans).
- License: strictly internal/personal use, no redistribution/display to others (https://www.tiingo.com/pricing). Delisted-stock coverage not stated on product page — Tiingo carries some delisted tickers but is not marketed as survivorship-bias-free (unverified this session).

**ALPHA VANTAGE** (https://www.alphavantage.co/premium/, https://www.alphavantage.co/documentation/)
- Free: 25 requests/DAY (severely cut from the old 500/day; confirmed current). Premium: $49.99/mo (75 req/min) up to $249.99/mo (1200 req/min), no daily caps. (Annual-plan numbers on the page fetched garbled; re-check before quoting.)
- TIME_SERIES_DAILY_ADJUSTED is now a PREMIUM endpoint ("this is a premium API function"). Intraday endpoint has 20+ yrs history (back to Jan 2000) but realtime/15-min-delayed intraday is premium.
- NEWS_SENTIMENT endpoint: news + AI sentiment scores + topic tags, filterable by ticker; archive extends back to ~2022; usable on free key within the 25/day cap; commercial use of sentiment requires separate license (https://www.alphavantage.co/documentation/, corroborated by https://www.wealth-lab.com/blog/news-sentiment).
- Verdict for this project: 25/day free cap makes it useless as a primary price source; only interesting as a free news-sentiment side channel.

**FINNHUB** (https://finnhub.io/pricing — page is JS-rendered, numbers below partly third-party)
- Free: 60 calls/min (30/sec burst) (https://finnhub.io/docs/api/rate-limit); includes real-time US quotes, company news, profiles, basic fundamentals, websocket for ~50 symbols.
- CRITICAL: historical US stock candles (OHLC) were REMOVED from the free tier — returns 403 on free keys (https://github.com/finnhubio/Finnhub-API/issues/546). So free Finnhub cannot backtest.
- Paid: roughly $50/mo per market, "All-in-one" ~$500/mo, enterprise from ~$3,000/mo (third-party summaries; could not verify exact 2026 numbers from the JS-rendered vendor page — treat as approximate). Free tier is personal-use licensed.

**YFINANCE** (https://pypi.org/project/yfinance/, https://ranaroussi.github.io/yfinance/)
- Actively maintained: v1.5.1 released 2026-06-28. Still an UNOFFICIAL scraper of Yahoo endpoints; "not affiliated, endorsed, or vetted by Yahoo"; Yahoo's API is "intended for personal use only" (https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html).
- Reliability 2026: recurring 429 rate-limit/IP-ban episodes, breakage whenever Yahoo changes endpoints, now depends on curl_cffi browser impersonation to evade blocking (https://medium.com/@trading.dude/why-yfinance-keeps-getting-blocked-and-what-to-use-instead-92d84bb2cc01, https://scrapfly.io/blog/posts/guide-to-yahoo-finance-api). Verdict: acceptable for ad-hoc sanity checks/cross-validation; unacceptable as a load-bearing dependency in an automated pipeline (both on ToS and reliability grounds).

**NEWS/SENTIMENT FEEDS**
- Alpaca News API: best free option — Benzinga content, 2015+ archive, websocket push, full article content field (see Alpaca section above).
- Benzinga direct: enterprise/custom pricing, not retail-priced (https://www.benzinga.com/apis/); AWS Marketplace "Basic Financial News API" free tier = headline + teaser + link only (https://aws.amazon.com/marketplace/pp/prodview-xwgvhwowjmw3g).
- Alpha Vantage NEWS_SENTIMENT: free 25 req/day, pre-scored sentiment, ~2022+ archive.
- Tiingo News: included in plans, 3-month queryable history.
- Finnhub company-news endpoint: free at 60/min.
- RSS (free, no license issues): SEC EDGAR filing feeds updated every 10 min Mon-Fri (https://www.sec.gov/about/rss-feeds, https://www.sec.gov/structureddata/rss-feeds); Nasdaq and other outlet feeds cataloged at https://rss.feedspot.com/stock_market_news_rss_feeds/. PR Newswire/GlobeNewswire also publish public feeds (not individually verified this session).

**SURVIVORSHIP-BIAS-FREE UNIVERSES**
- Norgate Data US Stocks "Platinum": $630/yr (6- or 12-month terms only, no monthly), includes delisted securities AND historical index constituents (S&P 500 etc. point-in-time membership), data back to 1990, official Python package `norgatedata` on PyPI (https://norgatedata.com/, https://norgatedata.com/prices.php, https://pypi.org/project/norgatedata/, https://alvarezquanttrading.com/blog/norgate-data-review/, https://concretumgroup.com/how-to-construct-a-survivorship-bias-free-database-in-norgate-using-python/). Voted best EOD provider, TASC magazine 2025 & 2026 (https://norgatedata.com/). Windows-centric updater app is the main friction point (prior knowledge, unverified).
- Sharadar Core US Equities (via Nasdaq Data Link): alternative survivorship-bias-free bundle incl. delisted tickers + fundamentals (https://resources.quandl.com/a/res-hub/Sharadar_Datasheet_final.pdf); ~$399/yr region historically — 2026 price unverified.

=== 2. WHAT DAILY-TO-INTRADAY STRATEGY DEV ACTUALLY NEEDS ===
- Historical depth: 10+ yrs daily bars minimum; 15-20 yrs strongly preferred to span 2008/2020/2022 regimes. Alpaca free tops out ~2016 (borderline); Tiingo (1962+), Massive Advanced (20+ yrs), Norgate (1990+) clear the bar.
- Corporate actions: need BOTH raw and split+dividend-adjusted series (raw for realistic fill prices/position sizing, total-return-adjusted for signal generation). Tiingo and Norgate provide both natively; Alpaca via `adjustment=` param (with known bug reports); Massive/Polygon aggregates are split-adjusted only (dividends must be applied yourself from their corporate-actions endpoints — unverified detail).
- Survivorship bias: only matters materially for universe-selection/rotation strategies (e.g., "top-N momentum in S&P 500"). For a fixed watchlist of liquid ETFs/mega-caps, bias is minor and free sources suffice. If ranking across a broad universe, Norgate ($630/yr) is the only retail-priced product with point-in-time index constituents.
- Real-time latency: at daily-to-hourly decision cadence, milliseconds are irrelevant; seconds are fine. IEX-derived free feeds (Alpaca Basic) are adequate for SIGNALS. The real gap is quote quality at EXECUTION: IEX ~2-3% of volume means its top-of-book can diverge from NBBO; mitigate with marketable limit orders priced off last SIP-eligible historical quote (free tier allows SIP history older than 15 min) or upgrade to SIP ($99/mo) when going live with real money.
- LLM news agent needs: timestamped, ticker-tagged, full-text-ish articles with push delivery — Alpaca News API meets all four for $0.

=== 3. RECOMMENDED MINIMAL-COST STACK ===
(a) BACKTESTING 10+ yrs daily bars:
- Primary: Tiingo Power, $30/mo — 60 yrs of split/dividend-adjusted + raw EOD across 80k tickers, generous quotas, clean multi-source data. Best depth-per-dollar; personal-use license fits this project.
- $0 alternative: Alpaca free historical bars (2016+, `adjustment=all`) cross-validated with yfinance spot checks — acceptable to start, marginal on depth.
- Upgrade trigger: if strategy selects from a broad/index universe → add Norgate Platinum $630/yr (~$52.50/mo) for delisted stocks + point-in-time constituents; it replaces Tiingo for backtesting entirely.
- Skip: Alpha Vantage (25 req/day), Finnhub (no free candles), Databento (pay-per-GB shines for tick/L1, overkill for daily bars).
(b) LIVE PAPER/REAL TRADING, daily-hourly cadence:
- Start: Alpaca Basic free — real-time IEX websocket (30 symbols) + minute bars + paper trading on the same keys/API as eventual live routing. Zero cost, one integration.
- Go-live with real money: keep free tier if orders are marketable-limit on liquid ETFs; add Alpaca Algo Trader Plus $99/mo for SIP NBBO + unlimited symbols if slippage measurement (paper vs IEX quote drift) shows it matters. Alternative real-time: Databento EQUS.MINI (zero license fees) or Massive Advanced $199/mo — both are more plumbing for no benefit at this cadence.
(c) LLM NEWS-ANALYSIS AGENT:
- Alpaca News API (free): websocket subscribe `news:["*"]`, plus 2015+ REST archive for backtesting news signals — primary feed.
- Supplement: SEC EDGAR RSS (10-min updates, authoritative filings, free), Alpha Vantage NEWS_SENTIMENT (free, 25/day, use as batch pre-scored baseline to calibrate the LLM's own scores).
- Total news cost: $0.

BOTTOM LINE COSTS: research phase $0-30/mo (Alpaca free + optional Tiingo Power); live phase $30-129/mo (+ Algo Trader Plus only if slippage data justifies); + $630/yr Norgate only for universe-rotation strategies.

CAVEATS/STALENESS: Massive/Polygon pricing captured mid-transition post-rebrand — recheck https://massive.com/pricing at purchase time. Finnhub paid tiers and Alpha Vantage annual pricing not verifiable from vendor pages (JS-rendered/garbled). Databento US Equities Standard plan contents ($199/mo, schema history windows) sourced from vendor blog/search snippets, not a clean pricing-page fetch. Polygon dividend-adjustment behavior, Tiingo delisted coverage, Norgate Windows-updater friction, and Sharadar 2026 pricing are prior-knowledge/unverified flags.


---

# Backtesting Frameworks

All research complete. Final structured findings follow.

# PYTHON BACKTESTING/TRADING FRAMEWORK SURVEY — mid-July 2026

## 1. PER-FRAMEWORK FINDINGS

### vectorbt (open-source) — REVIVED, ACTIVE
- Repo: https://github.com/polakowo/vectorbt — 8.4k stars, 121 open issues, 15 open PRs, 1,077 commits.
- **Status: active and recently revitalized.** v1.0.0 (~April 2026) added an optional Rust engine with auto-dispatch between Numba and Rust backends; v1.1.0 (July 5, 2026) added Python 3.14 / pandas 3 / NumPy 2.4+ / Numba 0.66+ support, wheels for Py 3.11–3.14, new community contributors (https://github.com/polakowo/vectorbt/releases). CAVEAT: one fetch rendered the v1.1.0 date as "July 5, 2024," but the repo page shows 2026 and Python 3.14/pandas 3 didn't exist in 2024, so 2026 is correct. Flag for orchestrator: the 1.x line with Rust engine is a significant 2026 development; treat exact release dates as high-confidence-but-not-notarized.
- License: Apache 2.0 + Commons Clause (no commercial resale; fine for personal use).
- Paradigm: vectorized (NumPy/pandas arrays), not event-driven. Massive parameter sweeps at C speed.
- Walk-forward/OOS: OSS has sklearn-style splitters incl. `rolling_split()` and expanding walk-forward splitters (https://vectorbt.dev/api/generic/splitters/). Purged/combinatorial CV (López de Prado style) is a **PRO** feature (https://vectorbt.pro/features/optimization/).
- Costs/slippage: per-order `fees`, `fixed_fees`, `slippage` parameters in portfolio simulation — simple percentage models, not microstructure-realistic.
- Backtest↔live parity: **none.** No live/paper execution. Signal arrays ≠ deployable strategies.
- Data format: pandas DataFrames/Series (OHLCV columns); very low friction.
- LLM-in-the-loop: awkward natively (vectorized paradigm wants precomputed signal arrays); works if LLM emits signals offline that are then fed in as arrays.
- Learning curve: moderate — pandas-native entry, but broadcasting semantics get deep.

### vectorbt PRO — ACTIVE, PAID
- https://vectorbt.pro/ — private-repo membership model: ~$25/mo, $20/mo annual, lifetime conversion min $150 (https://ko-fi.com/vectorbtpro/tiers, corroborated by https://www.threads.com/@quantscience_/post/DR7FjpmDuL5 "$20/month").
- Beyond OSS: faster Rust backend, walk-forward optimization, purged + combinatorial CV, robustness checks, parameter-surface inspection, AND — notable for this project — "Built-in MCP server and CLI commands, function calling, LLM-friendly documentation, and agent support" (https://vectorbt.pro/). This is the only surveyed tool with first-class LLM/agent tooling.
- Still research-only: no live trading ("External integration required" per https://python.financial/).

### backtrader — EFFECTIVELY DEAD. DO NOT ADOPT.
- Repo: https://github.com/mementum/backtrader — 22.5k stars but **last commit April 19, 2023** (v1.9.78.123) per https://github.com/mementum/backtrader/commits/master; substantive development stopped ~2018.
- Community consensus 2026: "legacy framework… compatibility issues increasingly require manual patches… not recommended for new serious systems" (https://python.financial/); community fork `backtrader2` is bugfix-only, no new features (https://hasanjaved.me/blog/best-python-backtesting-libraries-2026/, https://github.com/kernc/backtesting.py/blob/master/doc/alternatives.md).
- Historically good: event-driven, broker/slippage schemes, IB live support — but the IB integration predates modern IB API and breaks on modern Python/matplotlib. Maintenance risk disqualifying.

### Nautilus Trader — MOST ACTIVE; BEST NATIVE BACKTEST↔LIVE PARITY
- Repo: https://github.com/nautechsystems/nautilus_trader — 24.8k stars, 83 open issues, v1.230.0 (June 29, 2026), bi-weekly releases, Rust 71% / Python 22% / Cython 5%.
- Parity (the critical criterion): explicit design goal — "deploys backtested strategies to live markets with no code changes. The same actors, strategies, and execution algorithms run against both the backtest engine and a live trading node" (https://nautilustrader.io/docs/latest/concepts/live/); README: "research-to-live parity."
- Execution realism: best-in-class of everything surveyed — probabilistic FillModel (`prob_fill_on_limit`, `prob_slippage`), order-book synthetic-liquidity fill models (ThreeTier/TwoTier/BestPrice/SizeAware/VolumeSensitive/MarketHours), queue-position tracking, liquidity consumption, bar-to-tick OHLC decomposition (https://nautilustrader.io/docs/latest/concepts/backtesting/).
- Walk-forward: **no first-class WFA.** Docs describe parameter sweeps via `BacktestEngine.reset()` + `clear_strategies()` and Partialable `BacktestRunConfig`; walk-forward orchestration is DIY scripting (https://nautilustrader.io/docs/latest/concepts/backtesting/).
- **Broker gap (decisive for this user):** integrations are crypto-heavy + Interactive Brokers (stable) + Databento; **no Alpaca, no other US retail equities broker** (https://nautilustrader.io/docs/latest/integrations/). An Alpaca RFC (#3374) was opened Jan 1, 2026 with **no visible maintainer response as of mid-July 2026** (https://github.com/nautechsystems/nautilus_trader/issues/3374). Paper trading = IB paper account through the IB adapter, or the sandbox adapter with real-time data + virtual execution (https://nautilustrader.io/docs/latest/concepts/adapters/).
- Data format: high-level API requires ingesting into Nautilus-specific Parquet `ParquetDataCatalog`; low-level API accepts lists of typed Data objects sorted by `ts_init` (https://nautilustrader.io/docs/latest/concepts/backtesting/). Real friction vs. plain DataFrames.
- LLM-in-the-loop: strategies are Python classes with event handlers; an LLM call inside `on_bar` is possible and tolerable at daily frequency, but the engine is engineered for low-latency determinism — blocking network calls in handlers is against the grain; better pattern is LLM as an external actor publishing signals onto the message bus.
- Learning curve: steepest of the OSS options — "heavier setup overhead" (https://python.financial/); domain model (instruments, venues, catalogs, configs) is large. Has a documented RiskEngine layer with pre-trade checks (per docs site; not deep-verified this session — flag as high-confidence).

### zipline-reloaded — MAINTAINED (SLOW CADENCE); BACKTEST-ONLY IN PRACTICE
- Repo: https://github.com/stefan-jansen/zipline-reloaded — 1.8k stars, 25 open issues, latest release 3.1.1 (July 23, 2025 — ~1 yr old); maintained for modern Python (>=3.9, NumPy 2, pandas >=2.2).
- Slippage/commission: mature, pluggable — subclass `SlippageModel.process_order()`; built-ins incl. FixedSlippage, VolumeShareSlippage; per-dollar/per-share commissions (https://zipline.ml4trading.io/api-reference.html).
- Parity: live-trading heritage is Quantopian-era; **no maintained live/paper path today** — third-party bridge forks are stale. Fails the critical criterion.
- Data: bundle-ingestion system widely reported as the main friction point ("data bundle ingestion friction," https://python.financial/). Pipeline API remains best-in-class for equity factor research.
- Walk-forward: none built-in.

### QuantConnect LEAN — VERY ACTIVE; FULL PARITY; BUT PAY-WALLED LOCAL CLI + PLATFORM COUPLING
- Repo: https://github.com/QuantConnect/Lean — 20.6k stars, 13,269 commits, 232 open issues; C# 94% engine with Python algorithm API.
- Parity: genuine backtest/paper/live with same algorithm code; `lean backtest` / `lean live` via Docker (https://www.quantconnect.com/docs/v2/lean-cli/key-concepts/getting-started).
- Brokerages: broadest — IB, **Alpaca**, Charles Schwab, TradeStation, Tastytrade, Public, Webull, plus crypto; own paper-trading venue (https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages).
- **Cost gate:** "To use the CLI, you must be a member in an organization on a paid tier" — Researcher tier $60/mo minimum for local CLI + live; live nodes from $24/mo (https://www.quantconnect.com/docs/v2/lean-cli/key-concepts/getting-started, https://www.quantconnect.com/pricing/). Data must be in LEAN's proprietary zip/csv layout or bought from their datasets; conversion tooling exists but is friction.
- Realism: full "reality modeling" (fee/slippage/fill/margin models) — professional grade. Walk-forward: parameter optimization exists; WFA per se is DIY.
- LLM-in-the-loop: fine locally (Python algo can call any API); learning curve steep (C# engine internals leak into debugging; Docker workflow).

### bt — ALIVE BUT WRONG SHAPE FOR THIS PROJECT
- Repo: https://github.com/pmorissette/bt — 2.9k stars, 79 issues, v1.2.0 (April 25, 2026). Maintained.
- Tree-structured, weight/rebalance "Algo stacks" for allocation strategies; explicitly research-only, no live trading, no intraday microstructure (https://python.financial/). Good for ETF rebalancing overlays; not for signal-driven trading with execution parity.

### PyBroker — ACTIVE; BEST BUILT-IN WALK-FORWARD; NO LIVE EXECUTION
- Repo: https://github.com/edtechre/pybroker — 3.5k stars, only 8 open issues, v1.2.12 (March 5, 2026), 48 releases. Apache 2.0 + Commons Clause.
- Walk-forward analysis is the default backtest mode; bootstrapped metrics (confidence intervals on Sharpe etc.) built-in — unique among surveyed OSS (https://github.com/edtechre/pybroker, https://www.pybroker.com/en/latest/index.html).
- Costs/slippage: `fee_mode` (ORDER_PERCENT / PER_ORDER / PER_SHARE / custom callable) in StrategyConfig (https://www.pybroker.com/en/latest/reference/pybroker.config.html); `RandomSlippageModel(min_pct, max_pct)` via `Strategy.set_slippage_model()` (https://www.pybroker.com/en/latest/reference/pybroker.html).
- Data: pulls Alpaca/Yahoo/AKShare or custom DataFrames; Numba-accelerated NumPy engine.
- Parity: **backtest-only** — Alpaca is a *data* source; no live order routing module. Execution callbacks (`exec_fn(ctx)`) are per-bar Python functions → LLM hook is easy in research, but you'd still have to build the live side.
- Learning curve: low-moderate; ML-first design.

### backtesting.py (context, not requested but relevant)
- https://github.com/kernc/backtesting.py — 8.7k stars, actively maintained, AGPL-3.0, single-asset only, no portfolio mechanics, no live trading. Good for quick single-instrument idea checks only.

### Custom event-driven engine (build option)
- Supporting evidence for feasibility: alpaca-py (official SDK) v0.43.5 released July 2, 2026, Python 3.8–3.14, actively maintained (https://github.com/alpacahq/alpaca-py/releases, https://pypi.org/project/alpaca-py/); Alpaca paper sandbox at paper-api.alpaca.markets uses **the same API surface as live**, 24/5 (https://docs.alpaca.markets/us/docs/paper-trading) — i.e., parity can be achieved at the *broker-interface* level rather than the framework level: one `Broker` ABC with `SimBroker` (backtest), `AlpacaPaperBroker`, `AlpacaLiveBroker` implementations.
- LLM-in-the-loop ecosystem exists but is immature/unvetted: backtest-kit claims 10+ LLM providers with enforced JSON-schema trade signals (https://backtest-kit.github.io/ — treat as unaudited); academic state of the art: Strat-LLM (https://arxiv.org/html/2605.06024), Trading-R1 (https://arxiv.org/pdf/2509.11420). Common thread: LLM proposes structured signals; deterministic code validates/executes — matches the user's failsafe requirement.
- Known risks of DIY: look-ahead bias, split/dividend adjustment, order-fill assumptions — the classic bug classes the mature frameworks have already paid for.

## 2. CRITERIA MATRIX (condensed)

| Framework | Active mid-2026 | Same code BT↔live | WFA built-in | Cost/slippage realism | LLM hook ease | Data friction | Learning curve |
|---|---|---|---|---|---|---|---|
| vectorbt OSS | Yes (v1.1.0 Jul 2026) | No | Splitters yes | Basic pct fees/slip | Poor (vectorized) | Low (DataFrames) | Moderate |
| vectorbt PRO | Yes (paid) | No | Best-in-class (purged/CPCV) | Basic-plus | Good (MCP/agent support) | Low | Moderate |
| backtrader | **No (Apr 2023)** | Nominal, bitrotted | No | Good (historic) | OK | Low | Moderate |
| Nautilus | Yes (biweekly) | **Yes (IB only for US eq.)** | No (DIY) | **Best** | Moderate (against grain) | High (Parquet catalog) | **Steep** |
| zipline-reloaded | Slow (Jul 2025) | No (live path dead) | No | Good models | OK | High (bundles) | Moderate |
| LEAN | Yes (very) | **Yes (many brokers incl. Alpaca)** | Partial (optimizer) | Excellent | OK (local) | High (LEAN format) + **$60/mo CLI gate** | Steep |
| bt | Yes (Apr 2026) | No | No | Minimal | Poor | Low | Low |
| PyBroker | Yes (Mar 2026) | No | **Yes (default)** | Fees + random slippage | Good (exec_fn) | Low | Low-moderate |
| Custom engine | n/a | Yes via broker-API parity (Alpaca paper=live API) | You write it | You write it | **Best** | Your choice | Cost = build time |

## 3. RECOMMENDATION

**Primary: build a lean custom event-driven engine for execution + adopt vectorbt (OSS) and PyBroker as research-side tools.** Rationale tied to this user's constraints:
1. The critical "same strategy code in backtest and live/paper" requirement is only natively met by Nautilus (Interactive Brokers only — no Alpaca adapter, RFC unanswered since Jan 2026) or LEAN ($60/mo minimum for local CLI, proprietary data format, C# debugging surface). For a $5–10k US retail account trading at daily/low frequency, both impose disproportionate complexity or cost. Parity is instead achievable at the broker boundary: Alpaca's paper API is identical to its live API (https://docs.alpaca.markets/us/docs/paper-trading) and alpaca-py is actively maintained (v0.43.5, Jul 2026) — so one strategy class against a `Broker` interface runs unchanged in sim, paper, and live.
2. LLM-in-the-loop with hard failsafes is architecturally easiest in a custom engine: LLM emits a JSON-schema trade proposal → deterministic validator (position caps, notional caps, kill switch, rate limits, market-hours check) → broker interface. This is the pattern the 2026 literature converges on (Strat-LLM, Trading-R1) and is awkward to bolt into Nautilus's low-latency event loop or LEAN's sandbox.
3. At daily-bar frequency with ~$5–10k, microstructure-grade fill simulation (Nautilus's main edge) is not the binding risk — overfitting is. So put the sophistication where it pays: vectorbt OSS (revived, Rust engine, active) for wide parameter sweeps and rolling/expanding walk-forward splits; PyBroker for walk-forward + bootstrapped-confidence-interval validation of finalists. Both consume plain DataFrames, so one data layer (e.g., Parquet OHLCV from a single vendor) feeds everything. Optional: vectorbt PRO at $20–25/mo is cheap and adds purged/combinatorial CV plus first-party MCP/LLM-agent tooling.
4. Scope containment for the custom engine: daily/hourly bars, long-only-ish US equities, single broker → ~1–2k LOC. The classic DIY bug classes (look-ahead, corporate actions) are mitigated because the custom engine is only the *execution shell*; statistical validation happens in the mature vectorized tools, and paper-trading divergence vs. backtest becomes the ongoing integration test.

**Alternative (adopt-a-framework path): Nautilus Trader — choose it only if the user commits to Interactive Brokers as the broker.** It is the most active project surveyed (24.8k stars, biweekly releases), has genuine no-code-change backtest→live parity, and the best execution realism; costs are the steep learning curve, Nautilus-specific Parquet data catalog, DIY walk-forward, and the IB-or-nothing US-equities constraint. LEAN is the runner-up if $60+/mo and platform coupling are acceptable (broadest broker support incl. Alpaca/Schwab).

**Explicitly rejected:** backtrader (last commit April 2023, community declares it legacy; adopting it in 2026 is adopting an unmaintained dependency for a live-money system), zipline-reloaded (no live path, bundle friction, 1-year release gap), bt (rebalancing-only), backtesting.py (single-asset).

## 4. UNVERIFIED / STALENESS FLAGS
- vectorbt OSS v1.x release dates: repo page says v1.1.0 Jul 5 2026; one sub-fetch misrendered "2024." Feature set (Py 3.14, pandas 3) makes 2026 the only consistent reading. Verify on the releases page before relying on the Rust-engine feature.
- vectorbt PRO pricing ($25/mo, $20/mo annual, $150-min lifetime) sourced from Ko-fi tier pages + a social post, not an official pricing page (the /pricing/ URL 404s); could drift.
- Nautilus RiskEngine pre-trade-check specifics (max order rate, position limits) come from general docs knowledge; the live-concepts page fetched this session covered reconciliation, not risk limits — verify at https://nautilustrader.io/docs/latest/concepts/ before depending on them.
- LEAN "paid tier required for CLI" was confirmed from the official getting-started doc + forum threads; QuantConnect changes pricing policy periodically — recheck https://www.quantconnect.com/pricing/.
- backtest-kit (LLM-signal framework) is unaudited; do not adopt without code review.
- Nautilus Alpaca RFC (#3374) status checked 2026-07-18; may progress.


---

# Risk Controls & Failsafes

RESEARCH FINDINGS: RISK CONTROLS & FAILSAFES FOR RETAIL SINGLE-MACHINE AUTOMATED TRADING (as of 2026-07-18)

=== 1. SEC RULE 15c3-5 AS A DESIGN TEMPLATE ===
Rule text (17 CFR 240.15c3-5, https://www.law.cornell.edu/cfr/text/17/240.15c3-5) requires broker-dealers with market access to have controls that:
- "Prevent the entry of orders that exceed appropriate pre-set credit or capital thresholds in the aggregate for each customer and the broker or dealer" (aggregate notional/capital cap).
- "Prevent the entry of erroneous orders, by rejecting orders that exceed appropriate price or size parameters, on an order-by-order basis or over a short period of time, or that indicate duplicative orders" — i.e., three distinct checks: price collars, size limits (per-order AND per-time-window), duplicate-order detection.
- Regulatory controls: block restricted symbols, restrict access to authorized processes, immediate post-trade surveillance reporting.
- Governance: controls under "direct and exclusive control" of the firm; documented annual review of effectiveness; CEO certification. (SEC compliance guide: https://www.sec.gov/files/rules/final/2010/34-63241-secg.htm; FAQ: https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0)

FINRA 2026 Annual Regulatory Oversight Report — Market Access section (https://www.finra.org/rules-guidance/guidance/reports/2026-finra-annual-regulatory-oversight-report/market-access-rule) — cited deficiencies and effective practices directly portable to retail scale:
- Deficiencies: limits set at unreasonable levels with no documented rationale; no process for intraday threshold adjustment (no approval trail, no automatic reversion); excluding certain order types from erroneous-order controls; multiple disconnected risk systems with no aggregate view; no documented annual effectiveness review.
- Effective practices: hard blocks (not warnings) pre-trade; documented ad-hoc limit adjustments with automatic reversion to defaults; price/size parameters calibrated per security characteristics and applied in ALL sessions (incl. extended hours); complementary checks — market-impact check, liquidity check, average-daily-volume control (reject orders > X% of ADV); test hard blocks under scenarios like large orders in thin names.

MiFID II RTS 6 (EU analogue, https://ec.europa.eu/finance/securities/docs/isd/mifid/rts/160719-rts-6_en.pdf) adds: mandatory "kill functionality" to immediately cancel all algorithmic orders; annual self-assessment incl. stress-testing algos against increased order flow/market stress (https://www.deloitte.com/uk/en/services/audit-assurance/blogs/mifid-ii-rts-6-requirements-annual-self-assessment.html). ESMA supervisory briefing on algo trading (Feb 2026): https://www.esma.europa.eu/sites/default/files/2026-02/ESMA74-1505669079-10311_Supervisory_Briefing_on_Algorithmic_Trading_in_the_EU.pdf

=== 2. KNIGHT CAPITAL 2012 — ROOT CAUSES & LESSONS ===
Sources: SEC settled order 34-70694 summarized at https://jrvarma.wordpress.com/2013/10/20/sec-order-explains-knight-capital-systems-failure/ and https://www.wilmerhale.com/en/insights/client-alerts/knight-capital-settles-rule-15c3-5-violations-with-sec-agrees-to-pay-12-million; engineering deep-dives: https://specbranch.com/posts/knight-capital/, https://www.kitchensoap.com/2013/10/29/counterfactuals-knight-capital/, https://dougseven.com/2014/04/17/knightmare-a-devops-cautionary-tale/, https://www.henricodolfing.ch/en/case-study-4-the-440-million-software-error-at-knight-capital/. (Note: sec.gov press release returned HTTP 403 to my fetcher; the widely-reported "97 email alerts at 8:01am" figure comes from the SEC order but I could not re-verify it against sec.gov directly.)
Chain of causes (each a lesson):
1. Dead code left live: "Power Peg" function deprecated in 2003 but never removed from SMARS servers; its fill-tracking/position-reporting had silently broken in a 2005 refactor, so it sent child orders with NO fill counter — it never knew it was done.
2. Flag reuse: flag field out of bits, so engineers reused the Power Peg flag bit for the new Retail Liquidity Program code.
3. Partial deployment: new code deployed to 7 of 8 SMARS servers; deployment was manual with no automated verification and no second-engineer signoff; per specbranch, the deploy tooling failed silently and reported success.
4. Rollback made it worse: at open, engineers "rolled back" the 7 good servers, which activated the bad path on all 8.
5. No order-entry-layer risk check: SMARS "simply accepted orders and executed them, regardless of whether the strategy (or the firm) had the requisite capital" — capital checks lived only in strategies whose position data was broken.
6. Alerts ignored: pre-open automated alerts referencing the misconfiguration were not acted on; no runbook/kill-switch procedure existed, so shutdown took ~45 min.
Result: ~4 million executions in 154 stocks, ~397 million shares, >$460M loss in ~45 minutes; SEC's first 15c3-5 enforcement, $12M penalty, cited failures under 15c3-5(b), (c)(1)(i), (c)(1)(ii), (e)(1), plus a CEO certification that attested to "processes" rather than actual compliance.
Portable lessons: delete dead code paths, never repurpose config flags; automated + verified deploys (checksum/version handshake at startup); an independent risk layer at the LAST hop before the broker that maintains its own position/notional view; a rehearsed kill procedure ("stop trading first, debug second"); treat unexplained alerts as trading halts.

=== 3. KILL-SWITCH & DEAD-MAN-SWITCH PATTERNS ===
- Dead-man's switch (cancel-all-after): client repeatedly arms a countdown; if no re-arm arrives, venue cancels all orders. Kraken `cancelAllOrdersAfter`: recommended call every 15–30s with 60s timeout; timeout=0 disarms (https://docs.kraken.com/api/docs/futures-api/trading/cancel-all-orders-after/). Same pattern at Binance Options auto-cancel (https://www.binance.com/en/support/faq/binance-options-auto-cancel-all-open-orders-kill-switch-17f031ed1a5642ab9c74a7b64b6864d2). Neither Alpaca nor IBKR offers a server-side dead-man endpoint for equities (unverified absence — I found no such endpoint in Alpaca docs as of mid-2026), so the retail equivalent is: (a) use DAY time-in-force so orders die at session end, (b) run a local watchdog that calls cancel-all when the engine misses heartbeats.
- Exchange-side kill switches exist as precedent (CME Globex Kill Switch cancels all working orders and blocks new ones per session: https://www.cmegroup.com/tools-information/webhelp/globex-credit-controls/Content/Kill-Switch.html).
- Kill-switch design cautions: industry debate notes an automated kill switch firing at the wrong time can itself destabilize; operators are reluctant to pull it without understanding the failure (https://www.nyif.com/articles/trading-system-kill-switch-panacea-or-pandora-box). Design consequence for retail: kill switch should be cheap to trigger and cheap to recover from (halt new orders + cancel opens; position flattening as a separate, deliberate second step), so there's no hesitation to use it.
- Retail panic buttons that DO exist: Alpaca `DELETE /v2/orders` (cancel all) and `DELETE /v2/positions?cancel_orders=true` (liquidate everything, HTTP 207 per-item statuses) (https://docs.alpaca.markets/reference/deleteallopenpositions-1, https://alpaca.markets/blog/position-liquidation-cancel-orders/). NautilusTrader trading-state machine is a good software template: ACTIVE / REDUCING (only position-reducing orders pass) / HALTED (everything except cancels denied) (https://nautilustrader.io/docs/latest/api_reference/risk/).

=== 4. ORDER IDEMPOTENCY & RECONCILIATION ===
- Idempotency-key pattern: client generates unique key per order intent; server dedupes on it; same key + different payload should be rejected as a client bug; keys expire after e.g. 24h (https://httptoolkit.com/blog/idempotency-keys/, https://www.tokenmetrics.com/blog/idempotency-keys-order-placement). Alpaca supports `client_order_id` (unique, client-supplied, queryable) — on a timeout/ambiguous submit, query by client_order_id BEFORE retrying; do not blind-retry (https://docs.alpaca.markets/us/docs/orders-at-alpaca; community discussion: https://forum.alpaca.markets/t/idempotency-on-order-create/15801). Caveat: Alpaca's enforcement is dedupe-by-lookup, not a strict server-side atomic idempotency guarantee — treat "submit" as at-least-once and reconcile.
- Reconciliation template (NautilusTrader execution reconciliation, https://nautilustrader.io/docs/latest/concepts/execution/): on startup, delay ~10s for connections to stabilize, then reconcile cached orders/positions against venue; continuously poll open orders and positions on intervals; treat discrepancies only after a threshold (~5s) to avoid racing in-flight events; orders found at venue but not in local cache become "EXTERNAL" orders that still get tracked; dedupe fills on trade_id (+side/price/qty); reconciliation-generated IDs are deterministic hashes so restarts don't double-count; overfills rejected by default.
- Retail translation: single source of truth is the BROKER's state, not the local DB. On every startup and every N seconds: fetch account, positions, open orders; diff against local intent; if diff exceeds tolerance → HALT + alert, never "auto-correct" by trading.

=== 5. CIRCUIT BREAKERS (CONCRETE, FROM OSS FRAMEWORKS) ===
Freqtrade "protections" plugin is the best retail-scale reference implementation (https://www.freqtrade.io/en/stable/plugins/):
- StoplossGuard: if >= trade_limit stoplosses within lookback_period → stop trading for stop_duration (consecutive-loss halt).
- MaxDrawdown: if drawdown over lookback exceeds max_allowed_drawdown (e.g., 20% over 48 candles) → halt for stop_duration; supports equity-curve peak-to-trough mode.
- CooldownPeriod: no re-entry in same instrument for N minutes after exit (kills self-reinforcing churn loops).
- LowProfitPairs: lock an instrument whose recent profit ratio is below threshold.
NautilusTrader RiskEngine defaults (https://nautilustrader.io/docs/latest/api_reference/risk/): max order submit rate AND max modify rate default 100/second (retail should set ~1–10/min); max_notional_per_order per instrument; failed check → OrderDenied, order never reaches venue.
Standard retail parameter set (synthesized from above + FINRA effective practices): per-order max shares, per-order max notional, per-symbol max position, aggregate gross notional cap, daily loss limit (halt), max drawdown from equity peak (halt), N-consecutive-losses halt, orders-per-minute throttle, max new orders per symbol per day, restricted-symbol allowlist (trade only a vetted universe).

=== 6. HOW RUNAWAY FEEDBACK LOOPS HAPPEN ===
Mechanisms (from incident literature):
- Strategy reacting to its own fills/quotes: Knight's Power Peg lost its fill counter so it re-sent child orders forever (https://specbranch.com/posts/knight-capital/); 2010 Flash Crash: volume-targeting sell algo (9% of prior-minute volume, no price/time limit) fed on volume that HFTs generated trading "hot potato" among themselves — the algo's own market impact raised the volume signal that told it to sell more (https://tradersunion.com/trading-glossary/algorithmic-trading/flash-crashes/; academic treatment: https://pmc.ncbi.nlm.nih.gov/articles/PMC8978471/).
- Retry storms: timeout ≠ failure; blind resubmission on HTTP timeout/5xx duplicates orders; without idempotency keys + exponential backoff + retry budget, an outage becomes N duplicate positions (https://httptoolkit.com/blog/idempotency-keys/).
- Stale-data loops: engine acts on a frozen feed (last price stops updating but engine keeps evaluating signals against it), or on its own out-of-date position cache (Knight strategies "remained unaware, continuing to send additional orders"). Defense: staleness stamps on every quote (reject decisions on data older than X seconds), and position derived from broker reconciliation, not local increments alone.
- Bad-deploy/config activation (Knight): a flag or config flips behavior on a subset of infrastructure.
Defenses tie back to sections 3–5: order-rate throttle, per-symbol day-order cap, cooldowns, decision-loop invariant checks ("did I already order this symbol this bar?"), staleness rejection, halt-on-reconciliation-mismatch.

=== 7. WHAT RETAIL BROKER APIS THEMSELVES PROVIDE ===
Alpaca (https://docs.alpaca.markets/us/docs/user-protection, https://docs.alpaca.markets/us/docs/orders-at-alpaca):
- Buying-power check at order acceptance (far-side NBBO valuation; open unfilled orders reserve buying power; excess → rejected).
- Pattern-day-trader protection: order rejected with 403 if it would trigger PDT flag (critical at $5–10k equity, under the $25k PDT threshold — day-trading capacity is structurally limited to 3 round trips per 5 business days in a margin account).
- Wash-trade protection: self-crossing orders rejected 403.
- client_order_id for dedupe; bracket/OCO/OTO order classes = server-side stop-loss/take-profit that survive local machine death; GTC auto-cancels after 90 days.
- Time-in-force as safety: DAY (self-expiring), IOC/FOK (no resting exposure), extended hours require limit+day only (no market orders off-hours).
- Panic endpoints: cancel-all-orders, close-all-positions (see section 3).
IBKR (https://www.interactivebrokers.com/campus/glossary-terms/precautionary-settings/, https://ibkrguides.com/tws/usersguidebook/configuretws/apiprecautions.htm):
- TWS Precautionary Settings: per-instrument-class Size Limit, Total Value Limit, and Percentage price constraint (rejects/flags limit prices too far off market — fat-finger collar).
- For API orders these act as hard gates UNLESS "Bypass Order Precautions for API Orders" is checked (File > Global Configuration > API > Precautions) — a violating API order errors out instead of popping a dialog. Design implication: leave bypass OFF and set tight limits so the broker layer independently rejects insane orders (https://www.elitetrader.com/et/threads/what-does-bypass-order-precautions-for-api-orders-mean-in-ib-tws.164714/). Note tension: many automation toolkits instruct users to enable bypass for convenience (e.g., https://support.tradeautomationtoolbox.com/hc/en-us/articles/43583605993875-Installation-Instructions-IBKR) — that removes a failsafe.
- TWS also has daily auto-restart/logoff (session naturally terminates daily) and Order Presets for default caps (https://www.interactivebrokers.com/en/trading/tws-order-presets.php).
General: broker-side controls are the ONLY controls that survive your machine going insane; local controls are the only ones you fully command (15c3-5's "direct and exclusive control" concept). Use both layers.

=== 8. WATCHDOG-PROCESS PATTERNS ===
- systemd watchdog: service reads WATCHDOG_USEC, sends sd_notify("WATCHDOG=1") heartbeats; missed heartbeat → systemd kills/restarts (catches hung-but-alive processes that Restart=on-failure misses). Canonical write-up: http://0pointer.de/blog/projects/watchdog.html; Python binding: https://pypi.org/project/systemd-watchdog/; config guides: https://oneuptime.com/blog/post/2026-03-02-how-to-configure-systemd-watchdog-for-service-health-checks-on-ubuntu/view. Emit the heartbeat from INSIDE the main decision loop (after a successful data-fetch + reconciliation cycle), not from a side thread — a side-thread heartbeat keeps beating while the real loop is wedged.
- "Insane engine" (running but wrong) needs a semantic watchdog, separate process with own broker credentials/read-only API keys, checking invariants: order count last N min < cap, position notional < cap, daily P&L > -limit, local-vs-broker position diff = 0, market-data timestamp fresh. On violation: call broker cancel-all + (optionally) close-all, stop the engine service, page the operator. This mirrors the exchange-side dead-man pattern locally.
- Restart discipline: auto-restart the ENGINE only into a reconcile-then-HALTED state (NautilusTrader-style trading states) — never auto-restart straight into order-sending; Knight's rollback-under-pressure lesson.

=== 9. CONCRETE CONTROL CHECKLIST (WITH RATIONALE) ===
PRE-TRADE GATE (independent module, last hop before broker SDK; 15c3-5 template):
1. Price collar: reject limit prices > X% (e.g., 2–5%) from last trade/NBBO mid; reject ALL market orders or convert to marketable limit. [15c3-5 "price parameters"; IBKR percentage constraint precedent]
2. Max shares per order + max notional per order (e.g., $500–$1,000 on a $5–10k account). [15c3-5 size parameters; Nautilus max_notional_per_order]
3. Aggregate caps: max gross notional of open positions + open orders; max position per symbol; reject if breach. [15c3-5 capital thresholds; Knight's missing check]
4. Duplicate-order check: reject same symbol/side/qty within a rolling window (e.g., 60s) unless explicitly flagged; enforce one open entry order per symbol per strategy. [15c3-5 duplicative-order clause]
5. Symbol allowlist + ADV check: trade only vetted liquid symbols; order qty < small % of ADV. [FINRA 2026 effective practices]
6. Session/time gate: no orders outside intended session; extended-hours only via limit+day. [Alpaca rejects otherwise]
7. Order-rate throttle: hard cap orders/min and orders/day, global and per symbol. [Nautilus rate limits; anti-runaway]
8. Stale-data gate: refuse decisions on market data older than X seconds; refuse if last reconciliation older than Y. [stale-loop defense]
CIRCUIT BREAKERS (post-trade monitors → trading-state machine ACTIVE/REDUCING/HALTED):
9. Daily loss limit → HALT (e.g., 1–2% of equity); intraday drawdown-from-peak halt; N consecutive losses → halt (StoplossGuard analogue); per-symbol cooldown after exit (CooldownPeriod analogue). [freqtrade protections]
10. Halt on reconciliation mismatch: broker positions/orders != local expectation beyond tolerance → HALT + alert, no auto-trading "fix". [Knight lesson; Nautilus reconciliation]
IDEMPOTENCY / STATE:
11. Deterministic client_order_id per order intent (strategy+symbol+bar-timestamp hash); on submit timeout, query by client_order_id before any retry; exponential backoff with a retry budget of 1–2. [Alpaca client_order_id; idempotency-key pattern]
12. Broker is source of truth: reconcile positions/open orders/account on startup (after ~10s stabilization) and every 30–60s; startup always enters HALTED until reconciliation passes. [Nautilus pattern]
KILL SWITCH / DEAD-MAN:
13. One-command kill: cancel all open orders + set HALTED; separate deliberate command for flatten-all (Alpaca DELETE /v2/orders, DELETE /v2/positions?cancel_orders=true). Rehearse it; make it reachable from the mobile dashboard. [MiFID RTS 6 kill function; Knight 45-min lesson]
14. Local dead-man: independent watchdog process cancels all orders via broker API if engine heartbeat missed for T seconds (no server-side dead-man exists at Alpaca/IBKR for equities). Prefer DAY TIF and server-side bracket stop-losses so a dead machine's exposure is bounded anyway. [Kraken dead-man pattern, adapted]
15. Broker-side backstop: keep IBKR precautionary settings tight with API bypass OFF, or rely on Alpaca buying-power/PDT/wash rejections; never widen broker limits to "make the bot work". [IBKR API precautions]
WATCHDOG / OPS:
16. systemd WatchdogSec + Restart=on-failure, heartbeat emitted from the main loop; restart lands in HALTED-reconcile state, never straight to trading. [0pointer watchdog; Knight rollback lesson]
17. Semantic sanity monitor (separate process, read-only keys): invariants on order rate, P&L, exposure, data freshness; violation → kill switch + push alert. [Knight's 97 ignored alerts]
DEPLOY / CHANGE GOVERNANCE (Knight-derived):
18. No dead code paths or reused flags in order-path config; versioned config; engine logs and asserts its own code/config version at startup; any deploy → start in paper/HALTED mode with a checklist before re-enabling live; keep a documented rollback that is actually tested.
19. Annual (or per-major-change) written review of limit values with rationale; any manual intra-day limit override auto-reverts next session. [15c3-5(e) annual review; FINRA reversion practice]
20. Kill-drill: periodically simulate engine-gone-insane in paper mode and verify watchdog + cancel-all + alerting fire end-to-end. [RTS 6 stress-testing analogue]

CAVEATS / UNVERIFIED: sec.gov pages (press release 2013-222, order PDF) returned 403 to automated fetch — Knight specifics (97 alerts, $12M penalty, subsection cites) are from secondary legal/analyst sources consistent with the SEC order. Absence of a server-side dead-man switch at Alpaca/IBKR equities is an absence-of-evidence finding as of mid-2026. Freqtrade protection details partially cited from 2021-era doc versions (feature still present in /en/stable/ plugins page). NautilusTrader reconciliation parameters reflect latest-docs defaults as of mid-2026 and change between releases.


---

# LLM/Agentic Trading Evidence

# LLM/Agentic Trading Research — State of Evidence as of mid-2026

## 1. Do LLM trading strategies credibly beat benchmarks after costs? Answer: No credible evidence, and strong counter-evidence.

**The single most load-bearing paper — "Can LLM-based Financial Investing Strategies Outperform the Market in Long Run?" (arXiv 2505.07078, v5):** Replicated headline LLM-agent frameworks (FinMem, FinAgent) that originally claimed to beat baselines, then removed three biases: survivorship bias, look-ahead bias (stock/parameter selection on full-period outcomes), and data-snooping. Findings:
- Prior positive results came from tests on **fewer than 10 hand-picked stocks over less than a year** (e.g., Oct 2022–Apr 2023, TSLA/NFLX/AMZN/MSFT).
- Extended to 2004–2024 across 63–91 symbols: **FinMem Sharpe −0.253 to 0.025, FinAgent 0.094–0.241, vs buy-and-hold 0.315–0.703**. Simple ARIMA and ATR-band baselines beat both LLM frameworks. No LLM strategy produced statistically significant alpha (all p > 0.34); B&H significantly outperformed under paired t-tests.
- Regime behavior: LLMs were "too cautious when risk is rewarded and too aggressive when it is penalised" (bull: FinAgent Sharpe 0.12 vs B&H 0.61; bear: −0.38 vs −0.28).
- Source: https://arxiv.org/html/2505.07078v5

**KTD-Fin memory-controlled benchmark (arXiv 2605.28359, May 2026):** Ten frontier LLMs (OpenAI, Anthropic, Google, DeepSeek, Alibaba, etc.) on CSI300, Jan 2024–Apr 2026, with a four-level ticker/date anonymization protocol validated by a de-anonymization probe (≤1.5% joint recovery) to close the pretraining-memorization channel. Headline returns ranged −24.23% to +85.29%, but Barra-style attribution showed returns were **almost entirely passive market + style exposure; 9 of 10 models had negative stock-selection alpha (down to −77.8%); best model achieved only +0.2% alpha**. Crucially, revealing real tickers alone changed trading behavior even with no other data — direct evidence that unmasked backtests are contaminated by memorization. Source: https://arxiv.org/html/2605.28359v1

**LiveTradeBench (arXiv 2511.03628):** 50 days of live paper trading (Aug 18–Oct 24, 2025) across 21 LLM backbones. **No LLM consistently beat market benchmarks or buy-and-hold**; LMArena/static benchmark rank was a poor predictor of trading performance; results exclude transaction costs, so live results would be worse. Source: https://arxiv.org/pdf/2511.03628

**StockBench (arXiv 2510.02209):** Contamination-free window (Mar–Jun 2025), top-20 DJIA stocks, $100k paper capital, 82 trading days, 14 models × 3 seeds. Best models (Kimi-K2 +1.9%, Qwen3-235B +2.4%, GLM-4.5 +2.3%) modestly beat the equal-weight baseline's +0.4% and had lower drawdowns (~ −11% vs −15.2%); **GPT-5 and GPT-OSS variants roughly flat to negative (−0.9% to −2.8%)**. Margins are small, single-period, cost-free, and reasoning-tuned models underperformed instruction-tuned ones. Source: https://arxiv.org/html/2510.02209v1 / https://stockbench.github.io/

**Agentic Trading survey (arXiv 2605.19337):** Of 19 studies meeting minimal closed-loop evaluation criteria, only 2 had extractable time-consistent data splits, **1 specified transaction costs, 1 documented survivorship handling, 0 were fully replayable**. Conclusion: the field's published performance claims are essentially non-comparable and non-reproducible. Source: https://arxiv.org/html/2605.19337v1

**Bottom line:** Every leakage-controlled, cost-aware, long-horizon, or live evaluation to date shows LLM agents at or below buy-and-hold after adjusting for beta/style exposure. Published "beats the market" claims trace to short windows, tiny hand-picked universes, memorized data, or ignored costs.

## 2. Public experiments with real or live money

- **Alpha Arena Season 1 (nof1.ai, Oct 18–Nov 3, 2025):** Six frontier LLMs each given $10,000 real USD trading crypto perps autonomously on Hyperliquid. Final: **Qwen3 Max +~22% (won, with the fewest trades — 43 over 17 days), DeepSeek V3.1 +4–5%; GPT-5 and Gemini 2.5 Pro lost heavily (GPT-5 down ~40% at one point; reports of 60%+ drawdowns; Gemini reportedly near-liquidated)**. Observations: outcome dominated by execution/position-sizing discipline, not reasoning-benchmark rank; strategy homogeneity across models flagged as a systemic-risk concern; 17 days of crypto perps is statistically meaningless for alpha claims. Sources: https://www.iweaver.ai/blog/alpha-arena-ai-trading-season-1-results/ , https://finance.yahoo.com/news/deepseek-outperforms-ai-rivals-real-093000567.html , https://news.bitcoin.com/6-bots-with-real-money-hyperliquid-hosts-first-ever-ai-trading-showdown/ , https://nof1.ai/blog/TechPost1 (official; returned HTTP 429 during this research — details verified via secondary coverage only). Could not verify a completed Season 2 as of 2026-07-18.
- **ChatGPT Micro-Cap Experiment (LuckyOne7777/Nathan Smith, real $100, Jun 27–Dec 27, 2025):** Widely covered for its first-month +23.8% vs Russell 2000 +3.9% ( https://decrypt.co/332826/high-school-students-chatgpt-trading-bot ). **Final outcome, per the author's own evaluation report: max drawdown −50.33% (equity $67.10 on Nov 6, 2025), underperformed Russell 2000 and S&P 500, 50% win rate, profit factor 0.82, per-lot expectancy −$0.41.** Documented failure modes: extreme concentration (~3 tickers), repeated re-buying of losers (FBIO, IINN, ATYR), binary-event catalyst exposure (single position lost >2x the largest winner's gain), no transaction costs modeled, single run. This is the cleanest public case study of early-outperformance-was-noise. Sources: https://raw.githubusercontent.com/LuckyOne7777/LLM-Trading-Lab/main/Experiments/chatgpt_micro-cap/evaluation/evaluation_report.md , https://github.com/LuckyOne7777/ChatGPT-Micro-Cap-Experiment
- **TradingAgents (TauricResearch, arXiv 2412.20138):** Most popular open-source multi-agent LLM trading framework (analyst/researcher/trader/risk-manager roles, bull-bear debate). Paper claims 23–26% cumulative return over ~3 sampled stocks, but the repo itself warns results vary with backbone model, temperature, data quality, and non-determinism, and the team **explicitly recommends against real money**. Independent write-ups note ~7% vs SPY 4.5% over one 30-day run with 22% drawdowns, and that one quarter/3 stocks proves nothing. Sources: https://github.com/tauricresearch/tradingagents , https://arxiv.org/pdf/2412.20138 , https://pinggy.io/blog/best_ai_trading_agents/
- **ai-hedge-fund (virattt):** 13 investor-persona agents (Buffett, Burry, etc.), backtester included, supports local models via Ollama. **Explicitly educational; does not place trades; no credible published alpha.** Good as a reference architecture for signal-generation + explainability, not as evidence of profitability. Source: https://github.com/virattt/ai-hedge-fund
- Other benchmarks worth knowing: ContestTrade (multi-agent internal contest, arXiv 2508.00554), "When Agents Trade" live multi-market benchmark (arXiv 2510.11695), PredictionMarketBench (arXiv 2602.00133).

## 3. Documented failure modes

- **Phantom/hallucinated portfolio state ("epistemic hallucination"):** TradeTrap (arXiv 2512.02261) shows agents believing they hold positions already liquidated, producing "strategic paralysis" and uncontrolled position accumulation. State tampering on a procedural agent: **−61.02% portfolio loss, 91.97% max drawdown, volatility 889.61%**. Memory poisoning (fabricated transaction records): Sharpe 1.92 → −0.24. Source: https://arxiv.org/html/2512.02261v1
- **Prompt injection / adversarial content:** TradeTrap: injected directional-signal reversal cut return 7.81% → 0.89%, Sharpe 5.72 → 0.29, position concentration hit 99.98%; fake-news injection cut return 11.59% → 5.26% and spiked concentration to 77%. **The paper tested no defenses — none exist with empirical validation in this domain.** Real-world: Unit 42 and Forcepoint document in-the-wild indirect prompt injection, including a case-study attack combining hidden HTML on a stock-quote page plus fabricated news that tricked an agent into buying penny stock FCEL instead of PLTR (~$32k simulated client loss), and IPI payloads targeting agent-initiated payments. Sources: https://unit42.paloaltonetworks.com/ai-agent-prompt-injection/ , https://www.forcepoint.com/blog/x-labs/indirect-prompt-injection-payloads , https://arxiv.org/pdf/2603.15714 , https://arxiv.org/pdf/2606.00914 , https://www.helpnetsecurity.com/2026/04/24/indirect-prompt-injection-in-the-wild/ . Implication for this project: any pipeline that feeds scraped news/web text into the LLM is an attack surface; treat all fetched content as untrusted data.
- **Numerical misreading:** Agent Trading Arena (arXiv 2502.17967): LLMs given plain-text price series "focus on absolute values, overlook percentage changes and relational patterns, and overemphasize recent trends"; fail at trend reversals and percentage math. GPT-4o returns: text-only 13.04% vs chart+text 26.18% in their sim — i.e., representation format materially changes decisions, which is itself a fragility. Source: https://arxiv.org/html/2502.17967v2
- **Overconfidence/miscalibration:** "What Does ChatGPT Make of Historical Stock Returns?" (arXiv 2409.11540): LLM return forecasts over-extrapolate past performance and are excessively optimistic on expected value (though better calibrated than human forecasters on risk). KalshiBench (arXiv 2512.16030) links verbal overconfidence to reward-model incentives. Sources: https://arxiv.org/pdf/2409.11540 , https://arxiv.org/pdf/2512.16030
- **Systematic stock-picking biases + stubbornness:** "Your AI, Not Your View" (arXiv 2507.20957): across six LLMs — significant tech-sector buy bias (p<0.001), large-cap favoritism, contrarian tilt; and **when counter-evidence outnumbered supporting evidence 3:2, flip rates stayed below 60%** — models cling to initial positions. Source: https://arxiv.org/html/2507.20957v4
- **Sycophancy/self-confirmation in multi-agent setups:** Identity-driven sycophancy (deferring to peer agents) is empirically widespread in multi-agent debate and more common than self-bias (arXiv 2510.07517 / ACL 2026); multi-agent systems showed unpredictable bias amplification on financial decision tasks (arXiv 2512.16433); isolated self-correction beat homogeneous multi-agent debate (arXiv 2605.00914). Mitigations proposed: anonymized debate, "Disagree-or-Commit" protocols forcing explicit critique (FinCom, arXiv 2606.00939). Implication: bull/bear "debate" agents sharing a backbone tend to converge, not truly adversarially check each other. Sources: https://arxiv.org/html/2510.07517v3 , https://arxiv.org/pdf/2512.16433 , https://arxiv.org/pdf/2605.00914 , https://arxiv.org/pdf/2606.00939
- **Overtrading vs discipline:** Alpha Arena's winner traded least (43 trades/17 days); losers churned. StockBench found reasoning-heavy models underperform — more deliberation produced worse trading. Sources above.
- **Backtest contamination specific to LLMs:** models have memorized historical prices/news/outcomes for any pre-cutoff period, so any backtest before the model's training cutoff is suspect (KTD-Fin proved ticker visibility alone changes behavior; StockBench/LiveTradeBench exist precisely because of this). This is the single biggest methodological trap for a personal backtesting platform using LLMs.

## 4. Architectures that constrain LLMs safely (consensus patterns)

From the Agentic Trading survey (https://arxiv.org/html/2605.19337v1), TradeTrap's recommendations, and observed failure data:
- **LLM-as-analyst, never LLM-as-executor:** LLM emits a structured proposal (ticker, direction, size, confidence, rationale); deterministic code validates ticker existence against a reference list (kills hallucinated tickers), price sanity, position limits, exposure caps, and rejects anything malformed. LLM has no broker credentials or order API access.
- **Deterministic state layer:** portfolio state, open orders, cash, and risk limits are maintained by conventional code and provided to the LLM read-only; the LLM's beliefs about its own positions are never trusted (directly counters TradeTrap's phantom-position and memory-poisoning failures — the worst empirical blowups, −61%, came from state confusion).
- **Hierarchy of truth:** internal system state overrides anything found in external text; fetched news/web content is data, never instructions (prompt-injection mitigation; no validated defense exists, so containment is the defense).
- **Reasoning I/O contract:** strict structured-output schemas (JSON schema / function-calling) with validity checks before any action; reject-and-retry or reject-and-skip on schema violation.
- **Outcome embargo + time-aware memory:** episodic memory must not expose outcomes before their realization time (look-ahead via retrieval is a documented leak).
- **Immutable audit logs** of every tool call, data snapshot, and decision.
- **Hard deterministic failsafes independent of the LLM:** max daily loss, max position size, max order count/day, kill switch, trade-frequency throttle (addresses overtrading), stale-data halt.
- **Human-in-the-loop gate** for any live order at least initially; ensembling/multi-seed runs to detect non-determinism (StockBench used 3 seeds; variance across seeds was material).
- Anonymization of tickers during backtests (KTD-Fin's masking protocol) if you want any signal about genuine skill rather than memorization.

## 5. Realistic expectations vs buy-and-hold SPY

- Every rigorous evaluation puts LLM strategies at or below B&H after bias control; the best leakage-controlled result is ~zero selection alpha (Claude Opus 4.7 in KTD-Fin at +0.2%). Expected value of an LLM-picks-stocks strategy vs SPY: **negative after transaction costs, slippage, LLM API costs, and taxes**, with materially higher drawdown risk (observed: −50% in the real-money micro-cap run; 22% drawdowns in TradingAgents runs; near-liquidation in Alpha Arena).
- Where LLMs plausibly add value for this project: research automation (summarizing filings/news into structured features), sentiment scoring as one input to a conventional systematic strategy, explainable daily reports, and hypothesis generation — not autonomous alpha. The Frontiers review of 84 studies (2022–early 2025) reflects this "signal-input, not decision-maker" consensus: https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1608365/full
- Appropriate benchmark discipline: compare any strategy against SPY total return and equal-weight baseline over the identical window, with costs; treat sub-year outperformance as noise (the micro-cap experiment's +24% month one → −33%+ final is the canonical cautionary tale).

## Verification caveats
- nof1.ai official pages rate-limited (HTTP 429); Alpha Arena figures cross-checked across Yahoo Finance, Bitcoin.com, PANews, and iWeaver secondary coverage, which disagree slightly on interim GPT-5 loss figures (−39.7% interim vs "60%+" peak-drawdown claims). Final standings (Qwen3 Max ~+22%, DeepSeek positive, others negative) are consistent across sources.
- No verified Alpha Arena Season 2 results found as of 2026-07-18.
- Several 2026 arXiv papers cited (2605.x, 2606.x) are recent preprints, not peer-reviewed.
- TradeTrap's quantitative attack results come from the authors' own testbed; no independent replication found.
- The "$32k FCEL/PLTR injection loss" is a simulated case study from security-vendor research (Unit 42), not a reported real client loss.


---

# Regulatory & Practical Constraints

## RESEARCH FINDINGS: US Retail Automated Trading Constraints ($5-10k account), as of 2026-07-18

### 1. FINRA PATTERN DAY TRADER RULE — MAJOR CHANGE: RULE ELIMINATED EFFECTIVE JUNE 4, 2026

**This is the single most important finding.** The classic PDT regime is being retired right now.

- **What changed:** FINRA amended Rule 4210, eliminating (a) the PDT designation/day-trade-count thresholds, (b) the $25,000 minimum equity requirement for PDT accounts, (c) day-trading buying power limits. Filed as SR-FINRA-2025-017; FINRA Board approved Sept 2025; **SEC approved April 14, 2026** (https://www.sec.gov/files/rules/sro/finra/2026/34-105226.pdf); **effective June 4, 2026**, with an **18-month phase-in ending October 20, 2027**. Primary source: FINRA Regulatory Notice 26-10, published April 20, 2026 (https://www.finra.org/rules-guidance/notices/26-10). Secondary: https://www.acaglobal.com/industry-insights/finra-ends-the-pattern-day-trader-rule/, https://www.nerdwallet.com/investing/news/pattern-day-trading-rule-change
- **Replacement framework ("intraday margin," per Notice 26-10):** firms must compute each customer's "intraday margin deficit" (IMD) = highest deficiency between required margin and available equity after any IML-reducing transaction (purchases, short sales). End-of-day pricing permitted for the calc. Deficits must be satisfied "as promptly as possible" via deposits or IML increases; max 15 business days outstanding. **90-calendar-day freeze** (no new shorts/debit increases) if deficits repeatedly unsatisfied by the 5th business day. **De minimis exception: deficits under $1,000 or 5% of account equity don't trigger the freeze.**
- **Practical effect for a $5-10k account:** unlimited day trades in a margin account are now permitted, limited only by real-time margin excess, not a $25k floor. The **$2,000 Reg T minimum equity for margin accounts remains** (confirmed on FINRA's investor day-trading page, updated June 4, 2026: https://www.finra.org/investors/investing/investment-products/stocks/day-trading, and Alpaca's docs: https://docs.alpaca.markets/us/docs/the-intraday-margin-rule).
- **Legacy rule (still relevant during phase-in; brokers implement on their own timelines through Oct 20, 2027):** PDT flag = 4+ day trades within 5 business days in a *margin* account where those trades are >6% of total trades; a day trade = buy and sell (or short and cover) the same security the same day; flagged accounts under $25k were restricted to liquidation-only / 90-day restrictions; day-trading buying power was 4x maintenance margin excess. Existing sub-$25k PDT-restricted accounts are unrestricted under the new framework (per ACA/E*TRADE summaries: https://us.etrade.com/knowledge/library/margin/pattern-day-trading-rule-change).
- **Broker rollout status (mid-2026):** Alpaca implemented the new Intraday Margin Framework on the June 4, 2026 effective date; PDT-related API fields (day-trade count etc.) replaced by `buying_power` and **fully removed from Alpaca's API by July 6, 2026** (https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/, https://docs.alpaca.markets/us/docs/the-intraday-margin-rule). Schwab stopped counting day trades ~June 8, 2026; E*TRADE "shortly after" June 4 (https://us.etrade.com/knowledge/library/margin/pattern-day-trading-rule-change). **Caveat: verify each broker's current status at build time — implementations are actively rolling out and house rules may be stricter than FINRA minimums.**
- Alpaca specifics under new framework: intraday buying power updates dynamically per transaction; unrealized/realized intraday gains increase available margin immediately; same-day deposits and FDIC sweep balances count toward equity; IMD margin call must be met within 2 business days (https://docs.alpaca.markets/us/docs/the-intraday-margin-rule).

### 2. CASH-ACCOUNT TRADING UNDER T+1

- T+1 settlement standard since **May 28, 2024** (equities/ETFs/options settle next business day). PDT rules never applied to cash accounts; the binding constraint is Reg T settled-funds rules.
- **Good-faith violation (GFV):** buying a security with unsettled funds and selling it before the funds used to buy it settle. **3 GFVs in rolling 12 months → 90-day restriction to settled-cash-only trading** (restriction runs from due date of 3rd GFV). Sources: https://www.fidelity.com/learning-center/trading-investing/trading/avoiding-cash-trading-violations, https://www.schwab.com/learn/story/avoid-these-violations-when-trading-cash, https://us.etrade.com/knowledge/library/stocks/understanding-cash-account-violations
- **Freeriding:** paying for a purchase with proceeds of selling that same purchased security (never had the cash). **1 violation → 90-day restriction** (Fidelity, above).
- **Cash liquidation violation:** buying, then selling *other* securities after the purchase date to cover; 3 in 12 months → 90-day restriction (Fidelity, above).
- **Capital-cycling math for a $5-10k cash account:** (a) Buying with settled cash and selling same day is always fine — unlimited day trades, no PDT. (b) Sale proceeds become settled the next business day (T+1), so each dollar supports roughly **one full round trip per day**. (c) You *may* immediately reuse unsettled proceeds to buy again same-day, but selling that second position before the first sale settles = GFV. Net: with $5k settled, ~$5k of round-trip exposure per day, refreshed daily; ~5 full cycles/week. This is materially better than pre-2024 T+2 (which halved cycle rate), and for a research platform doing 0-2 trades/day per dollar, a cash account is workable — though post-June-2026, a margin account with $2k+ equity no longer needs this workaround.

### 3. WASH-SALE RULE & TAX DRAG

- **Wash sale (IRC §1091):** loss disallowed if you buy the same or substantially identical security within 30 days before or after the loss sale (61-day window); applies to options; disallowed loss is added to replacement basis (deferred, not destroyed) — except repurchase in an IRA = permanently lost. Sources: https://www.schwab.com/learn/story/primer-on-wash-sales, https://www.prudential.com/financial-education/how-the-wash-sale-rule-could-affect-your-taxes
- **Impact on an active bot:** repeatedly trading the same tickers generates constant wash-sale basis adjustments; mostly a bookkeeping/1099-B reconciliation nightmare intra-year, but becomes a real cash cost if losses straddle the year-end boundary or land in a tax-advantaged account. Mitigation: go flat and stop trading a losing ticker 31 days before year-end. (https://www.schwab.com/learn/story/year-end-tax-trading-wash-sales-and-more)
- **Tax drag:** all gains on positions held <1 year = **short-term capital gains taxed as ordinary income, 10-37% federal (2026 brackets)** vs LTCG 0/15/20%. **2026 LTCG breakpoints (single): 0% up to $49,450 taxable income; 15% to $545,500; 20% above** (MFJ: $98,900/$613,700) (https://www.kiplinger.com/taxes/irs-updates-capital-gains-tax-thresholds, https://taxfoundation.org/data/all/federal/2026-tax-brackets/). Net capital losses deductible against ordinary income only up to **$3,000/yr** (excess carries forward). +3.8% NIIT above $200k MAGI single (not relevant at this account size unless other income is high).
- **Trader Tax Status + §475(f) mark-to-market election** exempts from wash sales and converts to ordinary treatment — but requires trading as a de facto business (high frequency/volume/regularity) and election by ~April 15 for the current tax year; **a $5-10k research account almost certainly won't qualify** (https://www.tradingsim.com/blog/day-trading-taxes-guide-for-beginners, https://unclekam.com/tax-strategy-blog/day-trading-wash-sale-rule/).
- Concrete drag example: at 24% marginal bracket, $1,000 of realized short-term gains = $240 tax vs $150 (15% LTCG) if held >1yr — a 9-point spread; at 22-32% brackets the spread is 7-17 points. Active strategy must outperform buy-and-hold by roughly that margin after costs to break even post-tax.

### 4. BROKER TERMS ON PERSONAL API AUTOMATION

- **Alpaca** — purpose-built for this exact use case: "customers who can write automated investment code and self-direct their own investments"; commission-free US equities/options via API; reserves right to charge fees if order flow deemed "non-retail in nature" (https://alpaca.markets/, https://alpaca.markets/algotrading). Paper-trading environment is first-class. Best-rated US algo broker 2026 per BrokerChooser (https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading-in-the-united-states).
- **Interactive Brokers** — official TWS API (via TWS/IB Gateway; Python/Java/C++/C#) plus Client Portal REST API; personal automation explicitly supported; rate limits ~50 msgs/sec order submission, 100 concurrent market-data lines on standard accounts (https://www.interactivebrokers.com/en/trading/ib-api.php, https://www.tradealgo.com/trading-guides/tools/best-broker-apis-for-algorithmic-trading-in-2026).
- **Schwab** — official "Trader API — Individual" via developer.schwab.com, free with any brokerage account; personal trading automation is an accepted use (anecdotally, app registrations describing "personal trading automation" approve; "advanced algorithmic trading systems" phrasing gets flagged as institutional) (https://developer.schwab.com/products/trader-api--individual, https://developer.schwab.com/user-guides/individual-developer/about-individual-developer-role).
- **Robinhood** — no official retail *equities* API; ToS prohibits access outside official interfaces; unofficial libraries (robin_stocks etc.) violate ToS and Robinhood closes detected accounts (https://blockresearch.ai/blog/etoro-robinhood-trading-212-bot). BUT: official Crypto Trading API exists (since 2024, https://docs.robinhood.com/), and in 2026 Robinhood launched **"Agentic Trading"** — sanctioned connection of user AI agents with built-in safety controls (https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/). Depth/limits of Agentic Trading not verified this session.
- Bottom line: Alpaca and IBKR are the ToS-safe defaults for a Python bot; Schwab viable; Robinhood equities via unofficial API is a ToS violation with account-closure risk.

### 5. SEC/FINRA DEVELOPMENTS 2024-2026 ON RETAIL ALGO/AI TRADING

- **No new binding SEC/CFTC/FINRA rules specifically governing AI/algorithmic trading as of mid-2026**; regulators rely on existing frameworks (https://www.sidley.com/en/insights/newsupdates/2025/02/artificial-intelligence-us-financial-regulator-guidelines-for-responsible-use).
- **SEC formally withdrew the Gensler-era "predictive data analytics" conflicts proposal (S7-12-23) on June 12, 2025**, among 14 withdrawn proposals; any future AI rulemaking must restart from scratch (https://www.sec.gov/rules-regulations/2025/06/s7-12-23, https://www.proskauer.com/alert/sec-withdraws-fourteen-rule-proposals).
- FINRA 2026 Annual Regulatory Oversight Report has a Gen-AI section (14 member use cases; explicit note on **agentic AI needing governance/controls**); risk areas: recordkeeping, customer data protection, Reg BI, supervision (https://www.finra.org/rules-guidance/guidance/reports/2026-finra-annual-regulatory-oversight-report/gen-ai). SEC 2026 exam priorities include assessing firms' AI supervision policies (https://www.rmmagazine.com/articles/article/2026/06/30/how-regulatory-enforcement-is-shaping-ai-compliance-on-wall-street).
- **Key scoping point: all of the above regulates FINRA member firms/advisers, not retail individuals.** A retail person running an algo on their own money via a broker API needs no license/registration; the Series 57/algo-developer registration requirement applies only to associated persons of member firms (https://www.finra.org/rules-guidance/key-topics/algorithmic-trading). The individual remains subject to ordinary market-manipulation rules (spoofing/layering/wash trading prohibitions) and the broker bears supervisory obligations for the order flow.

### 6. CRYPTO AS PDT-FREE 24/7 SANDBOX (ALPACA)

- **Mechanics:** Alpaca Crypto LLC (separate from Alpaca Securities), **FinCEN-registered MSB, NMLS #2160858** — not SIPC-covered, not a broker-dealer product (https://brokshield.com/alpaca-review-2026/, https://alpaca.markets/). Trading **24/7/365**, 20+ coins, **non-marginable (cash only, no leverage)**, single-order cap $200k notional, some state availability restrictions (https://docs.alpaca.markets/us/docs/crypto-trading-1).
- **PDT/settlement:** PDT and Reg T settled-funds rules never applied to crypto — no day-trade limits, instant/continuous settlement, unlimited capital cycling at any account size. (This advantage is now diminished for equities post-June 2026, but crypto remains the only 24/7 venue and the only one free of margin-account mechanics entirely.)
- **Fees (the real constraint vs commission-free equities):** volume-tiered maker/taker; **Tier 1 (<$100k/30-day volume): 0.15% maker / 0.25% taker**, scaling to 0.00%/0.10% at $100M+; tiers recalc daily 3am ET (https://alpaca.markets/support/crypto-maker-taker-gmt-faq, https://docs.alpaca.markets/us/docs/crypto-fees). A round trip at Tier 1 taker = ~0.50% — this dominates strategy economics at $5-10k and must be modeled in backtests.
- **Regulatory status mid-2026:** **CLARITY Act (digital asset market structure: CFTC gets exclusive jurisdiction over "digital commodity" spot markets, SEC keeps investment-contract assets) passed the House in 2025 and cleared Senate Banking Committee May 14, 2026 (15-9), but is NOT yet law**; full implementation would need agency rulemaking, realistically ~2027 (https://www.lw.com/en/us-crypto-policy-tracker/legislative-developments, https://www.arnoldporter.com/en/perspectives/advisories/2025/08/clarifying-the-clarity-act, https://www.klgates.com/Crypto-in-2026-The-Democratization-of-Digital-Assets-1-29-2026). GENIUS Act (stablecoins) became law July 2025. Practical takeaway: spot BTC/ETH trading via a registered US platform (Alpaca Crypto, Robinhood Crypto, Coinbase) is legal and uncontroversial for retail; the pending legislation affects token classification/venue registration, not the individual retail trader.
- **Crypto taxes:** treated as **property, NOT a security → wash-sale rule does NOT apply as of 2026** (tax-loss harvest and rebuy immediately is legal; repeated legislative proposals to change this have not passed) (https://chainwisecpa.com/crypto-wash-sale-2026/, https://www.irs.gov/instructions/i1099da). Same ST/LT capital-gains treatment as stocks. **Form 1099-DA:** brokers report gross proceeds for tax year 2025 filings; **cost basis becomes "covered"/reported for assets acquired on-platform starting Jan 1, 2026** (https://www.irs.gov/instructions/i1099da, https://www.thetaxadviser.com/issues/2026/mar/navigating-the-form-1099-da-reporting-maze/).

### VERIFICATION / STALENESS NOTES
- PDT elimination is ~6 weeks old at research time; **phase-in runs to Oct 20, 2027 and broker implementations differ** — confirm the chosen broker's live behavior (day-trade counters, house minimums) before building around it. FINRA's own investor page (updated June 4, 2026) already describes the intraday-margin regime and confirms the $2,000 margin minimum persists.
- Alpaca crypto fee tiers verified via Alpaca support page (schedule originally dated 2023); confirm current PDF (https://files.alpaca.markets/disclosures/library/AlpacaCryptoLLCFeeDisclosure.pdf) before modeling.
- Robinhood "Agentic Trading" (2026) announced but capabilities/limits not independently verified this session.
- Cash-violation policies (GFV counts, 90-day windows) are broker-house implementations of Reg T — thresholds cited are Fidelity/Schwab/E*TRADE standard practice, not statute; individual brokers may vary slightly.
- Not covered (out of scope but adjacent): exchange market-data licensing fees for real-time feeds, options-specific rules, futures (CFTC regime has no PDT and favorable 60/40 tax treatment — worth a follow-up if strategy scope expands).