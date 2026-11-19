"""Keep a website's page cache warm.

Every few minutes it checks a few canary pages (edge cache and server copy, desktop and phone). If they went cold,
it loads every page once as a desktop browser and once as a phone, gently, 2 at a time.
Which site and pages come from the WARM_CONFIG secret, so nothing about the site is in this code or its logs.
If the firewall answers 403 with `cf-mitigated: challenge` (or 429), it stops at once.

Usage: python warm.py          check, warm only if cold
       python warm.py --force  warm even if the canaries look warm
       python warm.py --loop N keep checking every 5 minutes for N minutes
"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

CFG = json.loads(os.environ.get("WARM_CONFIG") or open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "private", "warm-config.json"), encoding="utf-8").read())
SITE = CFG["site"].rstrip("/")
FIRST = CFG.get("first", ["/"])          # warmed before everything else
SITEMAPS = tuple(CFG.get("sitemaps", ("post", "page")))
LINKS = CFG.get("links", [])             # exact links with query strings that are cached separately
CANARY_EXTRA = CFG.get("canary", "/")    # a third edge canary page

DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/130.0.0.0 Safari/537.36")
MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.5 Mobile/15E148 Safari/604.1")
HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br"}
# Never load cart/checkout/account pages: a cookie-free visit there can get an empty cart cached.
NEVER = r"checkout|/cart|add-to-cart|my-account|order-received|thank-you|/offer/|/upsell|wp-admin|wp-login|/wp-content/"
SKIP = re.compile(NEVER + r"|\?")
LINK_SKIP = re.compile(NEVER)
SAVED_MARK = "Debug: cached@"  # the server's page-cache mark at the end of a saved copy
WORKERS = 2
PAUSE = 1.5  # seconds each worker waits between requests


def extra_links():
    links = []
    for link in LINKS:
        link = link.strip()
        if not link.startswith("/") or LINK_SKIP.search(link):
            continue
        links.append(SITE + link)
        # /Free/?x=1 and /join?x=1 redirect to the lowercase, slash-ended address: warm that page too.
        p, q = (link.split("?", 1) + [""])[:2]
        clean = p.lower() if p.endswith("/") else p.lower() + "/"
        if clean != p and not re.search(r"\.[A-Za-z0-9]{1,8}$|^/(wp-|freebie/)", p, re.I):
            links.append(SITE + clean + ("?" + q if q else ""))
    return links


class Blocked(Exception):
    pass


stop = threading.Event()


def get(url, ua):
    """One cookie-free GET -> (code, edge status, seconds, saved). Raises Blocked on a challenge or rate limit."""
    if stop.is_set():
        return None
    t0 = time.perf_counter()
    r = requests.get(url, headers={**HEADERS, "User-Agent": ua}, timeout=60, allow_redirects=False)
    took = time.perf_counter() - t0
    if (r.status_code == 403 and "challenge" in r.headers.get("cf-mitigated", "").lower()) or r.status_code == 429:
        stop.set()
        raise Blocked(f"HTTP {r.status_code}")
    edge = r.headers.get("cf-cache-status", "-").upper()
    saved = None
    if r.status_code == 200 and edge != "HIT":
        try:
            saved = SAVED_MARK in r.text[-4000:]
        except Exception:
            saved = None
    return r.status_code, edge, took, saved


def fresh(url):
    """The same page with a never-seen query value: the edge can't answer it, the server cache ignores it."""
    fresh.n += 1
    return url + ("&" if "?" in url else "?") + "vid=w%d%04d" % (int(time.time()), fresh.n % 10000)


fresh.n = 0


def sitemap_urls():
    idx = requests.get(f"{SITE}/sitemap_index.xml", headers={"User-Agent": DESKTOP}, timeout=60).text
    maps = [m for m in re.findall(r"<loc>([^<]+)</loc>", idx)
            if re.search(r"/(%s)-sitemap\d*\.xml$" % "|".join(SITEMAPS), m)]
    urls = []
    for m in maps:
        xml = requests.get(m, headers={"User-Agent": DESKTOP}, timeout=60).text
        urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if not u.startswith(f"{SITE}/wp-content/")]
    return urls


