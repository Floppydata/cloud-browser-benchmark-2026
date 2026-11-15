#!/usr/bin/env python3
"""Cloud browser benchmark - analysis of the raw CSV files.

Reads every lifecycle_*, fingerprint_*, targets_*.csv and balance_*.json under
--raw (any depth: collect each server's data/ folder there) and writes
results/cloud_browser_results_<UTC time>.md / .json (and .xlsx when openpyxl
is installed). Output names carry the time, so nothing is ever overwritten.

Rules, the same as the proxy benchmark: rates with Wilson 95% intervals,
medians with bootstrap 95% intervals, and a tie when the intervals overlap -
providers in one tie share a rank.

Only the standard library is needed; openpyxl adds the workbook.
"""
import argparse
import csv
import glob
import json
import math
import os
import random
import re
import statistics
import time
from collections import Counter, defaultdict

HOME = os.path.dirname(os.path.abspath(__file__))
LABELS = {"floppydata": "Floppydata", "gologin": "GoLogin", "steel": "Steel",
          "browserbase": "Browserbase", "kernel": "Kernel", "anchor": "Anchor",
          "hyperbrowser": "Hyperbrowser", "browseruse": "Browser Use", "notte": "Notte",
          "browserless": "Browserless", "lightpanda": "Lightpanda"}
PHASES = ["create_ms", "connect_ms", "goto_ms", "release_ms"]
# weights of the overall rating; warm start is shown on its own, not scored
WEIGHTS = dict(fingerprint=0.40, cost=0.20, sites=0.15, reliability=0.15, speed=0.10)


# Refusals by the provider's billing, not failures of its browser: the account
# ran out of credits (402, Notte's 401 "Free plan limit exceeded") or hit its
# plan's limit (429: Browserless at 10 sessions, Lightpanda's monthly pool).
# They are "not measured" and left out of every metric; COVERAGE counts them.
QUOTA = re.compile(r"http 40[12]|insufficient|credit|free plan limit|429 too many", re.I)
COVERAGE = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # test -> provider -> [measured, scheduled]


# ----------------------------------------------------------------- loading
def load_csv(raw, prefix, servers):
    rows = []
    for path in sorted(glob.glob(os.path.join(raw, "**", "%s_*.csv" % prefix), recursive=True)):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if servers and r.get("server") not in servers:
                    continue
                cov = COVERAGE[prefix][r["provider"]]
                cov[1] += 1
                if QUOTA.search(r.get("err") or ""):
                    continue
                cov[0] += 1
                r["ok"] = int(r.get("ok") or 0)
                rows.append(r)
    return rows


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- stats
def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def boot_median(vals, it=1000, seed=7):
    if not vals:
        return None, None, None
    rnd = random.Random(seed)
    meds = sorted(statistics.median(rnd.choices(vals, k=len(vals))) for _ in range(it))
    return statistics.median(vals), meds[int(0.025 * it)], meds[int(0.975 * it) - 1]


def pctl(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))] if s else None


def tiers(items, higher_better):
    """items: [(key, value, lo, hi)] -> {key: rank}. Sorted by value; a provider
    shares the rank of its group when its interval overlaps the interval of the
    group's best provider. Comparing with the neighbour above instead would
    chain ties: 75% ties 71%, 71% ties 67%... and a 49% ends up tied with 75%."""
    items = [i for i in items if i[1] is not None]
    items.sort(key=lambda i: i[1], reverse=higher_better)
    ranks, rank, lead = {}, 0, None
    for pos, (key, val, lo, hi) in enumerate(items, 1):
        if lead is None or not (lo <= lead[3] and lead[2] <= hi):
            rank, lead = pos, (key, val, lo, hi)
        ranks[key] = rank
    return ranks


def rate(rows, pred):
    k = sum(1 for r in rows if pred(r))
    lo, hi = wilson(k, len(rows))
    return dict(k=k, n=len(rows), rate=(k / len(rows) if rows else None), lo=lo, hi=hi)


