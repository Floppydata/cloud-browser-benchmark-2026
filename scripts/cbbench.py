#!/usr/bin/env python3
"""Cloud browser benchmark - pilot runner.

Commands:
  setup        save provider keys (file is readable only by you)
  check        one session per provider: create, connect, open a page, release
  lifecycle    session speed and reliability: create, connect, goto, release (ms)
  fingerprint  what a site sees: IP, country, timezone, WebRTC, webdriver, WebGL
  targets      visits to bot-protected sites from targets.txt, with screenshots
  baseline     the same visits with plain local headless Chrome, to find pages
               that really block bots (writes targets_baseline.txt)
  summary      tables from data/*.csv (rates with 95% CI, median/p95/p99)

Needs Python 3.9+ and Playwright for Python (pip install playwright). No local
browser is needed: every session is a remote browser reached over CDP.
Every session is first-attempt: no retries, a failure is a data point.
"""
import argparse
import asyncio
import csv
import getpass
import glob
import ipaddress
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

HOME = os.environ.get("BENCH_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HOME, "providers.json")
DATA = os.path.join(HOME, "data")
SHOTS = os.path.join(HOME, "shots")
TARGETS = os.path.join(HOME, "targets.txt")

DEFAULT_URL = "https://www.google.com/"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# What each provider needs. "secret" fields are typed hidden and never printed.
PROVIDERS = {
    "floppydata": dict(label="Floppydata", fields=[("api_key", True)]),
    "gologin": dict(label="GoLogin", fields=[("token", True), ("profile_id", False)]),
    "steel": dict(label="Steel", fields=[("api_key", True)]),
    "browserbase": dict(label="Browserbase", fields=[("api_key", True)]),
    "kernel": dict(label="Kernel", fields=[("api_key", True)]),
    "anchor": dict(label="Anchor", fields=[("api_key", True)]),
    "hyperbrowser": dict(label="Hyperbrowser", fields=[("api_key", True)]),
    "browseruse": dict(label="Browser Use", fields=[("api_key", True)]),
    "notte": dict(label="Notte", fields=[("api_key", True)]),
    "browserless": dict(label="Browserless", fields=[("token", True)]),
    "lightpanda": dict(label="Lightpanda", fields=[("token", True)]),
}

FD_API = "https://api.floppydata.net/v2/browser/sessions"
FD_BALANCE = "https://api.floppydata.net/v2/account/balances"
GL_CONNECT = "wss://cloudbrowser.gologin.com/connect"
STEEL_API = "https://api.steel.dev/v1/sessions"
STEEL_CONNECT = "wss://connect.steel.dev"

GL_API = "https://api.gologin.com"
STATE = os.path.join(DATA, "state.json")

# Session bodies per test kind, the same idea as the published benchmarks:
# "real" (fingerprint, targets, check) is the product as a customer runs it -
# each provider's own US residential proxy, like Browser Use's Stealth Bench;
# "speed" (lifecycle) keeps proxies out where the provider allows it, like
# Steel's browserbench and browserarena. Floppydata always runs through its
# proxies, so its speed body takes the lightest one, datacenter.
# A provider's "custom_body" in providers.json replaces these.
BODIES = {
    "real": {
        "floppydata": {"proxy": {"type": "residential", "location": {"countryCode": "US"}},
                       "browser": {"fingerprint": {"os": "windows"}},
                       "runtime": {"timeoutSeconds": 900}},
        # Steel-managed proxies need a $10 paid balance on the Launch plan
        "steel": {"useProxy": {"geolocation": {"country": "US"}}},
        # CAPTCHA solving is on by default; residential needs Developer or higher
        "browserbase": {"proxies": [{"type": "browserbase", "geolocation": {"country": "US"}}]},
        # stealth adds Kernel's default ISP proxy and its CAPTCHA solver
        "kernel": {"stealth": True},
        # Free plan: the CAPTCHA solver needs Starter ($50) and, when requested
        # on Free, changed nothing (2026-11-12: nowsecure, g2, tripadvisor gave
        # the same result with it on and off), so it is left off and the
        # benchmark page says so
        "anchor": {"session": {"proxy": {"active": True, "country_code": "us"}},
                   "browser": {"captcha_solver": {"active": False},
                               "extra_stealth": {"active": True}}},
        "hyperbrowser": {"useProxy": True, "proxyCountry": "US", "solveCaptchas": True,
                         "useStealth": True},
        # stealth is always on, CAPTCHA solving on by default
        "browseruse": {"proxyCountryCode": "us"},
        "notte": {"proxies": [{"type": "notte", "country": "us"}], "solve_captchas": True},
    },
    "speed": {
        "floppydata": {"proxy": {"type": "datacenter", "location": {"countryCode": "US"}},
                       "browser": {"fingerprint": {"os": "windows"}},
                       "runtime": {"timeoutSeconds": 900}},
        "steel": {},
        "browserbase": {},
        "kernel": {},
        "anchor": {"session": {"proxy": {"active": False}}},
        "hyperbrowser": {},
        "browseruse": {"proxyCountryCode": None},
        "notte": {"proxies": False},
    },
}

# Providers whose session is created and released over REST. "id"/"cdp" are
# dotted paths in the create response; "release" is (method, path, body).
# "profile" makes the saved identity a warm start reuses: "make" creates it
# (or None when it is just a name), "attach" puts it into a create body.
REST = {
    "browserbase": dict(
        base="https://api.browserbase.com", create="/v1/sessions",
        auth=lambda e: {"X-BB-API-Key": e["api_key"]}, id="id", cdp="connectUrl",
        release=("POST", "/v1/sessions/{id}", {"status": "REQUEST_RELEASE"}),
        profile=dict(make=("POST", "/v1/contexts", {}, "id"),
                     attach=lambda b, p, keep: b.setdefault("browserSettings", {}).update(
                         context={"id": p, "persist": keep}))),
    "kernel": dict(
        base="https://api.onkernel.com", create="/browsers",
        auth=lambda e: {"Authorization": "Bearer %s" % e["api_key"]},
        id="session_id", cdp="cdp_ws_url",
        release=("DELETE", "/browsers/{id}", None),  # closing CDP does not end it
        profile=dict(make=("POST", "/profiles", {"name": "cbbench-warm"}, "name"),
                     attach=lambda b, p, keep: b.update(profile=dict(
                         {"name": p}, **({"save_changes": True} if keep else {}))))),
    "anchor": dict(
        base="https://api.anchorbrowser.io", create="/v1/sessions",
        auth=lambda e: {"anchor-api-key": e["api_key"]}, id="data.id", cdp="data.cdp_url",
        release=("DELETE", "/v1/sessions/{id}", None)),
    "hyperbrowser": dict(
        base="https://api.hyperbrowser.ai", create="/api/session",
        auth=lambda e: {"x-api-key": e["api_key"]}, id="id", cdp="wsEndpoint",
        release=("PUT", "/api/session/{id}/stop", None),
        profile=dict(make=("POST", "/api/profile", {"name": "cbbench-warm"}, "id"),
                     attach=lambda b, p, keep: b.update(profile={"id": p, "persistChanges": keep}))),
    "browseruse": dict(
        base="https://api.browser-use.com", create="/api/v4/browsers",
        auth=lambda e: {"X-Browser-Use-API-Key": e["api_key"]}, id="id", cdp="cdpUrl",
        get="/api/v4/browsers/{id}",  # cdpUrl may come null at first
        release=("PATCH", "/api/v4/browsers/{id}", {"action": "stop"}),  # disconnect does not stop it
        profile=dict(make=("POST", "/api/v4/profiles", {"name": "cbbench-warm"}, "id"),
                     attach=lambda b, p, keep: b.update(profileId=p))),
    "notte": dict(
        base="https://api.notte.cc", create="/sessions/start",
        auth=lambda e: {"Authorization": "Bearer %s" % e["api_key"]},
        id="session_id", cdp="cdp_url",
        release=("DELETE", "/sessions/{id}/stop", None)),
}

# Providers reached by connecting to a WebSocket URL: the session starts on
# connect and ends when the connection closes. Lightpanda's own engine (not
# Chromium) has no residential proxy, so "real" is its US datacenter pool.
WS = {
    "browserless": dict(
        speed="wss://production-sfo.browserless.io/chromium?token={token}",
        real="wss://production-sfo.browserless.io/stealth?token={token}"
             "&proxy=residential&proxyCountry=us&solveCaptchas=true"),
    # euwest: on 2026-11-12 the uswest endpoint returned empty pages (status 0,
    # ERR_EMPTY_RESPONSE in Chrome mode) while euwest loaded them; recheck
    # uswest before the full run
    "lightpanda": dict(
        speed="wss://euwest.cloud.lightpanda.io/ws?token={token}&browser=lightpanda",
        real="wss://euwest.cloud.lightpanda.io/ws?token={token}&browser=lightpanda"
             "&proxy=datacenter&country=us"),
}
DEFAULT_BODY = {"floppydata": BODIES["real"]["floppydata"]}  # kept for setup

# GoLogin: profiles made over its API are capped (about 300 a day on new
# accounts, 100 on the Professional plan) and an HTTP 429 permanently
# invalidates the API token. The runner paces its calls, deletes every profile
# it makes right after use and stops before this many creations a day.
# the cap is per account but every server counts on its own: with three
# servers each one runs with CBBENCH_GL_CAP=80
GL_DAILY_CAP = int(os.environ.get("CBBENCH_GL_CAP", 250))
GL_MIN_GAP = 1.0

SESSION_FIELDS = ["run_id", "ts_utc", "server", "provider", "session_id", "ok",
                  "create_ms", "connect_ms", "release_ms"]
LIFE_FIELDS = SESSION_FIELDS + ["start", "concurrency", "goto_ms", "total_ms", "status", "url", "err"]
FP_FIELDS = SESSION_FIELDS + ["ip", "country", "colo", "tz", "tz_match", "webrtc_ips",
                              "webrtc_leak", "webdriver", "headless", "ua", "platform",
                              "languages", "hw", "mem", "screen", "plugins", "webgl",
                              "brands", "err"]
TG_FIELDS = SESSION_FIELDS + ["site", "url", "verdict", "marker", "vendors", "status", "title",
                              "final_url", "text_len", "load_ms", "kb", "screenshot", "err"]

# Which anti-bot product a site runs, from the cookies it sets and the headers of
# its main response. A site can show several (Cloudflare CDN + DataDome).
VENDOR_COOKIES = [
    ("datadome", "datadome"), ("akamai", "_abck"), ("akamai", "bm_sz"),
    ("perimeterx", "_px"), ("imperva", "incap_ses"), ("imperva", "visid_incap"),
    ("imperva", "reese84"), ("kasada", "kp_uidz"), ("cloudflare", "__cf_bm"),
    ("cloudflare", "cf_clearance"), ("aws-waf", "aws-waf-token"),
]
VENDOR_HEADERS = [("cloudflare", "cf-ray"), ("datadome", "x-datadome"),
                  ("kasada", "x-kpsdk-ct"), ("akamai", "akamai-grn")]

# Challenge and block pages. Title markers always count; page markers only on
# pages with little text, because normal pages embed Turnstile or reCAPTCHA in
# forms. The verdict is preliminary: screenshots are kept for a final check.
TITLE_MARKERS = [
    ("cloudflare", "just a moment"), ("cloudflare", "attention required"),
    ("akamai", "access denied"), ("imperva", "pardon our interruption"),
    ("generic", "are you a robot"), ("generic", "request unsuccessful"),
]
PAGE_MARKERS = [
    ("cloudflare", "challenges.cloudflare.com"), ("cloudflare", "cf-turnstile"),
    ("datadome", "captcha-delivery.com"), ("perimeterx", "px-captcha"),
    ("perimeterx", "press & hold"), ("imperva", "_incapsula_resource"),
    ("akamai", "reference #"), ("recaptcha", "g-recaptcha"),
    ("hcaptcha", "hcaptcha.com"), ("generic", "verify you are human"),
    ("cloudflare", "ray id:"), ("generic", "humans only"),
]
SHORT_PAGE = 3000

FINGERPRINT_JS = r"""
async () => {
  const n = navigator, out = {};
  out.webdriver = n.webdriver;
  out.ua = n.userAgent;
  out.platform = n.platform;
  out.languages = (n.languages || []).join(",");
  out.hw = n.hardwareConcurrency;
  out.mem = n.deviceMemory ?? null;
  out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.screen = screen.width + "x" + screen.height;
  out.plugins = n.plugins ? n.plugins.length : 0;
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    const d = gl.getExtension("WEBGL_debug_renderer_info");
    out.webgl = d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  } catch (e) { out.webgl = "error"; }
  out.brands = n.userAgentData ? n.userAgentData.brands.map(b => b.brand).join("|") : "";
  out.webrtc = await new Promise(done => {
    const ips = new Set();
    let pc;
    try { pc = new RTCPeerConnection({iceServers: [{urls: "stun:stun.l.google.com:19302"}]}); }
    catch (e) { return done("no_rtc"); }
    pc.createDataChannel("x");
    pc.onicecandidate = ev => {
      if (!ev.candidate) return;
      const m = ev.candidate.candidate.match(/(\d{1,3}(?:\.\d{1,3}){3})/);
      if (m) ips.add(m[1]);
    };
    pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => {});
    setTimeout(() => { try { pc.close(); } catch (e) {} done([...ips].join(",")); }, 4000);
  });
  return out;
}
"""


# ----------------------------------------------------------------- config
def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("No providers.json yet. Run:  python3 cbbench.py setup")
    with open(CONFIG) as f:
        return json.load(f)["providers"]


def save_config(cfg):
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(CONFIG, 0o600)


def cmd_setup(args):
    cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {"providers": {}}
    print("Enter provider keys. Press Enter to keep the saved value or skip.")
    for key, spec in PROVIDERS.items():
        if args.providers and key not in args.providers:
            continue
        cur = cfg["providers"].get(key, {})
        print("\n%s" % spec["label"])
        entry = dict(cur)
        for name, secret in spec["fields"]:
            state = "saved" if cur.get(name) else "empty"
            if secret:
                val = getpass.getpass("  %s (hidden while typing) [%s]: " % (name, state)).strip()
            else:
                val = input("  %s (optional) [%s]: " % (name, cur.get(name) or "empty")).strip()
            if val:
                entry[name] = val
        if entry.get(spec["fields"][0][0]):
            entry.setdefault("create_body", json.loads(json.dumps(DEFAULT_BODY.get(key, {}))))
            cfg["providers"][key] = entry
    save_config(cfg)
    print("\nSaved providers: %s" % (", ".join(sorted(cfg["providers"])) or "none"))
    print("File %s is readable only by the current user." % CONFIG)
    print("Session options (proxy, country, stealth) go into each provider's \"create_body\".")


def pick_providers(cfg, arg):
    keys = arg.split(",") if arg else sorted(cfg)
    for k in keys:
        if k not in cfg:
            sys.exit("Unknown provider: %s (saved: %s)" % (k, ", ".join(sorted(cfg))))
    return keys


# ---------------------------------------------------------------- network
def redact(s):
    return re.sub(r"(token|apiKey|api_key|jwt)=[^&\s\"']+", r"\1=REDACTED", s)


def short_err(e):
    msg = " ".join(str(e).split())
    return redact("%s: %s" % (type(e).__name__, msg))[:160]


def http_json(method, url, headers, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    # Cloudflare in front of the provider APIs answers Python's default
    # "Python-urllib" User-Agent with error 1010, so the runner names itself.
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "cbbench/0.1 (cloud browser benchmark)", **headers}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()[:4000].decode(errors="replace")
        # Floppydata errors: {"error": {"code", "message", "details": {"failureStage"}}}
        # other providers put code and message at the top level (Steel, Browserbase)
        try:
            doc = json.loads(raw)
            err = doc.get("error") if isinstance(doc.get("error"), dict) else doc
            stage = (err.get("details") or {}).get("failureStage")
            text = " / ".join(str(x) for x in (err.get("code"), stage, err.get("message")) if x)
        except (ValueError, AttributeError):
            text = ""
        raise RuntimeError("http %d %s" % (e.code, text or " ".join(raw[:200].split()))) from None
    return json.loads(raw) if raw.strip() else {}


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1)


