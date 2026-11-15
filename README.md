# Cloud Browser Benchmark 2026: raw data and scripts

Raw measurements, test scripts and results behind the Cloud Browser Benchmark 2026 published by Floppydata.

**Disclosure:** Floppydata is one of the 11 tested providers and also the publisher of this benchmark.
Floppydata and GoLogin, also in this test, share infrastructure, so some of their browser-side numbers can be close.
Every provider was tested with the same script, servers, sites and schedule. That is why the raw data
and the code are public: you can check the numbers yourself.

## What was tested

- **Period:** 12-15 November 2026 (UTC).
- **Providers (11):** Floppydata, GoLogin, Steel, Browserbase, Kernel, Anchor, Hyperbrowser, Browser Use,
  Notte, Browserless, Lightpanda. Each was reached over the Chrome DevTools Protocol with Playwright.
- **Test servers (3):** Germany (Falkenstein), US (Ashburn, VA), Singapore (Hetzner cloud). In the files
  they are `de`, `us`, `sg`. The servers took turns and never ran at the same time.
- **Tests:**
  1. Speed: create, connect, first navigation and release of a session, in milliseconds, first attempt only.
     Cold start (a new profile every session) and warm start (one saved profile; not measured for
     Floppydata in this run, because the script did not save a reusable profile for it).
  2. Load: 5 and 10 sessions at once, as far as each provider's plan allows.
  3. Fingerprint: exit IP and country, timezone against the IP's country, WebRTC, `navigator.webdriver`,
     headless markers, Chrome version, hardware and WebGL as a site sees them.
  4. Bot-protected sites: 61 pages that blocked plain headless Chrome from the US server, every provider
     visiting each page at the same moment, with a screenshot of every visit.
  5. Cost per 1000 pages that got through, at each provider's published usage prices (`config/pricing.json`).

Speed tests run without proxies where a provider allows it; the fingerprint and site tests use each
provider's own US proxy and whatever CAPTCHA solving and stealth its tested plan includes. Enterprise-only
modes were not tested. The full method, the setup of every provider and the limits are in the article.

## Overall score

| Metric | Weight |
|---|---|
| Clean fingerprint (share of sessions) | 40% |
| Cost per 1000 pages that got through | 20% |
| Bot-protected sites: share of visits that got through | 15% |
| Reliability: share of cold sessions that completed | 15% |
| Speed: median cold-start session time | 10% |

Each metric gives a provider its value as a share of the best value among the providers (for cost and
speed, where lower is better: best value / provider's value), times the weight. The score is the sum, out
of 100. If a provider has no value for a metric, that weight is spread over its other metrics; Lightpanda
is not scored on cost, because the price of its proxy traffic is not published. Warm start is reported on
its own and not scored.

A session counts as having a clean fingerprint when the exit IP is in the US, the browser's timezone is in
the IP's country, WebRTC shows no other public IP, `navigator.webdriver` is off and the user agent has no
headless marker.

## Coverage

Requests that a provider refused because the account ran out of credits or hit a plan limit (HTTP 401/402,
"insufficient credits", "free plan limit", 429) are counted as not measured, not as failures. The results
list how many scheduled visits, sessions and fingerprint checks were measured for each provider. Anchor and
Notte ran out of credits early, so only a small part of their schedule was measured; their numbers are
marked as limited data.

## Screenshots

Every site visit was saved as a screenshot (about 14,000 files, 3.9 GB). They are not in this repository
because of their size. Visits whose verdict the heuristics could not settle clearly were checked against
their screenshots on a sample: 20 of 22 verdicts were right and the two wrong ones went in opposite
directions, so no verdict was changed (`scripts/review_verdicts.py` builds that review page).

## Repository content

| Path | What it is |
|---|---|
| `scripts/cbbench.py` | The runner: one script for every provider and test |
| `scripts/run.sh` | One scheduled run on a server (full or light) |
| `scripts/analyze_cloud.py` | Tables, intervals and the overall rating from the raw files |
| `scripts/review_verdicts.py` | The screenshot review page for contested site visits |
| `config/targets.txt` | The 61 tested pages, grouped by anti-bot product |
| `config/candidates.txt` | The 110 candidate pages the list was chosen from |
| `config/pricing.json` | Usage prices used for the cost metric, with sources |
| `config/schedule.txt` | When each server ran |
| `data/raw/{de,us,sg}/lifecycle_*.csv` | Speed and load tests, one row per session |
| `data/raw/{de,us,sg}/fingerprint_*.csv` | Fingerprint test, one row per session |
| `data/raw/{de,us,sg}/targets_*.csv` | Site test, one row per visit |
| `data/raw/{de,us,sg}/balance_*.json` | Floppydata traffic balance before and after each site run |
| `data/raw/us-baseline/` | The plain-Chrome run that selected the 61 pages |
| `results/cloud_browser_results_*.md` | The published tables |
| `results/cloud_browser_results_*.json` | The same numbers, machine-readable |
| `results/cloud_browser_results_*.xlsx` | The same tables as an Excel workbook |

## Anonymization

Exit IP addresses belong to the providers' proxy pools, so they are not published. In the `ip` and
`webrtc_ips` columns, and wherever an address appears in an error text, the real address is replaced by
`ip_` plus 16 hex characters: a keyed hash (HMAC-SHA256) with a random key that was used once and then
discarded. The same address gets the same value in all files, so unique-IP counts are unchanged; the hashes
cannot be turned back into addresses. Provider API keys, saved profile ids, server addresses and logs are
not published. No other columns were changed.

## Reproduce the numbers

You need Python 3.9+; `openpyxl` adds an Excel workbook to the output.

```bash
python3 scripts/analyze_cloud.py --raw data --targets config/targets.txt --pricing config/pricing.json --out-dir my_results
```

This writes `my_results/cloud_browser_results_<time>.md` and `.json` (and `.xlsx` with `openpyxl`) with the
same numbers as `results/`. The overall score is computed as described above. The ranks shown per metric,
and the leaders marked in the article, use one rule: two providers tie when their 95% intervals overlap
(Wilson intervals for rates, bootstrap intervals for medians).

## Run it yourself

```bash
python3 -m venv .venv && .venv/bin/pip install playwright
.venv/bin/python scripts/cbbench.py setup            # your own provider keys, stored only on your machine
.venv/bin/python scripts/cbbench.py check            # one session per provider
.venv/bin/python scripts/cbbench.py lifecycle --server mybox --n 30
```

`scripts/cbbench.py --help` lists every test. Session setups per provider are in `BODIES`, `REST` and `WS`
at the top of the script.