# ----------------------------------------------------------------- metrics
def speed(rows, start, conc):
    """Per provider: reliability and median times of one lifecycle setup."""
    out = {}
    by = defaultdict(list)
    for r in rows:
        if (r.get("start") or "cold") == start and str(r.get("concurrency")) == str(conc):
            by[r["provider"]].append(r)
    for prov, rs in by.items():
        good = [r for r in rs if r["ok"]]
        total = [num(r["total_ms"]) for r in good if num(r.get("total_ms")) is not None]
        startup = [sum(num(r[p]) or 0 for p in ("create_ms", "connect_ms")) for r in good]
        med, lo, hi = boot_median(total)
        smed, slo, shi = boot_median(startup)
        out[prov] = dict(
            reliability=rate(rs, lambda r: r["ok"]),
            total=dict(median=med, lo=lo, hi=hi, p95=pctl(total, 0.95), p99=pctl(total, 0.99)),
            startup=dict(median=smed, lo=slo, hi=shi),
            phases={p[:-3]: statistics.median(v) if v else None for p in PHASES
                    for v in [[num(r[p]) for r in good if num(r.get(p)) is not None]]},
            servers={s: statistics.median(v) if v else None
                     for s in sorted({r["server"] for r in good})
                     for v in [[num(r["total_ms"]) for r in good if r["server"] == s
                                and num(r.get("total_ms")) is not None]]},
            errors=dict(Counter(re.sub(r"\s+", " ", r["err"])[:80] for r in rs if not r["ok"])
                        .most_common(3)))
    return out


def fingerprint(rows):
    # recomputed with the runner's own check, so rows written before its
    # alias fix (America/Indianapolis) are judged the same way as new ones
    from cbbench import tz_in_country
    for r in rows:
        if r["ok"] and r.get("country"):
            r["tz_match"] = str(int(tz_in_country(r.get("tz"), r["country"])))
    out = {}
    by = defaultdict(list)
    for r in rows:
        by[r["provider"]].append(r)
    for prov, rs in by.items():
        good = [r for r in rs if r["ok"]]
        chrome = Counter(m.group(1) for r in good
                         for m in [re.search(r"Chrome/(\d+)", r.get("ua") or "")] if m)
        clean = lambda r: (r.get("country") == "us" and str(r.get("tz_match")) == "1"  # noqa: E731
                           and str(r.get("webrtc_leak")) != "1" and str(r.get("webdriver")) != "True"
                           and str(r.get("headless")) != "1")
        out[prov] = dict(
            sessions=len(rs), ok=len(good),
            clean=rate(rs, lambda r: r["ok"] and clean(r)),
            us_ip=rate(good, lambda r: r.get("country") == "us"),
            tz_match=rate(good, lambda r: str(r.get("tz_match")) == "1"),
            webrtc_leak=rate(good, lambda r: str(r.get("webrtc_leak")) == "1"),
            webdriver=rate(good, lambda r: str(r.get("webdriver")) == "True"),
            headless_ua=rate(good, lambda r: str(r.get("headless")) == "1"),
            chrome=dict(chrome.most_common(3)),
            platforms=dict(Counter(r.get("platform") for r in good).most_common(3)),
            hw_mem=dict(Counter("%s/%s" % (r.get("hw"), r.get("mem")) for r in good).most_common(3)),
            unique_ips=len({r.get("ip") for r in good}),
            webgl_distinct=len({r.get("webgl") for r in good}))
    return out


def site_groups(path):
    """site -> anti-bot group, from the '# --- group' headers of targets.txt."""
    groups, cur = {}, "other"
    if not os.path.exists(path):
        return groups
    for line in open(path):
        m = re.match(r"#\s*---\s*([\w-]+)", line)
        if m:
            cur = m.group(1)
            continue
        url = line.split("#", 1)[0].strip()
        if url:
            host = re.sub(r"^https?://", "", url).split("/")[0]
            groups[host.removeprefix("www.")] = cur
    return groups