_gl_last = [0.0]
_gl_lock = threading.Lock()  # concurrent sessions call from several threads
_state_lock = threading.Lock()
_gl_timing = threading.local()


def gl_call(method, path, entry, body=None, timeout=60):
    """Every GoLogin API call goes through here: calls start at most one per
    GL_MIN_GAP seconds, and a 429 stops the whole run - retrying would not
    help, the token is already gone. The call's own duration, without the
    wait for its turn, is left in _gl_timing.ms."""
    with _gl_lock:
        wait = _gl_last[0] + GL_MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _gl_last[0] = time.monotonic()
    t = time.perf_counter()
    try:
        return http_json(method, GL_API + path,
                         {"Authorization": "Bearer %s" % entry["token"]}, body, timeout)
    except RuntimeError as e:
        if str(e).startswith("http 429"):
            print("GoLogin answered 429: its docs say the API token is now invalid. "
                  "Stopping. Create a new token and run setup gologin.", flush=True)
            os._exit(3)
        raise
    finally:
        _gl_timing.ms = ms(t)


def gl_create_profile(entry, name, timeout):
    """A new Windows profile without proxy (speed tests), counted against the
    daily cap kept in data/state.json."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _state_lock:  # count before creating, so parallel sessions cannot pass the cap
        state = load_state()
        made = state.setdefault("gologin_created", {})
        if made.get(day, 0) >= GL_DAILY_CAP:
            raise RuntimeError("GoLogin daily profile cap reached (%d), try tomorrow" % GL_DAILY_CAP)
        made[day] = made.get(day, 0) + 1
        save_state(state)
    p = gl_call("POST", "/browser/custom", entry,
                {"name": name, "os": "win", "proxy": {"mode": "none"}}, timeout)
    return p["id"]


def gl_delete_profiles(entry, ids, timeout):
    if ids:
        gl_call("DELETE", "/browser", entry, {"profilesToDelete": list(ids)}, timeout)


def body_for(key, entry, test):
    return entry.get("custom_body") or BODIES[test].get(key, {})


def create_session(key, entry, timeout, test="real", start="cold"):
    """Blocking. Returns {id, cdp, api_release}; cdp is the CDP WebSocket URL.
    api_release False: closing the CDP connection ends the session (Floppydata
    with keepAlive false, the default; GoLogin). GoLogin's session starts when
    the CDP connection opens; its create phase is the profile creation of a
    cold start and empty otherwise, so the browser start-up lands in connect.

    start "cold": a new identity every session. "warm": the saved identity
    from data/state.json (see prime_warm), as a returning customer runs it."""
    body = dict(body_for(key, entry, test))
    warm = load_state().get("warm", {}).get("%s/%s" % (key, test)) if start == "warm" else None
    if start == "warm" and not warm:
        raise RuntimeError("no warm profile yet - run lifecycle with --start warm, it primes one")
    if key == "floppydata":
        if warm:  # settings.id cannot be combined with browser or proxy fields
            body = {"settings": {"id": warm}, "runtime": body.get("runtime", {})}
        h = {"X-Api-Key": entry["api_key"]}
        s = http_json("POST", FD_API, h, body, timeout)
        try:
            deadline = time.monotonic() + timeout
            while not s.get("connectUrl"):
                if s.get("status") in ("failed", "stopped", "timedOut"):
                    raise RuntimeError("session %s" % s.get("status"))
                if time.monotonic() > deadline:
                    raise TimeoutError("no connectUrl after %ss" % timeout)
                time.sleep(0.25)
                s = http_json("GET", "%s/%s" % (FD_API, s["id"]), h, None, timeout)
        except Exception:
            release_session(key, entry, {"id": s.get("id")}, timeout)
            raise
        return dict(id=s["id"], cdp=s["connectUrl"], api_release=False, stop_on_failure=True,
                    settings_id=(s.get("settings") or {}).get("id"))
    if key == "steel":
        if warm:
            body["profileId"] = warm
        s = http_json("POST", STEEL_API, {"steel-api-key": entry["api_key"]}, body, timeout)
        cdp = "%s?%s" % (STEEL_CONNECT, urllib.parse.urlencode(
            {"apiKey": entry["api_key"], "sessionId": s["id"]}))
        return dict(id=s["id"], cdp=cdp, api_release=True, profile_id=s.get("profileId"))
    if key == "gologin":
        sess = dict(api_release=False, no_create=True)
        if test == "real":
            # the user's own profile with GoLogin's US residential proxy; its
            # cookies are cleared on connect so earlier visits do not help
            profile, sess["clear_state"] = entry.get("profile_id"), True
        elif warm:
            profile = warm
        else:
            profile = gl_create_profile(entry, "cbbench-cold", timeout)
            # the API call alone; our pacing queue is not GoLogin's time
            sess.update(no_create=False, delete_profile=profile, create_ms=_gl_timing.ms)
        q = {"token": entry["token"]}
        if profile:
            q["profile"] = profile
        sess.update(id=profile or "", cdp="%s?%s" % (GL_CONNECT, urllib.parse.urlencode(q)))
        return sess
    if key in REST:
        spec = REST[key]
        if warm:
            spec["profile"]["attach"](body, warm, False)
        s = http_json("POST", spec["base"] + spec["create"], spec["auth"](entry), body, timeout)
        sid, cdp = dig(s, spec["id"]), dig(s, spec["cdp"])
        try:
            deadline = time.monotonic() + timeout
            while not cdp:
                if "get" not in spec or time.monotonic() > deadline:
                    raise RuntimeError("no CDP URL in the create response")
                time.sleep(0.25)
                cdp = dig(http_json("GET", spec["base"] + spec["get"].format(id=sid),
                                    spec["auth"](entry), None, timeout), spec["cdp"])
        except Exception:
            release_session(key, entry, {"id": sid}, timeout)
            raise
        return dict(id=sid, cdp=cdp, api_release=True)
    if key in WS:
        if warm:
            raise RuntimeError("warm start is not supported for %s yet" % key)
        return dict(id="", cdp=WS[key][test].format(token=urllib.parse.quote(entry["token"])),
                    api_release=False, no_create=True, fresh_page=key == "lightpanda")
    raise ValueError("no adapter for %s" % key)


def dig(obj, path):
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def release_session(key, entry, sess, timeout):
    if not sess.get("id"):
        return
    if key == "floppydata":
        http_json("POST", "%s/%s/stop" % (FD_API, sess["id"]),
                  {"X-Api-Key": entry["api_key"]}, {}, timeout)
    elif key == "steel":
        http_json("POST", "%s/%s/release" % (STEEL_API, sess["id"]),
                  {"steel-api-key": entry["api_key"]}, {}, timeout)
    elif key in REST:
        spec = REST[key]
        method, path, body = spec["release"]
        http_json(method, spec["base"] + path.format(id=sess["id"]), spec["auth"](entry),
                  body, timeout)


def prime_warm(key, entry, test, timeout):
    """Make the saved identity a warm start reuses: one ordinary session whose
    profile is kept. Floppydata keeps settings.id, Steel a persisted profile,
    GoLogin a profile made over its API (launched once so it is not cold)."""
    from playwright.sync_api import sync_playwright
    state = load_state()
    slot = "%s/%s" % (key, test)
    if state.get("warm", {}).get(slot):
        return
    if key == "gologin":
        saved = gl_create_profile(entry, "cbbench-warm", timeout)
        sess = dict(cdp="%s?%s" % (GL_CONNECT, urllib.parse.urlencode(
            {"token": entry["token"], "profile": saved})))
    elif key in REST:
        spec = REST[key]
        if "profile" not in spec:
            raise RuntimeError("warm start is not supported for %s yet" % key)
        method, path, pbody, field = spec["profile"]["make"]
        try:
            saved = dig(http_json(method, spec["base"] + path, spec["auth"](entry), pbody,
                                  timeout), field)
        except RuntimeError as e:
            if not str(e).startswith("http 409"):  # a name-based profile may already exist
                raise
            saved = pbody.get("name")
        body = dict(body_for(key, entry, test))
        spec["profile"]["attach"](body, saved, True)
        sess = create_session(key, dict(entry, custom_body=body), timeout, test)
    elif key in WS:
        raise RuntimeError("warm start is not supported for %s yet" % key)
    else:
        if key == "steel":
            entry = dict(entry, custom_body=dict(body_for(key, entry, test), persistProfile=True))
        if key == "floppydata":
            # without persist the settings are "burner" and gone when the session
            # ends; persist.ttlSeconds (60-86400, in the OpenAPI spec, not the docs)
            # keeps settings.id reusable
            entry = dict(entry, custom_body=dict(body_for(key, entry, test), persist={"ttlSeconds": 86400}))
        sess = create_session(key, entry, timeout, test)
        saved = sess.get("settings_id") or sess.get("profile_id")
    with sync_playwright() as pw:
        pw.chromium.connect_over_cdp(sess["cdp"], timeout=timeout * 1000).close()
    if key == "floppydata":
        # only an explicit stop saves the settings (closing CDP does not), which
        # is also why cold sessions never fill the account's 20 saved settings
        release_session(key, entry, sess, timeout)
    if key in REST:
        # these providers save the profile when the session is released, and
        # their docs say to give the upload a few seconds before reusing it
        release_session(key, entry, sess, timeout)
        time.sleep(10)
    if key == "steel":
        release_session(key, entry, sess, timeout)
        # the profile uploads after release; a session started meanwhile gets 409
        deadline = time.monotonic() + 120
        while True:
            try:
                probe = http_json("POST", STEEL_API, {"steel-api-key": entry["api_key"]},
                                  dict(body_for(key, entry, test), profileId=saved), timeout)
                release_session(key, entry, probe, timeout)
                break
            except RuntimeError as e:
                if not str(e).startswith("http 409") or time.monotonic() > deadline:
                    raise
                time.sleep(3)
    if not saved:
        raise RuntimeError("%s returned no reusable profile id" % key)
    state = load_state()
    state.setdefault("warm", {})[slot] = saved
    save_state(state)
    print("[%s] warm profile for %s ready: %s" % (utcnow(), slot, saved))


def fd_balance_bytes(entry, timeout):
    """Rotating proxy traffic left on the Floppydata account, in bytes. The API
    has no traffic per session, so a run's traffic is the balance difference."""
    b = http_json("GET", FD_BALANCE, {"X-Api-Key": entry["api_key"]}, None, timeout)
    total = ((b.get("proxy") or {}).get("rotating") or {}).get("total") or {}
    return (total.get("traffic") or {}).get("availableBytes")


