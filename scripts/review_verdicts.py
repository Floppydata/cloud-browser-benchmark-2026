#!/usr/bin/env python3
"""Screenshot review of the contested target visits.

The runner's verdict (ok / blocked / empty) comes from markers and status
codes; the pilot showed it can be wrong (Amazon read too early, Glassdoor's
"Humans only" page taken for a real one). This builds one HTML page with the
screenshots of the visits most likely to be wrong, so a person can check them
in minutes and correct the verdict where needed:

  empty        no challenge marker but almost no text
  error-status ok, yet the first response was 401/403/429
  odd-one-out  in one run, one provider differs from all the others on a site
  late         ok, but the page took over 25 s (likely a challenge that cleared)

Open the page in a browser, click the right verdict on each card and press
"Download overrides": verdict_overrides.json goes next to the data, and
analyze_cloud.py applies it.

Usage: python3 review_verdicts.py --raw collected [--limit 150]
"""
import argparse
import csv
import glob
import html
import json
import os
from collections import defaultdict

HOME = os.path.dirname(os.path.abspath(__file__))


def load(raw):
    rows = []
    for path in sorted(glob.glob(os.path.join(raw, "**", "targets_*.csv"), recursive=True)):
        base = os.path.dirname(os.path.dirname(path))  # <server folder>/data/x.csv -> <server folder>
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("screenshot"):
                    r["image"] = os.path.join(base, r["screenshot"])
                rows.append(r)
    return rows


def key(r):
    return "%s|%s|%s" % (r["run_id"], r["provider"], r["site"])


def contested(rows):
    out = []
    by_visit = defaultdict(list)  # one site in one run: every provider at the same moment
    for r in rows:
        if r["verdict"] in ("ok", "blocked", "empty"):
            by_visit[(r["run_id"], r["site"])].append(r)
    for r in rows:
        v = r["verdict"]
        if v == "empty":
            out.append((r, "empty", "no marker, %s characters of text" % r["text_len"]))
        elif v == "ok" and r["status"] in ("401", "403", "429"):
            out.append((r, "error-status", "first response %s, %s characters" % (r["status"], r["text_len"])))
        elif v == "ok" and float(r.get("load_ms") or 0) > 25000:
            out.append((r, "late", "loaded in %.0f s" % (float(r["load_ms"]) / 1000)))
    for (_, site), rs in by_visit.items():
        if len(rs) < 4:
            continue
        for r in rs:
            others = [x["verdict"] == "ok" for x in rs if x is not r]
            if all(others) and r["verdict"] != "ok":
                out.append((r, "odd-one-out", "the only one not through"))
            elif not any(others) and r["verdict"] == "ok":
                out.append((r, "odd-one-out", "the only one through"))
    seen, uniq = set(), []
    for item in out:
        if key(item[0]) not in seen:
            seen.add(key(item[0]))
            uniq.append(item)
    return uniq


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Verdict review</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--ok:#1f7a3a;--bad:#b3261e;--card:#f7f7f7}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--muted:#aaa;--line:#333;--card:#202020}}
body{margin:0;padding:16px;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:1}
.card{border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:12px;background:var(--card)}
.card img{max-width:100%;border:1px solid var(--line);margin-top:8px}
.meta{color:var(--muted);font-size:13px}.why{font-weight:600}
button{margin-right:6px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
button.on.ok{background:var(--ok);color:#fff}button.on.blocked{background:var(--bad);color:#fff}
.done{opacity:.55}
</style></head><body>
<header><b>Verdict review</b> · <span id="count"></span> ·
<button onclick="dl()">Download overrides</button>
<label><input type="checkbox" id="hide" onchange="filt()"> hide reviewed</label></header>
<main id="cards">__CARDS__</main>
<script>
const KEY='cbbench-overrides';let o={};try{o=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
function save(){try{localStorage.setItem(KEY,JSON.stringify(o))}catch(e){}}
function paint(){document.querySelectorAll('.card').forEach(c=>{const v=o[c.dataset.k];
c.classList.toggle('done',!!v);c.querySelectorAll('button[data-v]').forEach(b=>b.classList.toggle('on',b.dataset.v===v))});
document.getElementById('count').textContent=Object.keys(o).length+' of '+document.querySelectorAll('.card').length+' reviewed';filt()}
function pick(b){const k=b.closest('.card').dataset.k;o[k]=b.dataset.v;save();paint()}
function filt(){const h=document.getElementById('hide').checked;document.querySelectorAll('.card').forEach(c=>c.style.display=h&&o[c.dataset.k]?'none':'')}
function dl(){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(o,null,1)],{type:'application/json'}));
a.download='verdict_overrides.json';a.click()}
paint();
</script></body></html>"""


def card(r, why, detail, out_dir):
    img = ""
    if r.get("image") and os.path.exists(r["image"]):
        img = '<img loading="lazy" src="%s" alt="">' % html.escape(os.path.relpath(r["image"], out_dir))
    return ('<div class="card" data-k="%s"><div><span class="why">%s</span> · %s · <b>%s</b> · %s</div>'
            '<div class="meta">runner: %s · %s · marker %s · title "%s" · %s · run %s</div>'
            '<div><button data-v="ok" class="ok" onclick="pick(this)">got through</button>'
            '<button data-v="blocked" class="blocked" onclick="pick(this)">blocked</button>'
            '<button data-v="%s" onclick="pick(this)">runner is right</button></div>%s</div>') % (
        html.escape(key(r)), why, html.escape(r["site"]), html.escape(r["provider"]), html.escape(detail),
        r["verdict"], r["server"], html.escape(r.get("marker") or "-"), html.escape(r.get("title") or ""),
        html.escape(r.get("final_url") or ""), html.escape(r["run_id"]), r["verdict"], img)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw", default=os.path.join(HOME, "collected"))
    p.add_argument("--limit", type=int, default=150, help="cards per kind, at most")
    p.add_argument("--out", default="")
    args = p.parse_args()
    items = contested(load(args.raw))
    per = defaultdict(int)
    picked = []
    for r, why, detail in sorted(items, key=lambda i: (i[1], i[0]["site"], i[0]["provider"])):
        if per[why] < args.limit:
            per[why] += 1
            picked.append((r, why, detail))
    out = args.out or os.path.join(args.raw, "review.html")
    out_dir = os.path.dirname(os.path.abspath(out))
    with open(out, "w") as f:
        f.write(PAGE.replace("__CARDS__", "\n".join(card(r, w, d, out_dir) for r, w, d in picked)))
    print("%d contested visits (%s); %d on the page: %s" % (
        len(items), ", ".join("%s %d" % kv for kv in sorted(per.items())), len(picked), out))


if __name__ == "__main__":
    main()