def targets(rows, groups):
    out = {}
    by = defaultdict(list)
    for r in rows:
        by[r["provider"]].append(r)
    for prov, rs in by.items():
        ok = [r for r in rs if r["verdict"] == "ok"]
        per_site = defaultdict(list)
        for r in rs:
            per_site[r["site"]].append(r["verdict"] == "ok")
        per_group = defaultdict(list)
        for r in rs:
            per_group[groups.get(r["site"], "other")].append(r)
        loads = [num(r["load_ms"]) for r in ok if num(r.get("load_ms")) is not None]
        kbs = [num(r["kb"]) for r in ok if num(r.get("kb")) is not None]
        out[prov] = dict(
            got_through=rate(rs, lambda r: r["verdict"] == "ok"),
            blocked_every_visit=sorted(s for s, v in per_site.items() if not any(v)),
            sites=len(per_site),
            by_group={g: rate(v, lambda r: r["verdict"] == "ok") for g, v in sorted(per_group.items())},
            load_median=statistics.median(loads) if loads else None,
            kb_median=statistics.median(kbs) if kbs else None,
            errors=dict(Counter(re.sub(r"\s+", " ", r["err"])[:80] for r in rs if r["err"])
                        .most_common(3)))
    return out


def usage_cost(rows, pricing):
    """Usage cost of the targets test per 1000 pages that got through, the same
    way for every provider: browser time (create + connect + load + release) at
    its hourly rate plus page traffic seen over CDP at its proxy rate (list prices
    in pricing.json). Failed visits cost too. Floppydata's billed traffic, read
    from its balance, is reported on its own (floppydata_cost)."""
    out = {}
    by = defaultdict(list)
    for r in rows:
        by[r["provider"]].append(r)
    for prov, rs in by.items():
        price = pricing.get(prov)
        ok = sum(1 for r in rs if r["verdict"] == "ok")
        if not price or not ok:
            continue
        hours = sum(sum(num(r.get(k)) or 0 for k in ("create_ms", "connect_ms", "load_ms", "release_ms"))
                    for r in rs) / 3.6e6
        gb = sum(num(r.get("kb")) or 0 for r in rs) / 1024 ** 2
        usd = hours * price["usd_per_hour"] + gb * (price["usd_per_gb"] or 0)
        out[prov] = dict(usd_per_1000_ok=usd / ok * 1000, hours=hours, gb=gb, ok_pages=ok,
                         proxy_priced=price["usd_per_gb"] is not None)
    return out


def overall(res, weights):
    """One score per provider: in every metric, points = the provider's value as
    a share of the best value (best / value where lower is better), then the
    weighted sum; a metric a provider lacks hands its weight to the others.
    The tied ranks are kept per metric for the leader marks."""
    cold, tg, fp, cost = res["cold"], res["targets"], res["fingerprint"], res["cost"]
    pooled = res["reliability_all"]
    values = dict(
        sites={k: v["got_through"]["rate"] for k, v in tg.items()},
        reliability={k: v["rate"] for k, v in pooled.items()},
        speed={k: v["total"]["median"] for k, v in cold.items() if v["total"]["median"] is not None},
        cost={k: v["usd_per_1000_ok"] for k, v in cost.items() if v["proxy_priced"]},
        fingerprint={k: v["clean"]["rate"] for k, v in fp.items()})
    lower = {"speed", "cost"}
    metrics = dict(
        sites=tiers([(k, v["got_through"]["rate"], v["got_through"]["lo"], v["got_through"]["hi"])
                     for k, v in tg.items()], True),
        reliability=tiers([(k, v["rate"], v["lo"], v["hi"]) for k, v in pooled.items()], True),
        speed=tiers([(k, v["total"]["median"], v["total"]["lo"], v["total"]["hi"])
                     for k, v in cold.items()], False),
        # a provider whose proxy price is not published is not ranked on cost:
        # its traffic would count as free (Lightpanda)
        cost=tiers([(k, v["usd_per_1000_ok"], v["usd_per_1000_ok"], v["usd_per_1000_ok"])
                    for k, v in cost.items() if v["proxy_priced"]], False),
        fingerprint=tiers([(k, v["clean"]["rate"], v["clean"]["lo"], v["clean"]["hi"])
                           for k, v in fp.items()], True))
    provs = sorted({k for m in metrics.values() for k in m})
    out = {}
    for p in provs:
        pts, wsum = 0.0, 0.0
        detail = {}
        for m, ranks in metrics.items():
            if p not in ranks or p not in values[m]:
                continue
            x = values[m][p]
            best = (min if m in lower else max)(values[m].values())
            if m in lower:
                point = best / x if x else 0.0
            else:
                point = x / best if best else 0.0
            detail[m] = dict(rank=ranks[p], points=round(point, 3))
            pts += weights[m] * point
            wsum += weights[m]
        out[p] = dict(score=round(100 * pts / wsum, 1) if wsum else None, metrics=detail)
    return out