# ---------------------------------------------------------------- session
def utcnow():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def ms(t):
    return round((time.perf_counter() - t) * 1000, 1)


def base_row(run_id, server, provider, **extra):
    row = dict(run_id=run_id, ts_utc=utcnow(), server=server, provider=provider, ok=0,
               create_ms="", connect_ms="", release_ms="", err="")
    row.update(extra)
    return row


async def run_session(pw, key, entry, args, work, row, test="real", start="cold"):
    """create -> connect -> work(page, row) -> release, timing each phase.
    The session is released even when a phase fails, but only a clean run
    gets a release time and ok=1."""
    sess = browser = None
    closed = False
    phase = "create"
    try:
        t = time.perf_counter()
        sess = await asyncio.to_thread(create_session, key, entry, args.timeout, test, start)
        row["session_id"] = sess.get("id", "")  # lets the provider look a session up
        if not sess.get("no_create"):
            row["create_ms"] = sess.get("create_ms") or ms(t)
        phase = "connect"
        t = time.perf_counter()
        browser = await pw.chromium.connect_over_cdp(sess["cdp"], timeout=args.timeout * 1000)
        if sess.get("fresh_page"):  # Lightpanda has no usable start-up page
            ctx = await browser.new_context()
            page = await ctx.new_page()
        else:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        row["connect_ms"] = ms(t)
        if sess.get("clear_state"):  # untimed: a fixed profile starts each visit clean
            await ctx.clear_cookies()
        phase = "work"
        await work(page, row)
    except Exception as e:  # noqa: BLE001 - every failure is a data point
        row["err"] = "%s: %s" % (phase, short_err(e))
    finally:
        try:
            if sess and sess.get("api_release"):
                if browser:
                    await browser.close()  # disconnect only, not timed
                t = time.perf_counter()
                await asyncio.to_thread(release_session, key, entry, sess, args.timeout)
            elif browser:
                t = time.perf_counter()
                await browser.close()  # Floppydata, GoLogin: this ends the session
                closed = True
            if not row["err"] and (sess.get("api_release") or closed):
                row["release_ms"] = ms(t)
                row["ok"] = 1
        except Exception as e:  # noqa: BLE001
            if not row["err"]:
                row["err"] = "release: %s" % short_err(e)
        # Floppydata session never connected, or the close failed: stop it so it
        # does not hold a slot until its timeout. Cleanup only, not timed.
        if sess and sess.get("stop_on_failure") and not closed:
            try:
                await asyncio.to_thread(release_session, key, entry, sess, args.timeout)
            except Exception:  # noqa: BLE001
                pass
        # GoLogin cold start: the profile made for this session is deleted at once,
        # so the plan's profile limit is never reached. Cleanup only, not timed.
        if sess and sess.get("delete_profile"):
            try:
                await asyncio.to_thread(gl_delete_profiles, entry, [sess["delete_profile"]],
                                        args.timeout)
            except Exception as e:  # noqa: BLE001
                print("could not delete GoLogin profile %s: %s" % (
                    sess["delete_profile"], short_err(e)), flush=True)
    return row