def canaries():
    posts = re.findall(r"<loc>(%s/blog/[^<]+)</loc>" % re.escape(SITE),
                       requests.get(f"{SITE}/post-sitemap.xml", headers={"User-Agent": DESKTOP}, timeout=60).text)
    blog = posts[0] if posts else f"{SITE}/"
    mid = posts[len(posts) // 2] if posts else f"{SITE}/"
    return [f"{SITE}/", blog, SITE + CANARY_EXTRA], [f"{SITE}/", blog, mid]


def summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def check_and_warm(force):
    stop.clear()
    t_start = time.time()
    edge_pages, server_pages = canaries()
    devices = (("desktop", DESKTOP), ("phone", MOBILE))

    edge_cold = {}
    for name, ua in devices:
        n = 0
        for i, u in enumerate(edge_pages, 1):
            code, status, took, _ = get(u, ua)
            n += status != "HIT"
            print(f"edge   {name:7} canary {i} {status:8} {took:5.2f}s")
            time.sleep(0.5)
        edge_cold[name] = n >= 2
    server_cold = {}
    for name, ua in devices:
        n = 0
        for i, u in enumerate(server_pages, 1):
            code, status, took, saved = get(fresh(u), ua)
            n += saved is False
            word = "saved copy" if saved else ("REBUILT" if saved is False else f"{code} {status}")
            print(f"server {name:7} canary {i} {word:10} {took:5.2f}s")
            time.sleep(0.5)
        server_cold[name] = n >= 2
    cold_parts = [f"edge {d}" for d in edge_cold if edge_cold[d]] + [f"server {d}" for d in server_cold if server_cold[d]]
    if not cold_parts and not force:
        summary("Warm (edge + server, desktop + phone). Nothing to do.")
        return 0

    urls, seen = [], set()
    links = extra_links()
    keep = set(links)
    for u in [SITE + p for p in FIRST] + links + sitemap_urls():
        u = u.strip()
        if u in seen or (u not in keep and SKIP.search(u[len(SITE):])):
            continue
        seen.add(u)
        urls.append(u)
    jobs = [(u, name, ua) for u in urls for name, ua in devices]
    print(f"cold ({', '.join(cold_parts) or 'forced'}): warming {len(urls)} pages x desktop+phone")

    results, extra, lock = [], [], threading.Lock()

    def work(job):
        if stop.is_set():
            return
        u, name, ua = job
        try:
            res = get(u, ua)
        except requests.RequestException as e:
            res = ("ERR", type(e).__name__, 0.0, None)
        if not res:
            return
        with lock:
            results.append((u, name, *res))
        time.sleep(PAUSE)
        # Edge answered, so the server wasn't asked: if the server looked cold, make it save a copy too.
        if res[0] == 200 and res[1] == "HIT" and (server_cold[name] or force) and not stop.is_set() and not u.endswith(".txt"):
            try:
                r2 = get(fresh(u), ua)
            except requests.RequestException as e:
                r2 = ("ERR", type(e).__name__, 0.0, None)
            if r2:
                with lock:
                    extra.append((u, name, *r2))
            time.sleep(PAUSE)

    blocked = None
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(work, j) for j in jobs]
        for f in futures:
            try:
                f.result()
            except Blocked as e:
                blocked = blocked or e
    mins = (time.time() - t_start) / 60

    if blocked:
        summary(f"STOPPED: firewall challenge ({blocked}). Warmed {len(results)} of {len(jobs)} first. Not retrying.")
        return 1

    ok = [r for r in results if r[2] == 200]
    bad = [r for r in results if r[2] != 200]
    summary(f"{'Forced warm' if force else 'Was cold (' + ', '.join(cold_parts) + '), re-warmed'}: "
            f"{len(ok)} page loads ({len(urls)} pages x desktop + phone) + {len(extra)} server loads in {mins:.1f} min.")
    for name, _ in devices:
        mine = [r for r in ok if r[1] == name]
        hit = sum(r[3] == "HIT" for r in mine)
        reached = [r for r in mine if r[3] != "HIT"]
        sv = sum(r[5] is True for r in reached)
        rb = sum(r[5] is False for r in reached)
        ex = [r for r in extra if r[1] == name and r[2] == 200]
        summary(f"- {name}: edge had {hit}; {len(reached)} went to the server ({sv} from its copy, {rb} rebuilt, "
                f"{sum(r[4] > 1.5 for r in reached)} over 1.5 s); extra server loads {len(ex)}.")
    if bad:
        codes = {}
        for r in bad:
            codes[str(r[2])] = codes.get(str(r[2]), 0) + 1
        summary(f"{len(bad)} loads were not a normal page: " + ", ".join(f"{c} x{n}" for c, n in codes.items()))
    return 0


def once(force):
    try:
        return check_and_warm(force)
    except Blocked as e:
        summary(f"STOPPED: firewall challenge on the cache check ({e}). Not retrying.")
        return 1
    except requests.RequestException as e:
        summary(f"Could not reach the site ({type(e).__name__}); will try again at the next check.")
        return 0


def switched_off():
    """True when the workflow was disabled in the Actions tab (so a running loop stops too)."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not (repo and token):
        return False
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/warm.yml",
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return r.ok and r.json().get("state") != "active"
    except requests.RequestException:
        return False


def loop(minutes):
    """Check every 5 minutes for `minutes`, then exit (the workflow starts the next run)."""
    end, failed = time.time() + minutes * 60, 0
    while time.time() < end:
        if switched_off():
            summary("Workflow was switched off: stopping.")
            return 0
        t = time.time()
        print(time.strftime("%H:%M UTC", time.gmtime()), "check")
        if once(False):
            failed = 1
            time.sleep(15 * 60)
        time.sleep(max(0, 5 * 60 - (time.time() - t)))
    return failed


if __name__ == "__main__":
    if "--loop" in sys.argv:
        sys.exit(loop(int(sys.argv[sys.argv.index("--loop") + 1])))
    sys.exit(once("--force" in sys.argv))