def floppydata_cost(raw, servers):
    used = visits = ok = 0
    runs = []
    for path in sorted(glob.glob(os.path.join(raw, "**", "balance_*.json"), recursive=True)):
        rec = json.load(open(path))
        if servers and rec.get("server") not in servers:
            continue
        used += rec["used_bytes"]
        visits += rec["visits"]
        ok += rec["ok_pages"]
        runs.append(rec["run_id"])
    if not ok:
        return None
    gb = used / 1024 ** 3
    return dict(runs=len(runs), gb=gb, visits=visits, ok_pages=ok,
                gb_per_1000_ok=gb / ok * 1000, usd_per_1000_ok=gb / ok * 1000 * 1.0)


# ------------------------------------------------------------------ output
def ms(v):
    return "-" if v is None else "%.0f" % v


def pct(d):
    if not d or d["n"] == 0:
        return "-"
    return "%.1f%% (%.1f–%.1f), n=%d" % (100 * d["rate"], 100 * d["lo"], 100 * d["hi"], d["n"])


def md_table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def speed_section(title, data, rank_on="total"):
    rk = tiers([(p, d[rank_on]["median"], d[rank_on]["lo"], d[rank_on]["hi"])
                for p, d in data.items()], higher_better=False)
    rel = tiers([(p, d["reliability"]["rate"], d["reliability"]["lo"], d["reliability"]["hi"])
                 for p, d in data.items()], higher_better=True)
    rows = []
    for p in sorted(data, key=lambda p: rk.get(p, 99)):
        d = data[p]
        rows.append([rk.get(p, "-"), LABELS.get(p, p), ms(d["startup"]["median"]),
                     "%s (%s–%s)" % (ms(d["total"]["median"]), ms(d["total"]["lo"]), ms(d["total"]["hi"])),
                     ms(d["total"]["p95"]), ms(d["total"]["p99"]),
                     " / ".join(ms(d["phases"][k[:-3]]) for k in PHASES),
                     pct(d["reliability"]), rel.get(p, "-")])
    return "## %s\n\n%s\n" % (title, md_table(
        ["Rank", "Provider", "Start-up median (create+connect)", "Total median (95% CI)",
         "p95", "p99", "create / connect / goto / release", "Reliability (95% CI)",
         "Reliability rank"], rows))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw", default=os.path.join(HOME, "collected"),
                   help="folder with the servers' data (searched at any depth)")
    p.add_argument("--servers", default="de,us,sg", help="comma separated; empty = all")
    p.add_argument("--targets", default=os.path.join(HOME, "targets.txt"))
    p.add_argument("--out-dir", default=os.path.join(HOME, "results"))
    p.add_argument("--pricing", default=os.path.join(HOME, "pricing.json"))
    args = p.parse_args()
    servers = set(args.servers.split(",")) if args.servers else set()

    life = load_csv(args.raw, "lifecycle", servers)
    tg_rows = load_csv(args.raw, "targets", servers)
    # verdicts corrected by hand in review_verdicts.py's page win over the runner's
    overrides = {}
    for path in glob.glob(os.path.join(args.raw, "**", "verdict_overrides.json"), recursive=True):
        overrides.update(json.load(open(path)))
    changed = 0
    for r in tg_rows:
        v = overrides.get("%s|%s|%s" % (r["run_id"], r["provider"], r["site"]))
        if v and v != r["verdict"]:
            r["verdict"], changed = v, changed + 1
    pricing = json.load(open(args.pricing))["providers"]
    cold_by = defaultdict(list)
    for r in life:
        if (r.get("start") or "cold") == "cold":
            cold_by[r["provider"]].append(r)
    res = dict(
        weights=WEIGHTS,
        verdicts_corrected=changed,
        reliability_all={p: rate(rs, lambda r: r["ok"]) for p, rs in cold_by.items()},
        cost=usage_cost(tg_rows, pricing),
        generated_utc=time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        servers=sorted(servers) or "all",
        cold=speed(life, "cold", 1),
        warm=speed(life, "warm", 1),
        load={c: speed(life, "cold", c) for c in sorted(
            {int(r["concurrency"]) for r in life if r.get("concurrency")} - {1})},
        fingerprint=fingerprint(load_csv(args.raw, "fingerprint", servers)),
        targets=targets(tg_rows, site_groups(args.targets)),
        floppydata_cost=floppydata_cost(args.raw, servers))
    res["overall"] = overall(res, WEIGHTS)
    # share of scheduled sessions and visits that were measured; under 25% of
    # the site visits = "limited data" on the page
    res["coverage"] = {p: dict({t: dict(measured=COVERAGE[t][p][0], scheduled=COVERAGE[t][p][1])
                                for t in ("lifecycle", "fingerprint", "targets") if p in COVERAGE[t]},
                               servers=sorted({r["server"] for r in tg_rows if r["provider"] == p}))
                       for p in {p for t in COVERAGE.values() for p in t}}
    for p, c in res["coverage"].items():
        tgc = c.get("targets", dict(measured=0, scheduled=0))
        c["targets_share"] = tgc["measured"] / tgc["scheduled"] if tgc["scheduled"] else None
        c["limited"] = c["targets_share"] is not None and c["targets_share"] < 0.25

    md = ["# Cloud Browser Benchmark — results",
          "Generated %s UTC from servers: %s. Ranks: ties when 95%% intervals overlap."
          % (res["generated_utc"], ", ".join(res["servers"]) if servers else "all"),
          "Target verdicts corrected by screenshot review: %d." % res["verdicts_corrected"], ""]
    ov = res["overall"]
    order = sorted(ov, key=lambda p: -(ov[p]["score"] or 0))
    names = list(WEIGHTS)
    md.append("## Overall rating\n\nWeights: %s. Points per metric = value as a share of the best value "
              "(best / value where lower is better); the ranks shown are tied ranks (95%% intervals); "
              "warm start is shown on its own, not scored.\n\n" % ", ".join(
                  "%s %d%%" % (m, 100 * w) for m, w in WEIGHTS.items())
              + md_table(["#", "Provider", "Score"] + ["%s rank" % m for m in names],
                         [[1 + sum(1 for q in order if (ov[q]["score"] or 0) > (ov[p]["score"] or 0)),
                           LABELS.get(p, p), ov[p]["score"]]
                          + [ov[p]["metrics"].get(m, {}).get("rank", "-") for m in names]
                          for p in order]) + "\n")
    cov = res["coverage"]
    md.append("## Coverage (refusals for credits or plan limits are not measured)\n\n" + md_table(
        ["Provider", "Site visits measured", "Sessions measured", "Fingerprint measured", "Servers with site data", "Limited"],
        [[LABELS.get(p, p),
          "%d / %d" % (c["targets"]["measured"], c["targets"]["scheduled"]) if "targets" in c else "-",
          "%d / %d" % (c["lifecycle"]["measured"], c["lifecycle"]["scheduled"]) if "lifecycle" in c else "-",
          "%d / %d" % (c["fingerprint"]["measured"], c["fingerprint"]["scheduled"]) if "fingerprint" in c else "-",
          ", ".join(c["servers"]) or "-", "yes" if c["limited"] else ""]
         for p, c in sorted(cov.items(), key=lambda kv: kv[1]["targets_share"] or 0)]) + "\n")
    cost = res["cost"]
    md.append("## Cost per 1000 pages that got through (list usage prices, pricing.json)\n\n" + md_table(
        ["Provider", "USD per 1000", "Browser hours", "GB (CDP)", "Pages through"],
        [[LABELS.get(p, p), "%.2f%s" % (v["usd_per_1000_ok"], "" if v["proxy_priced"] else " (proxy not priced)"),
          "%.2f" % v["hours"], "%.3f" % v["gb"], v["ok_pages"]]
         for p, v in sorted(cost.items(), key=lambda kv: kv[1]["usd_per_1000_ok"])]) + "\n")
    md.append(speed_section("Speed, cold start (new profile every session, c=1)", res["cold"]))
    md.append(speed_section("Speed, warm start (saved profile, c=1)", res["warm"]))
    for c, data in res["load"].items():
        md.append(speed_section("Load, %d sessions at once (cold)" % c, data))

    tg = res["targets"]
    rk = tiers([(k, v["got_through"]["rate"], v["got_through"]["lo"], v["got_through"]["hi"])
                for k, v in tg.items()], higher_better=True)
    groups = sorted({g for v in tg.values() for g in v["by_group"]})
    rows = []
    for k in sorted(tg, key=lambda k: rk.get(k, 99)):
        v = tg[k]
        rows.append([rk.get(k, "-"), LABELS.get(k, k), pct(v["got_through"]),
                     "%d of %d" % (len(v["blocked_every_visit"]), v["sites"])]
                    + ["%.0f%%" % (100 * v["by_group"][g]["rate"]) if g in v["by_group"] else "-"
                       for g in groups]
                    + [ms(v["load_median"]), ms(v["kb_median"])])
    md.append("## Bot-protected sites\n\n" + md_table(
        ["Rank", "Provider", "Got through (95% CI)", "Blocked on every visit"] + groups
        + ["Load median, ms", "KB median"], rows) + "\n")

    fp = res["fingerprint"]
    rows = [[LABELS.get(k, k), "%d/%d" % (v["ok"], v["sessions"]), pct(v["us_ip"]), pct(v["tz_match"]),
             pct(v["webrtc_leak"]), pct(v["webdriver"]), pct(v["headless_ua"]),
             ", ".join("%s×%d" % kv for kv in v["chrome"].items()),
             ", ".join("%s×%d" % kv for kv in v["hw_mem"].items()), v["webgl_distinct"]]
            for k, v in sorted(fp.items())]
    md.append("## Fingerprint\n\n" + md_table(
        ["Provider", "OK", "US IP", "Timezone matches IP", "WebRTC leak", "webdriver true",
         "Headless in UA", "Chrome versions", "Cores/GB", "WebGL renderers"], rows) + "\n")

    fc = res["floppydata_cost"]
    md.append("## Floppydata traffic\n\n" + (
        "%.3f GB over %d targets runs, %d visits, %d got through: %.2f GB = $%.2f per 1000 "
        "successful pages at $1/GB.\n" % (fc["gb"], fc["runs"], fc["visits"], fc["ok_pages"],
                                          fc["gb_per_1000_ok"], fc["usd_per_1000_ok"])
        if fc else "No balance files.\n"))

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = os.path.join(args.out_dir, "cloud_browser_results_%s" % stamp)
    with open(base + ".md", "w") as f:
        f.write("\n".join(md))
    with open(base + ".json", "w") as f:
        json.dump(res, f, indent=1, default=str)
    written = [base + ".md", base + ".json"]
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for block in md:
            lines = [l for l in block.splitlines() if l.startswith("|") and not l.startswith("|---")]
            if not lines:
                continue
            title = block.splitlines()[0].lstrip("# ").strip()[:31] or "Sheet"
            ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "", title)[:31])
            for l in lines:
                ws.append([c.strip() for c in l.strip("|").split("|")])
        wb.save(base + ".xlsx")
        written.append(base + ".xlsx")
    except ImportError:
        pass
    print("Written:\n  " + "\n  ".join(written))


if __name__ == "__main__":
    main()