async def count_bytes(page):
    """Bytes received over the network, from CDP Network events. Proxy traffic
    is what Floppydata bills, so this feeds $ per 1000 pages."""
    box = {"bytes": 0}
    try:
        cdp = await page.context.new_cdp_session(page)
        cdp.on("Network.loadingFinished", lambda e: box.__setitem__(
            "bytes", box["bytes"] + int(e.get("encodedDataLength", 0))))
        await cdp.send("Network.enable")
    except Exception:  # noqa: BLE001 - a provider may not allow it
        box["bytes"] = None
    return box


# ------------------------------------------------------------------ tests
def make_goto(args):
    async def work(page, row):
        t = time.perf_counter()
        resp = await page.goto(args.url, wait_until="domcontentloaded",
                               timeout=args.timeout * 1000)
        row["goto_ms"] = ms(t)
        row["status"] = resp.status if resp else ""
        # a page that never arrived is a failure, not a fast load (Lightpanda
        # uswest once answered every navigation with status 0 and no body)
        if not resp or not resp.status:
            raise RuntimeError("empty response (status %s)" % row["status"])
    return work


def zones_by_country():
    out = defaultdict(set)
    for path in ("/usr/share/zoneinfo/zone1970.tab", "/usr/share/zoneinfo/zone.tab"):
        if not os.path.exists(path):
            continue
        for line in open(path):
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            for cc in parts[0].split(","):
                out[cc.lower()].add(parts[2].strip())
    return out


ZONES = zones_by_country()
ZONEINFO = "/usr/share/zoneinfo"


def tz_in_country(tz, cc):
    """True when the browser timezone belongs to the IP's country. Browsers may
    report legacy names (America/Indianapolis) that zone.tab lists under their
    current name (America/Indiana/Indianapolis); an alias has the same zone
    data, so compare the files."""
    zones = ZONES.get(cc, ())
    if tz in zones:
        return True
    try:
        data = open(os.path.join(ZONEINFO, tz), "rb").read()
    except (OSError, TypeError):
        return False
    for z in zones:
        try:
            if open(os.path.join(ZONEINFO, z), "rb").read() == data:
                return True
        except OSError:
            continue
    return False


def is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


async def fingerprint_work(page, row):
    await page.goto(TRACE_URL, wait_until="domcontentloaded", timeout=60000)
    text = await page.evaluate("document.body.innerText")
    trace = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)
    fp = await page.evaluate(FINGERPRINT_JS)
    ip, cc = trace.get("ip", ""), trace.get("loc", "").lower()
    rtc = fp.get("webrtc") or ""
    leaked = [x for x in rtc.split(",") if x and is_public(x) and x != ip]
    row.update(
        ip=ip, country=cc, colo=trace.get("colo", ""), tz=fp.get("tz", ""),
        tz_match=("" if not ZONES or not cc else int(tz_in_country(fp.get("tz"), cc))),
        webrtc_ips=rtc, webrtc_leak=int(bool(leaked)),
        webdriver=fp.get("webdriver"),
        headless=int("headless" in (fp.get("ua", "") + fp.get("brands", "")).lower()),
        **{k: fp.get(k) for k in ("ua", "platform", "languages", "hw", "mem", "screen",
                                  "plugins", "webgl", "brands")})
    if not ip:
        raise RuntimeError("no IP in Cloudflare trace")


def find_marker(title, html, text_len):
    t = title.lower()
    for kind, m in TITLE_MARKERS:
        if m in t:
            return "%s: %s" % (kind, m)
    if text_len < SHORT_PAGE:
        h = html.lower()
        for kind, m in PAGE_MARKERS:
            if m in h:
                return "%s: %s" % (kind, m)
    return ""


async def detect_vendors(page, resp):
    found = set()
    try:
        names = [c["name"].lower() for c in await page.context.cookies()]
    except Exception:  # noqa: BLE001
        names = []
    for vendor, prefix in VENDOR_COOKIES:
        if any(n.startswith(prefix) for n in names):
            found.add(vendor)
    headers = {k.lower() for k in (resp.headers if resp else {})}
    for vendor, h in VENDOR_HEADERS:
        if h in headers:
            found.add(vendor)
    return "|".join(sorted(found))


def verdict_for(marker, status, text_len, title=""):
    """blocked: a challenge marker, a block title, or an error status on a page
    with little text (Indeed "Blocked", StockX 403 "Error", eBay 403). The
    status is the first response's, so a challenge that cleared into a real
    page (long text) still counts as ok."""
    blocked_status = str(status) in ("401", "403", "429") and int(text_len or 0) < SHORT_PAGE
    if marker or blocked_status or "blocked" in title.lower():
        return "blocked"
    return "ok" if int(text_len or 0) >= 200 else "empty"


def make_target(args, site, url, shot_dir):
    async def work(page, row):
        box = await count_bytes(page)
        t = time.perf_counter()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        row["status"] = resp.status if resp else ""
        # challenge pages often clear themselves, and some sites (Amazon) first
        # answer with an empty interstitial that reloads into the real page -
        # give both args.settle seconds
        deadline = time.perf_counter() + args.settle
        title = html = ""
        text_len = 0
        while True:
            try:
                title = await page.title()
                html = await page.content()
                text_len = await page.evaluate(
                    "document.body ? document.body.innerText.length : 0")
            except Exception:  # noqa: BLE001 - page is navigating
                pass
            marker = find_marker(title, html, text_len)
            if (not marker and text_len >= 200) or time.perf_counter() >= deadline:
                break
            await asyncio.sleep(2)
        row["load_ms"] = ms(t)
        row["vendors"] = await detect_vendors(page, resp)
        row.update(title=" ".join(title.split())[:120], final_url=page.url,
                   text_len=text_len, marker=marker,
                   verdict=verdict_for(marker, row["status"], text_len, title),
                   kb="" if box["bytes"] is None else round(box["bytes"] / 1024, 1))
        name = "%s_%s_%s.png" % (row["provider"], site, time.strftime("%H%M%S"))
        try:
            await page.screenshot(path=os.path.join(shot_dir, name), timeout=15000)
            row["screenshot"] = os.path.relpath(os.path.join(shot_dir, name), HOME)
        except Exception:  # noqa: BLE001
            pass
    return work


def load_targets(path):
    out = []
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if line:
            url = line if "://" in line else "https://" + line
            site = urllib.parse.urlparse(url).hostname.removeprefix("www.")
            out.append((site, url))
    return out


# --------------------------------------------------------------- commands
def open_csv(test, server, run_id, fields):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "%s_%s_%s.csv" % (test, server, run_id))
    f = open(path, "w", newline="")
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    return f, w, path


def need_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("Playwright is missing. Install it:  pip install playwright")
    return async_playwright


async def cmd_check(args):
    cfg = load_config()
    async with need_playwright()() as pw:
        for key in pick_providers(cfg, args.providers):
            row = await run_session(pw, key, cfg[key], args, make_goto(args),
                                    base_row("check", "check", key), args.test)
            print("%-11s ok=%d  create=%s  connect=%s  goto=%s  release=%s ms  status=%s  %s" % (
                key, row["ok"], row["create_ms"] or "-", row["connect_ms"] or "-",
                row.get("goto_ms", "-"), row["release_ms"] or "-", row.get("status", "-"),
                row["err"]))


async def cmd_lifecycle(args):
    cfg = load_config()
    keys = pick_providers(cfg, args.providers)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M", time.gmtime())
    c = args.concurrency
    if args.start == "warm":
        if c > 1:
            sys.exit("A warm start reuses one saved profile, which cannot run twice at "
                     "once: use --concurrency 1")
        ready = []
        for key in keys:  # a provider whose saved profile cannot be made is left out
            try:
                await asyncio.to_thread(prime_warm, key, cfg[key], "speed", args.timeout)
                ready.append(key)
            except Exception as e:  # noqa: BLE001
                print("[%s] %s left out of the warm run: %s" % (utcnow(), key, short_err(e)))
        keys = ready
    f, w, path = open_csv("lifecycle", args.server, run_id, LIFE_FIELDS)
    work = make_goto(args)
    print("[%s] lifecycle run %s on '%s': %d providers x %d sessions, %s start, concurrency %d, url %s"
          % (utcnow(), run_id, args.server, len(keys), args.n, args.start, c, args.url))

    async def one(pw, key, row):
        return await run_session(pw, key, cfg[key], args, work, row, "speed", args.start)

    async with need_playwright()() as pw:
        for key in keys:  # warm-up, not written
            for _ in range(args.warmup):
                await one(pw, key, base_row("", "", key))
        # rounds of c sessions; providers in a new random order every round, so
        # all of them are measured at nearly the same time
        for r in range(math.ceil(args.n / c)):
            order = keys[:]
            random.shuffle(order)
            for key in order:
                batch = [base_row(run_id, args.server, key, concurrency=c, start=args.start,
                                  url=args.url, goto_ms="", status="")
                         for _ in range(min(c, args.n - r * c))]
                await asyncio.gather(*(one(pw, key, row) for row in batch))
                for row in batch:
                    if row["ok"]:
                        row["total_ms"] = round(sum(float(row[k] or 0) for k in (
                            "create_ms", "connect_ms", "goto_ms", "release_ms")), 1)
                w.writerows(batch)
                f.flush()
                print("[%s] round %d  %-11s %d/%d ok" % (
                    utcnow(), r + 1, key, sum(x["ok"] for x in batch), len(batch)))
    f.close()
    print("[%s] finished, raw data in %s" % (utcnow(), path))


async def cmd_fingerprint(args):
    cfg = load_config()
    keys = pick_providers(cfg, args.providers)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M", time.gmtime())
    f, w, path = open_csv("fingerprint", args.server, run_id, FP_FIELDS)
    async with need_playwright()() as pw:
        for i in range(args.n):
            order = keys[:]
            random.shuffle(order)
            for key in order:
                row = await run_session(pw, key, cfg[key], args, fingerprint_work,
                                        base_row(run_id, args.server, key))
                w.writerow(row)
                f.flush()
                print("[%s] %d/%d %-11s ok=%d ip_country=%s tz=%s tz_match=%s webrtc_leak=%s %s" % (
                    utcnow(), i + 1, args.n, key, row["ok"], row.get("country", ""),
                    row.get("tz", ""), row.get("tz_match", ""), row.get("webrtc_leak", ""),
                    row["err"]))
    f.close()
    print("[%s] finished, raw data in %s" % (utcnow(), path))


async def cmd_targets(args):
    cfg = load_config()
    keys = pick_providers(cfg, args.providers)
    sites = load_targets(args.targets)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M", time.gmtime())
    shot_dir = os.path.join(SHOTS, run_id)
    os.makedirs(shot_dir, exist_ok=True)
    f, w, path = open_csv("targets", args.server, run_id, TG_FIELDS)
    print("[%s] targets run %s on '%s': %d providers x %d sites x %d visits"
          % (utcnow(), run_id, args.server, len(keys), len(sites), args.repeat))
    before = None
    if "floppydata" in keys:
        before = fd_balance_bytes(cfg["floppydata"], args.timeout)
        print("[%s] Floppydata traffic balance: %s bytes. Do not use this account "
              "elsewhere until the run ends." % (utcnow(), before))
    ok_pages = visits = 0
    async with need_playwright()() as pw:
        for rep in range(args.repeat):
            random.shuffle(sites)
            # every provider visits the same site at the same moment, one
            # session each, so a run takes as long as the slowest provider
            # per site instead of the sum of all of them
            for site, url in sites:
                rows = [base_row(run_id, args.server, key, site=site, url=url,
                                 verdict="error", marker="", status="", screenshot="")
                        for key in keys]
                await asyncio.gather(*(
                    run_session(pw, key, cfg[key], args, make_target(args, site, url, shot_dir), row)
                    for key, row in zip(keys, rows)))
                w.writerows(rows)
                f.flush()
                for key, row in zip(keys, rows):
                    if key == "floppydata":
                        visits += 1
                        ok_pages += row["verdict"] == "ok"
                print("[%s] visit %d  %-24s ok: %s" % (
                    utcnow(), rep + 1, site,
                    ", ".join(k for k, r in zip(keys, rows) if r["verdict"] == "ok") or "-"))
    f.close()
    print("[%s] finished, raw data in %s, screenshots in %s" % (utcnow(), path, shot_dir))
    if before is not None:
        print("[%s] waiting %ds for the balance to settle" % (utcnow(), args.balance_wait))
        time.sleep(args.balance_wait)
        after = fd_balance_bytes(cfg["floppydata"], args.timeout)
        rec = dict(run_id=run_id, server=args.server, before_bytes=before, after_bytes=after,
                   used_bytes=before - after, visits=visits, ok_pages=ok_pages)
        with open(os.path.join(DATA, "balance_%s_%s.json" % (args.server, run_id)), "w") as bf:
            json.dump(rec, bf, indent=1)
        print_balance(rec)


async def cmd_baseline(args):
    """Plain headless Chrome on this machine, no proxy, no stealth: the same
    visit as `targets`. A page that blocks it really is protected; one that lets
    it through says nothing about cloud browsers and is left out of the list."""
    sites = load_targets(args.targets)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M", time.gmtime())
    shot_dir = os.path.join(SHOTS, "baseline-" + run_id)
    os.makedirs(shot_dir, exist_ok=True)
    f, w, path = open_csv("baseline", args.server, run_id, TG_FIELDS)
    print("[%s] baseline run %s: %d pages, local Chrome (%s)"
          % (utcnow(), run_id, len(sites), "headed" if args.headed else "headless"))
    rows = []
    async with need_playwright()() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=not args.headed)
        for site, url in sites:
            row = base_row(run_id, args.server, "baseline", site=site, url=url,
                           verdict="error", marker="", vendors="", status="", screenshot="")
            ctx = await browser.new_context()  # fresh cookies for every page
            try:
                page = await ctx.new_page()
                await make_target(args, site, url, shot_dir)(page, row)
                row["ok"] = 1
            except Exception as e:  # noqa: BLE001
                row["err"] = "work: %s" % short_err(e)
            finally:
                await ctx.close()
            w.writerow(row)
            f.flush()
            rows.append(row)
            print("[%s] %-28s %-7s %-14s %s %s" % (utcnow(), site, row["verdict"],
                                                   row["vendors"] or "-", row["marker"], row["err"]))
        await browser.close()
    f.close()
    # blocked or empty pages first: those are the candidates for targets.txt
    rank = {"blocked": 0, "empty": 1, "error": 2, "ok": 3}
    out = os.path.join(HOME, "targets_baseline.txt")
    with open(out, "w") as tf:
        tf.write("# Baseline %s: plain headless Chrome, no proxy. Verdict and anti-bot vendors per page.\n"
                 "# Pages blocked for the baseline are the real tests; check screenshots before use.\n"
                 % run_id)
        for r in sorted(rows, key=lambda r: (rank.get(r["verdict"], 9), r["vendors"], r["site"])):
            tf.write("%-70s # %-7s %s %s\n" % (r["url"], r["verdict"], r["vendors"] or "-",
                                               r["marker"] or r["err"][:40]))
    counts = Counter(r["verdict"] for r in rows)
    print("[%s] finished: %s. Raw data %s, ranked list %s" % (utcnow(), dict(counts), path, out))


def print_balance(rec):
    gb = rec["used_bytes"] / 1024 ** 3
    per_k = gb / rec["ok_pages"] * 1000 if rec["ok_pages"] else float("nan")
    print("  Floppydata run %s | server %s: %.3f GB for %d visits, %d got through -> "
          "%.2f GB = $%.2f per 1000 successful pages at $1/GB" % (
              rec["run_id"], rec["server"], gb, rec["visits"], rec["ok_pages"], per_k, per_k))


# ------------------------------------------------------------------ stats
def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def pctl(sorted_vals, q):
    n = len(sorted_vals)
    return sorted_vals[min(n - 1, max(0, math.ceil(q * n) - 1))]


def fmt_rate(k, n):
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return "%.1f%% (%.1f-%.1f)" % (100.0 * k / n, max(0.0, 100 * lo), min(100.0, 100 * hi))


def fmt_dist(vals):
    vals = sorted(float(v) for v in vals if v not in ("", None))
    if not vals:
        return "-"
    return "%.0f / %.0f / %.0f / %.0f" % (statistics.mean(vals), statistics.median(vals),
                                          pctl(vals, 0.95), pctl(vals, 0.99))


def load_rows(test):
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "%s_*.csv" % test))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                r["ok"] = int(r["ok"])
                rows.append(r)
    return rows


def errors_line(rs):
    errs = Counter(r["err"][:70] for r in rs if not r["ok"])
    if errs:
        print("    errors: " + "; ".join("%s x%d" % kv for kv in errs.most_common(4)))


def cmd_summary(args):
    rows = load_rows("lifecycle")
    if rows:
        print("\nLIFECYCLE (ms: avg / median / p95 / p99, successful sessions only)")
        groups = defaultdict(list)
        for r in rows:
            groups[(r["server"], r["provider"], r.get("start") or "cold",
                    r["concurrency"])].append(r)
        for (server, prov, start, c), rs in sorted(groups.items()):
            good = [r for r in rs if r["ok"]]
            print("\n  %s | server %s | %s start | concurrency %s | sessions %d | reliability %s" % (
                prov, server, start, c, len(rs), fmt_rate(len(good), len(rs))))
            for ph in ("create_ms", "connect_ms", "goto_ms", "release_ms", "total_ms"):
                print("    %-11s %s" % (ph[:-3], fmt_dist(r[ph] for r in good)))
            errors_line(rs)

    rows = load_rows("fingerprint")
    if rows:
        print("\nFINGERPRINT (share of successful sessions)")
        groups = defaultdict(list)
        for r in rows:
            groups[(r["server"], r["provider"])].append(r)
        for (server, prov), rs in sorted(groups.items()):
            good = [r for r in rs if r["ok"]]
            k = len(good)
            if ZONES:  # recomputed, so older rows get today's alias handling
                for r in good:
                    r["tz_match"] = str(int(tz_in_country(r["tz"], r["country"])))

            def share(col, val="1"):
                return fmt_rate(sum(1 for r in good if str(r[col]) == val), k)
            print("\n  %s | server %s | sessions %d | ok %s" % (
                prov, server, len(rs), fmt_rate(k, len(rs))))
            print("    countries: %s" % dict(Counter(r["country"] for r in good)))
            print("    timezone matches IP country: %s" % share("tz_match"))
            print("    WebRTC leaks another public IP: %s" % share("webrtc_leak"))
            print("    navigator.webdriver true: %s" % share("webdriver", "True"))
            print("    headless in UA: %s" % share("headless"))
            print("    WebGL renderers: %d distinct, unique IPs: %d" % (
                len(set(r["webgl"] for r in good)), len(set(r["ip"] for r in good))))
            errors_line(rs)

    rows = load_rows("targets")
    if rows:
        print("\nTARGETS (preliminary verdicts - check the screenshots)")
        groups = defaultdict(list)
        for r in rows:
            groups[(r["server"], r["provider"])].append(r)
        for (server, prov), rs in sorted(groups.items()):
            got = [r for r in rs if r["verdict"] == "ok"]
            by_site = defaultdict(list)
            for r in rs:
                by_site[r["site"]].append(r["verdict"] == "ok")
            never = sorted(s for s, v in by_site.items() if not any(v))
            print("\n  %s | server %s | visits %d | got through %s | blocked on every visit: %d of %d sites" % (
                prov, server, len(rs), fmt_rate(len(got), len(rs)), len(never), len(by_site)))
            print("    page load ms (avg/median/p95/p99): %s" % fmt_dist(r["load_ms"] for r in got))
            print("    KB per page (avg/median/p95/p99): %s" % fmt_dist(r["kb"] for r in got))
            print("    markers: %s" % dict(Counter(r["marker"] for r in rs if r["marker"])))
            if never:
                print("    never got through: %s" % ", ".join(never))
            errors_line(rs)

    balances = sorted(glob.glob(os.path.join(DATA, "balance_*.json")))
    if balances:
        print("\nFLOPPYDATA TRAFFIC (balance before/after each targets run)")
        for path in balances:
            print_balance(json.load(open(path)))
    if not glob.glob(os.path.join(DATA, "*.csv")):
        sys.exit("No data found in %s" % DATA)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s, n_default):
        s.add_argument("--providers", default="", help="comma separated, default: all saved")
        s.add_argument("--timeout", type=float, default=60, help="seconds per phase")
        if n_default:
            s.add_argument("--server", required=True, help="label of this machine, e.g. us, de, sg")
            s.add_argument("--n", type=int, default=n_default, help="sessions per provider")
            s.add_argument("--run-id", default="")

    s = sub.add_parser("setup")
    s.add_argument("providers", nargs="*", help="only these providers, e.g. steel gologin")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("check")
    common(s, 0)
    s.add_argument("--url", default=DEFAULT_URL)
    s.add_argument("--test", choices=["speed", "real"], default="speed",
                   help="which session setup to try: speed (no proxies where possible) "
                        "or real (each provider's US residential proxy)")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("lifecycle")
    common(s, 100)
    s.add_argument("--url", default=DEFAULT_URL)
    s.add_argument("--concurrency", type=int, default=1)
    s.add_argument("--start", choices=["cold", "warm"], default="cold",
                   help="cold: a new profile every session; warm: one saved profile reused")
    s.add_argument("--warmup", type=int, default=3, help="unrecorded sessions per provider")
    s.set_defaults(fn=cmd_lifecycle)

    s = sub.add_parser("fingerprint")
    common(s, 20)
    s.set_defaults(fn=cmd_fingerprint)

    s = sub.add_parser("targets")
    common(s, 0)
    s.add_argument("--server", required=True)
    s.add_argument("--run-id", default="")
    s.add_argument("--targets", default=TARGETS)
    s.add_argument("--repeat", type=int, default=2, help="visits per site and provider")
    s.add_argument("--settle", type=float, default=30,
                   help="seconds to wait for a challenge page to clear")
    s.add_argument("--balance-wait", type=int, default=60,
                   help="seconds before reading the Floppydata balance after the run")
    s.set_defaults(fn=cmd_targets)

    s = sub.add_parser("baseline")
    s.add_argument("--server", default="local")
    s.add_argument("--run-id", default="")
    s.add_argument("--timeout", type=float, default=60, help="seconds per page")
    s.add_argument("--targets", default=os.path.join(HOME, "candidates.txt"))
    s.add_argument("--settle", type=float, default=30)
    s.add_argument("--headed", action="store_true", help="show the browser window")
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("summary")
    s.set_defaults(fn=cmd_summary)

    args = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up in redirected logs
    if asyncio.iscoroutinefunction(args.fn):
        asyncio.run(args.fn(args))
    else:
        args.fn(args)


if __name__ == "__main__":
    main()
