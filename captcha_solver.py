"""
captcha_solver.py
------------------
Shared helper used by all three scrapers (Playwright / Selenium / Puppeteer).

Detection runs after EVERY page navigation in the main loop of all three
scrapers, regardless of what URL was requested — this is deliberate, not
scoped to any one page, because which challenge a visitor meets depends on
the exit and on what the address has been doing.

WHAT THIS MODULE IS FOR ON TIKTOK
---------------------------------
**Different things on different routes, and that is the measurement.**

On the profile, embed, video and Ad Library routes, from a bare datacentre
address with no key and no proxy, TikTok rendered no captcha at all
(measured 2026-09-22). But "no challenge rendered" is NOT "no captcha
configured" (CLAUDE.md §18), and the obvious grep lies here the same way it
lies on other sites in this family: a SERVED profile page carries the word
`captcha` 25 times. Every one is configuration for TikTok's own SDK, not a
widget — which is why the bare word is not a challenge marker.

TikTok Shop is the route where a challenge really renders: ByteDance's own
slide puzzle (`secsdk-captcha`, `captcha_verify_img_slide`). This module
does not implement a solver for it, and the engines do not try one — the
shop's measured way in is a warmed Scraping Browser profile, not a token.

The machinery below — the `turnstile.render` interception, the enterprise
task types, the variant heuristic — is family core. It is here so that a
route which starts rendering one of those challenges is met with tools that
already work.

PROVENANCE OF THE FIGURES BELOW
-------------------------------
This file is family core and carries measurements taken on SIBLING sites.
They are evidence about the SOLVER, not about TikTok, and are labelled as
such so nobody re-reads them as facts about this site:

  * foodpanda-scraper, 2026-09-16: a Cloudflare Turnstile Challenge page
    solved via the `turnstile.render` interception — all four parameters
    captured, an 837-character token in 11 seconds, $0.00145, and the page
    came back as the real site.
  * foodpanda-scraper, 2026-09-16: `RecaptchaV2EnterpriseTaskProxyless` on a
    PerimeterX denial page, ~55 seconds, $0.00299. The control matters as
    much as the result — reloading that page in the same session WITHOUT
    solving cleared it 0 of 8 times, so the token is what got in.

Two things decide whether that money buys anything anywhere:

  * **the task type.** An enterprise widget solved as ordinary v2 returns a
    token the site rejects. `CaptchaChallenge.enterprise` carries the page's
    own answer — its loader, and `window.grecaptcha.enterprise` — into
    `_v2_task_for`.
  * **the variant.** A sibling's first live attempt read a bframe with no
    `size` as v3, bought a `RecaptchaV3TaskProxyless` and got
    ERROR_CAPTCHA_UNSOLVABLE after 87 seconds. v3 renders no challenge frame
    at all, so a frame present settles it as a v2 checkbox.

Detection therefore stays BROAD while spending stays NARROW:
`--solve-captcha when-blocked` is the default and counts job links before
paying, `page_flow.SOLVES_PER_PAGE` caps a page at one purchase, and
`page_flow.STATE_POLICY` — not this file — decides which state is worth
money at all.

Flow:
  1. Both detectors run and are reconciled (see reconcile_detections) to
     decide the variant: v3, v2-invisible or v2-checkbox. The parameters
     differ per variant and are not interchangeable — v3 params sent for a
     v2-invisible widget buy a token the site rejects.
  2. The challenge goes to 2captcha's API (v2 by default, legacy v1 as a
     one-shot fallback) with the parameters for that variant.
  3. The token is injected — into `g-recaptcha-response` for reCAPTCHA, or
     into `cf-turnstile-response` plus the page's own callback for Turnstile.

Often none of this is needed. Over the Scraping Browser API,
`Captcha.setAutoSolve` can clear the challenge inside the browser before this
code gets a turn — treat `Captcha.solveFinished` as the success signal and
keep this path as the fallback. This module is the path for a browser you
launched yourself.

No other captcha vendor is integrated (per spec: no competitors).
"""

from __future__ import annotations

import base64
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests

logger = logging.getLogger("captcha_solver")


# An API key must never reach a log, and the easiest way for one to get there
# is an exception message. `requests` puts the FULL URL — query string and all
# — into the text of HTTPError and of every connection error, so any endpoint
# that takes its key as a query parameter leaks it the moment something goes
# wrong. That is not hypothetical: a 400 from the fingerprint endpoint printed
# a live key to the terminal.
#
# Everything raised or logged from this module goes through here first. The
# endpoint and the status survive, because which call failed is the useful
# half and is not the secret.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)

# A PROXY credential is the other secret this module handles, and it does not
# look like a query parameter: it rides in the userinfo of a URL
# (`scheme://login:password@host:port`). Since this module gained a path that
# takes a proxy — a vendor task bound to the solving exit — its
# errors can quote one, and an error that quotes a proxy URL prints the
# password.
#
# Found by a refusal message doing exactly that: a portless proxy was
# rejected with the whole URL in the text. The host and the port survive
# here, deliberately: WHICH exit failed is the useful half of that message
# and is not the secret.
#
# Matched GLOBALLY, not once. A masker that handles the first occurrence
# prints the password the other four times and still looks like it works.
_PROXY_CREDENTIALS_RE = re.compile(
    r"([a-z][a-z0-9+.\-]*://)[^\s/@\"']+:[^\s/@\"']+@", re.IGNORECASE)


def _redact(text) -> str:
    """Everything raised or logged from this module goes through here first.

    An EXCEPTION MESSAGE IS A LOG: `requests` puts the full URL, query string
    and all, into the text of HTTPError and of every connection error, so an
    endpoint that takes its key as a query parameter leaks it the moment
    anything goes wrong.
    """
    masked = _KEY_IN_TEXT_RE.sub(r"\1=***", str(text))
    return _PROXY_CREDENTIALS_RE.sub(r"\1***:***@", masked)

# 2captcha has two generations of solver API and both are live.
#
#   v2 (current, documented at https://2captcha.com/api-docs):
#       POST https://api.2captcha.com/createTask     {clientKey, task:{...}}
#       POST https://api.2captcha.com/getTaskResult  {clientKey, taskId}
#     JSON in, JSON out, typed task objects (RecaptchaV3TaskProxyless etc.).
#
#   v1 (legacy, still accepted):
#       POST https://2captcha.com/in.php   form-encoded, method=userrecaptcha
#       GET  https://2captcha.com/res.php  polling
#
# This module speaks v2 by default and keeps v1 as a fallback, because the
# original build was written against v1 and every live result recorded in the
# README came through it. Switch with solve_recaptcha(..., api_version="v1").
TWOCAPTCHA_API_V2 = "https://api.2captcha.com"
TWOCAPTCHA_CREATE_TASK_URL = f"{TWOCAPTCHA_API_V2}/createTask"
TWOCAPTCHA_GET_RESULT_URL = f"{TWOCAPTCHA_API_V2}/getTaskResult"
TWOCAPTCHA_BALANCE_URL = f"{TWOCAPTCHA_API_V2}/getBalance"

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"

# v2 rejects an arbitrary minScore: the documented values are these three.
V3_ALLOWED_MIN_SCORES = (0.3, 0.7, 0.9)

@dataclass
class CaptchaChallenge:
    kind: str            # "recaptcha_v3" | "recaptcha_v2_invisible" | "recaptcha_v2"
    sitekey: str
    action: str = "verify"
    page_url: str = ""
    # How the challenge was found: "html" (static markup) or "runtime"
    # (read out of the live page's reCAPTCHA client config). Recorded
    # because the two paths can disagree about the version, and the
    # runtime one is authoritative when they do.
    source: str = "html"
    # Raw `size` from the reCAPTCHA client config, when available:
    # "invisible" for v2-invisible and for v3, absent for a v2 checkbox.
    size: Optional[str] = None
    # Whether the page loads reCAPTCHA through the ENTERPRISE API rather than
    # the ordinary one. 2captcha has a separate task type for it
    # (`RecaptchaV2EnterpriseTaskProxyless`), and sending the ordinary type
    # for an enterprise widget buys a token the site rejects — so this has to
    # travel with the challenge rather than be guessed at solve time.
    #
    # Detected two ways, because they can each be present without the other:
    # the static markup loads `recaptcha/enterprise.js`, and the live page
    # exposes `window.grecaptcha.enterprise`.
    enterprise: bool = False
    # Cloudflare Turnstile only, and only on a CHALLENGE PAGE. These are
    # arguments to `turnstile.render` that the page makes once and does not
    # keep, so they exist here only if the interception script below was
    # installed before the widget loaded. A standalone Turnstile widget needs
    # none of them.
    cdata: Optional[str] = None
    pagedata: Optional[str] = None
    # The user agent 2captcha minted the token against. Cloudflare binds a
    # challenge token to it, so a run that solves with one UA and submits
    # with another is a run that pays and stays blocked.
    solved_user_agent: Optional[str] = None

    @property
    def is_turnstile(self) -> bool:
        return self.kind == "turnstile"

    @property
    def is_cloudflare_challenge(self) -> bool:
        """A Turnstile whose parameters say it guards a Challenge page."""
        return self.is_turnstile and bool(self.cdata or self.pagedata)

    @property
    def is_v3(self) -> bool:
        return self.kind == "recaptcha_v3"

    @property
    def is_invisible_v2(self) -> bool:
        return self.kind == "recaptcha_v2_invisible"


# The enterprise API's own loader. `api.js` is the ordinary one; a page that
# loads THIS is an enterprise widget however much its container looks like an
# ordinary v2 checkbox — which is exactly what foodpanda's refusal looks like.
_ENTERPRISE_LOADER_RE = re.compile(
    r"recaptcha/enterprise\.js|grecaptcha\.enterprise", re.I)


def _is_enterprise_html(html: str) -> bool:
    return bool(_ENTERPRISE_LOADER_RE.search(html or ""))


# Turnstile's own markup and the Challenge page's furniture. The sitekey is
# a `0x…` string rather than reCAPTCHA's `6L…`, and the widget's class is
# `cf-turnstile`.
_TURNSTILE_SITEKEY_RE = re.compile(
    r'(?:class=["\'][^"\']*\bcf-turnstile\b[^"\']*["\'][^>]*'
    r'data-sitekey=["\']([\w-]{8,})["\']'
    r'|data-sitekey=["\']([\w-]{8,})["\'][^>]*'
    r'class=["\'][^"\']*\bcf-turnstile\b)')
# Cloudflare's OWN furniture. `cf-turnstile` is not here: 2Captcha's
# Scraping Browser extension injects a `cf-turnstile-response` hunter into
# every page it loads, so the bare string appears on perfectly good listings
# fetched over --cdp-endpoint and does NOT appear on the real challenge
# (measured: 1 occurrence on a served page, 0 on the challenge).
#
# A standalone widget is still found, by the class-plus-sitekey pattern
# above, which the extension's script tag cannot match.
_TURNSTILE_MARKERS = ("challenges.cloudflare.com/turnstile",
                      "challenges.cloudflare.com",
                      "__cf_chl", "cf_chl_opt")


def detect_turnstile(html: str, page_url: str = "") -> Optional[CaptchaChallenge]:
    """A Cloudflare Turnstile on this page, from the STATIC markup.

    Finds a standalone widget by its sitekey. A CHALLENGE PAGE usually
    publishes no sitekey in its markup at all — the widget is rendered by
    script — so this returns a challenge with an empty sitekey when it can
    see Cloudflare's own furniture but no key. That is not a failure: it
    tells the caller a Turnstile is here and the runtime interception is the
    only way to get its parameters.

    A sitekey-less challenge is NOT solvable as it stands, and
    `turnstile_task_for` refuses to build a task from one rather than
    spending money on a request 2captcha will reject.
    """
    text = html or ""
    match = _TURNSTILE_SITEKEY_RE.search(text)
    sitekey = (match.group(1) or match.group(2)) if match else ""
    if not match and not any(m in text for m in _TURNSTILE_MARKERS):
        return None
    # `action` is left EMPTY rather than inheriting this dataclass's
    # reCAPTCHA-flavoured "verify" default. For a Turnstile that string is
    # not a placeholder the API ignores — on a Challenge page the action is
    # part of what the token is minted against, and an invented one buys a
    # token Cloudflare rejects.
    return CaptchaChallenge(kind="turnstile", sitekey=sitekey, action="",
                            page_url=page_url, source="html")


def detect_recaptcha_v3(html: str, page_url: str) -> Optional[CaptchaChallenge]:
    """Scan raw page HTML for a reCAPTCHA v3 challenge, in either of two
    real-world formats seen so far:

    1. An inline <script> calling grecaptcha.execute('SITEKEY', {action: '...'})
       directly — seen on Kohl's.
    2. A <captcha-widget> custom element carrying the config as HTML
       attributes (data-captcha-type="recaptcha" data-version="v3"
       data-sitekey="..." data-action="..."), with the actual execute()
       call happening inside a bundled JS file, never appearing as
       readable inline script text at all — confirmed live on the sibling
       farfetch-scraper repo's target, in its sign-up modal, via DevTools
       inspection. A previous version of this
       function, which only checked for format 1, reported "no captcha"
       on this exact page despite one being genuinely present — caught by
       manually inspecting the DOM, not by the detector itself, which is
       exactly the gap this format-2 check closes.
    """
    if "recaptcha" not in html.lower():
        return None

    # Format 2 first — a structured HTML attribute match is more reliable
    # than the format-1 regex when both happen to be present.
    for widget_match in re.finditer(r"<captcha-widget\b([^>]*)>", html, re.IGNORECASE):
        attrs = widget_match.group(1)
        version_match = re.search(r'data-version=["\']v(\d)["\']', attrs)
        sitekey_match = re.search(r'data-sitekey=["\']([\w-]{20,})["\']', attrs)
        if version_match and version_match.group(1) == "3" and sitekey_match:
            action_match = re.search(r'data-action=["\']([\w_]+)["\']', attrs)
            action = action_match.group(1) if action_match and action_match.group(1) != "null" else "verify"
            return CaptchaChallenge(
                kind="recaptcha_v3",
                sitekey=sitekey_match.group(1),
                action=action,
                page_url=page_url,
                enterprise=_is_enterprise_html(html),
            )

    # THE PLAIN v2 CONTAINER, which is what this site serves and what the
    # rest of this function could not see. `<div class="g-recaptcha"
    # data-sitekey="…" data-callback="…">` is the documented checkbox markup
    # and carries no `grecaptcha` identifier at all, so the bail-out below
    # used to discard it — leaving the static detector blind to the only
    # widget this repo meets, and a reader debugging from a `--dump-html`
    # capture with nothing to look at.
    #
    # Checked BEFORE the bail-out for that reason, and after the v3 shapes
    # above so a genuine v3 integration still wins.
    container = re.search(
        r'class=["\'][^"\']*\bg-recaptcha\b[^"\']*["\'][^>]*'
        r'data-sitekey=["\']([\w-]{20,})["\']'
        r"|data-sitekey=[\"']([\w-]{20,})[\"'][^>]*"
        r'class=["\'][^"\']*\bg-recaptcha\b',
        html)
    if container:
        sitekey = container.group(1) or container.group(2)
        enterprise = _is_enterprise_html(html)
        # `data-size="invisible"` is the invisible variant of the same
        # container; without it this is the checkbox.
        invisible = re.search(r'data-size=["\']invisible["\']', html) is not None
        return CaptchaChallenge(
            kind="recaptcha_v2_invisible" if invisible else "recaptcha_v2",
            sitekey=sitekey,
            page_url=page_url,
            enterprise=enterprise,
        )

    if "grecaptcha" not in html:
        return None

    exec_match = re.search(
        r"grecaptcha\.execute\(\s*['\"]([\w-]{20,})['\"]\s*,\s*\{\s*action:\s*['\"]([\w_]+)['\"]",
        html,
    )
    if exec_match:
        return CaptchaChallenge(
            kind="recaptcha_v3",
            sitekey=exec_match.group(1),
            action=exec_match.group(2),
            page_url=page_url,
            enterprise=_is_enterprise_html(html),
        )

    key_match = re.search(r"data-sitekey=['\"]([\w-]{20,})['\"]", html)
    if key_match and "grecaptcha.render" in html:
        return CaptchaChallenge(kind="recaptcha_v3",
                                sitekey=key_match.group(1),
                                page_url=page_url,
                                enterprise=_is_enterprise_html(html))

    return None


# ---------------------------------------------------------------------------
# Runtime detection (added 2026-08-24 — the static-HTML detector below no
# longer matched the sibling farfetch-scraper repo's target)
# ---------------------------------------------------------------------------
#
# What changed there: the sign-up modal used to render
#   <captcha-widget data-version="v3" data-sitekey="6Leif..." ...>
# with the whole config in HTML attributes. Verified live on 2026-08-24, that
# element is GONE. The modal now loads
#   https://recaptcha.net/recaptcha/api.js?render=explicit
# and configures the widget entirely in JavaScript, into a
#   <div id="register-captcha" class="g-recaptcha">
# container. There is no `data-sitekey`, no inline `grecaptcha.execute(...)`,
# and no `grecaptcha.render` anywhere in the served HTML — all four things
# detect_recaptcha_v3() keys off are absent. It returns None on this page
# even though a real reCAPTCHA is present and active.
#
# The sitekey is still recoverable, but only at runtime, from two places
# that exist in the live page and not in its HTML:
#   1. window.___grecaptcha_cfg.clients — the reCAPTCHA API's own client
#      registry. Property names inside it are minified and change between
#      releases, so this walks the object looking for a value shaped like a
#      sitekey (^6L[\w-]{30,}$) rather than trusting any key name.
#   2. the reCAPTCHA iframe's `k=` query parameter — a stable, documented
#      part of the widget's URL, used as a cross-check and fallback.
#
# The same client config also carries `size`, which is what tells v3 apart
# from v2-invisible. That distinction is not cosmetic: 2captcha needs
# `version=v3` + `action` + `min_score` for one and `invisible=1` for the
# other, and sending the wrong one burns balance for a token that won't
# validate.
RECAPTCHA_DISCOVERY_JS = r"""
() => {
  const out = {found: false, sitekey: null, size: null, action: null,
               enterprise: false, containerId: null, hints: []};

  const SITEKEY_RE = /^6L[\w-]{30,}$/;

  // --- 1. the reCAPTCHA client registry -----------------------------------
  // Minified property names, so match on value shape, not key name.
  try {
    const clients = (window.___grecaptcha_cfg || {}).clients || null;
    if (clients) {
      for (const id of Object.keys(clients)) {
        const seen = new Set();
        const walk = (o, depth) => {
          if (!o || depth > 4 || seen.has(o)) return;
          if (typeof o === 'object') seen.add(o);
          for (const k of Object.keys(o)) {
            let v;
            try { v = o[k]; } catch (e) { continue; }
            if (typeof v === 'string') {
              if (!out.sitekey && SITEKEY_RE.test(v)) { out.sitekey = v; out.found = true; }
              else if (v === 'invisible' || v === 'normal' || v === 'compact') out.size = out.size || v;
            } else if (v && typeof v === 'object') {
              walk(v, depth + 1);
            }
          }
        };
        walk(clients[id], 0);
      }
      if (out.sitekey) out.hints.push('sitekey from ___grecaptcha_cfg');
    }
  } catch (e) { out.hints.push('cfg walk failed: ' + e.message); }

  // --- 2. the widget iframe's k= parameter --------------------------------
  try {
    for (const f of document.querySelectorAll('iframe[src*="recaptcha"]')) {
      const m = /[?&]k=([\w-]{20,})/.exec(f.getAttribute('src') || '');
      if (m) {
        if (!out.sitekey) { out.sitekey = m[1]; out.found = true; out.hints.push('sitekey from iframe k= param'); }
        else if (out.sitekey !== m[1]) out.hints.push('iframe k= disagrees with cfg: ' + m[1]);
        break;
      }
    }
  } catch (e) { out.hints.push('iframe scan failed: ' + e.message); }

  // --- 3. legacy paths, still checked in case they come back --------------
  try {
    const w = document.querySelector('captcha-widget[data-sitekey]');
    if (w) {
      out.found = true;
      out.sitekey = out.sitekey || w.getAttribute('data-sitekey');
      const dv = w.getAttribute('data-version');
      if (dv) out.hints.push('legacy captcha-widget data-version=' + dv);
      const da = w.getAttribute('data-action');
      if (da && da !== 'null') out.action = da;
    }
    const gr = document.querySelector('[data-sitekey]');
    if (gr && !out.sitekey) {
      out.sitekey = gr.getAttribute('data-sitekey'); out.found = !!out.sitekey;
      out.hints.push('sitekey from a data-sitekey attribute');
    }
  } catch (e) { out.hints.push('legacy scan failed: ' + e.message); }

  // --- 4. context -------------------------------------------------------
  try {
    const c = document.querySelector('.g-recaptcha[id], [id*="captcha"]');
    if (c) out.containerId = c.id || null;
    out.enterprise = !!(window.grecaptcha && window.grecaptcha.enterprise);
    out.badge = !!document.querySelector('.grecaptcha-badge');
    // A bframe iframe is the interactive challenge. v3 never shows one;
    // v2-invisible does when it decides to challenge.
    out.challengeFrame = !!document.querySelector('iframe[src*="bframe"]');
    out.scripts = [...document.querySelectorAll('script[src*="recaptcha"]')]
                    .map(s => (s.getAttribute('src') || '').split('?')[0]);
    // The api.js `render` parameter is the strongest v2-vs-v3 signal there
    // is, per Google's own docs: v3 loads api.js?render=<SITE_KEY>, while
    // v2 (checkbox and invisible alike) loads api.js?render=explicit.
    out.renderParam = null;
    for (const sc of document.querySelectorAll('script[src*="recaptcha"]')) {
      const m = /[?&]render=([^&]+)/.exec(sc.getAttribute('src') || '');
      if (m) { out.renderParam = decodeURIComponent(m[1]); break; }
    }
  } catch (e) { out.hints.push('context scan failed: ' + e.message); }

  return out;
}
"""


def detect_recaptcha_in_page(evaluate, page_url: str = "") -> Optional[CaptchaChallenge]:
    """Detect a reCAPTCHA by inspecting the LIVE page, not its HTML.

    `evaluate` is a callable that runs RECAPTCHA_DISCOVERY_JS in the page and
    returns the resulting dict — i.e. `page.evaluate` under Playwright,
    `driver.execute_script` under Selenium (wrap it so the arrow function is
    invoked), or an awaited `page.evaluate` under pyppeteer.

    Version inference, and why it is deliberately conservative:
      * `size == "invisible"` PLUS an interactive bframe iframe present is
        the signature of **v2-invisible**, not v3 — v3 never renders a
        challenge frame. This is what that site looked like as of
        2026-08-24, on the same sitekey that its old markup explicitly
        labelled `data-version="v3"`.
      * `size == "invisible"` with no challenge frame is treated as v3,
        which is v3's normal appearance (badge only).
      * an interactive bframe present with NO size and NO render parameter is
        a **v2 checkbox**. v3 never renders a challenge frame, so the frame
        alone rules it out. foodpanda's PerimeterX denial page is exactly
        this shape, and before this rung existed it was classified v3 and a
        solve was bought against the wrong task type.
      * a `normal`/`compact` size is a v2 checkbox.
    When the two signals disagree the ambiguity is recorded in the returned
    challenge's `kind` and in the log, rather than silently guessing — pick
    the wrong one and 2captcha returns a token the site rejects.
    """
    try:
        info = evaluate(RECAPTCHA_DISCOVERY_JS)
    except Exception as e:  # noqa: BLE001 - any engine's evaluate can raise
        logger.debug("In-page reCAPTCHA discovery failed: %s", e)
        return None

    if not info or not info.get("found") or not info.get("sitekey"):
        return None

    size = info.get("size")
    render = info.get("renderParam")

    # Classification, strongest signal first.
    #
    # 1. api.js's `render` parameter. Per Google's docs, v3 loads
    #    api.js?render=<SITE_KEY> while v2 — checkbox and invisible alike —
    #    loads api.js?render=explicit. v3 has no `size` concept and never
    #    renders a challenge iframe at all.
    #      https://developers.google.com/recaptcha/docs/v3
    #      https://developers.google.com/recaptcha/docs/invisible
    #    So render=explicit rules v3 out outright, and render=<the sitekey>
    #    confirms it outright.
    # 2. `size`, which only exists for v2: invisible / normal / compact.
    # 3. presence of a bframe (interactive challenge) iframe — v2 only.
    if render and render != "explicit" and render == info.get("sitekey"):
        kind = "recaptcha_v3"
    elif render == "explicit":
        kind = "recaptcha_v2_invisible" if size == "invisible" else "recaptcha_v2"
    elif size in ("normal", "compact"):
        kind = "recaptcha_v2"
    elif info.get("challengeFrame") and size == "invisible":
        kind = "recaptcha_v2_invisible"
    elif info.get("challengeFrame"):
        # A bframe with NO size and NO render parameter. v3 renders no
        # challenge frame at all — it is a badge and a token — so a frame
        # being present rules v3 out whatever else is missing.
        #
        # This rung was absent, and its absence cost a real solve: foodpanda's
        # denial page (enterprise loader, no render, no size, checkbox markup,
        # bframe) fell through to the `else` below, bought a
        # RecaptchaV3TaskProxyless, and came back ERROR_CAPTCHA_UNSOLVABLE
        # after 87 seconds — the exact "wrong variant buys a rejected token"
        # failure this function's docstring warns about.
        kind = "recaptcha_v2"
    else:
        kind = "recaptcha_v3"

    hints = info.get("hints") or []
    logger.info("reCAPTCHA found at runtime: kind=%s sitekey=%s size=%s render=%s "
                "challengeFrame=%s container=%s hints=%s",
                kind, info["sitekey"], size, render, info.get("challengeFrame"),
                info.get("containerId"), "; ".join(hints))
    if info.get("enterprise"):
        logger.info("This is a reCAPTCHA ENTERPRISE widget — solving it through "
                    "2captcha's enterprise task type.")

    return CaptchaChallenge(
        kind=kind,
        sitekey=info["sitekey"],
        action=info.get("action") or "verify",
        page_url=page_url,
        source="runtime",
        size=size,
        enterprise=bool(info.get("enterprise")),
    )


def reconcile_detections(html_challenge: Optional[CaptchaChallenge],
                          runtime_challenge: Optional[CaptchaChallenge]
                          ) -> Optional[CaptchaChallenge]:
    """Pick between the two detectors when both find something.

    They can disagree on the SAME page, and on the site this was written
    against they did. A capture
    taken through the Scraping Browser on 2026-08-24 contains, simultaneously:

      * `<captcha-widget data-version="v3" data-sitekey="6Leif..."
         data-action="null">` — the site's own wrapper element, asserting v3
      * `recaptcha/api.js?render=explicit`, a client registered with
        `size: "invisible"`, and a `bframe` challenge iframe — the documented
        **v2-invisible** signature

    Both cannot be true. `data-version` is an attribute on the site's own
    component: it says what their code believes. `render=explicit` + `size` +
    the challenge frame describe the Google loader that is actually on the
    page, which is what Google enforces and therefore what 2captcha has to
    match. So the runtime reading wins, and the disagreement is logged rather
    than quietly resolved.

    Supporting detail: `data-action="null"` in that markup means there is no
    action string. v3 scores partly on the action; a real v3 integration
    passes one. An empty action fits a v2-invisible widget wearing a stale
    v3 label better than it fits working v3.

    (Also worth knowing: the same modal served to a European residential IP
    the same day had NO `<captcha-widget>` element at all — just the
    `render=explicit` loader. That site served more than one variant of this
    modal, so neither detector alone is enough.)
    """
    if runtime_challenge and not html_challenge:
        return runtime_challenge
    if html_challenge and not runtime_challenge:
        return html_challenge
    if not html_challenge and not runtime_challenge:
        return None

    if html_challenge.kind != runtime_challenge.kind:
        logger.warning(
            "Detectors disagree on this page: static markup says %s (from the "
            "site's own data-version), the live loader says %s (render/size/"
            "challenge-frame). Trusting the loader — that's what Google "
            "enforces and what 2captcha has to match.",
            html_challenge.kind, runtime_challenge.kind)
        # Keep the action if the static markup had a real one; the runtime
        # path often can't see it.
        if html_challenge.action and html_challenge.action != "verify":
            runtime_challenge.action = html_challenge.action
    return runtime_challenge


def _v2_task_for(challenge: CaptchaChallenge, min_score: float) -> dict:
    """Build the API-v2 `task` object for a challenge.

    Task type per variant, from https://2captcha.com/api-docs:
      * v3           -> RecaptchaV3TaskProxyless, with pageAction + minScore
      * v2 invisible -> RecaptchaV2TaskProxyless with isInvisible: true
      * v2 checkbox  -> RecaptchaV2TaskProxyless

    and the ENTERPRISE variants of the same, which are a different task type
    rather than a flag on these:
      * v2 enterprise -> RecaptchaV2EnterpriseTaskProxyless
      * v3 enterprise -> RecaptchaV3TaskProxyless with isEnterprise: true

    Getting that wrong is not cosmetic. An enterprise widget solved through
    the ordinary task type comes back with a token the site rejects — you pay
    and gain nothing — which is the shape of failure this whole module exists
    to avoid. `challenge.enterprise` is set from the page's own loader and
    from `window.grecaptcha.enterprise`, so the decision is the page's rather
    than ours.

    The `*Proxyless` types let 2captcha use its OWN IP pool, which is the
    right trade for reCAPTCHA: a v2/v3 token is not bound to the address that
    produced it, so the solve and the submission may leave from different
    places. The non-proxyless variants (RecaptchaV2Task) exist for the cases
    where that is not true, and they are not wired up here.
    """
    if challenge.is_turnstile:
        # A different product with a different task shape; see
        # `turnstile_task_for`, which also refuses to build one without a
        # sitekey rather than spending money on a request that cannot
        # succeed.
        return turnstile_task_for(challenge)

    if challenge.is_v3:
        # minScore is not free-form: 0.3 / 0.7 / 0.9 are the documented values.
        score = min(V3_ALLOWED_MIN_SCORES,
                    key=lambda allowed: abs(allowed - min_score))
        if score != min_score:
            logger.info("minScore %.2f is not one of %s — using %.1f.",
                        min_score, V3_ALLOWED_MIN_SCORES, score)
        task = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
            "minScore": score,
        }
        if challenge.enterprise:
            task["isEnterprise"] = True
        # v3 scores partly on the action, so send it when it's a real one.
        # "verify" is this module's placeholder for "the page didn't say".
        if challenge.action and challenge.action != "verify":
            task["pageAction"] = challenge.action
        return task

    task = {
        "type": ("RecaptchaV2EnterpriseTaskProxyless" if challenge.enterprise
                 else "RecaptchaV2TaskProxyless"),
        "websiteURL": challenge.page_url,
        "websiteKey": challenge.sitekey,
    }
    if challenge.is_invisible_v2:
        task["isInvisible"] = True
    if challenge.enterprise and challenge.action \
            and challenge.action != "verify":
        # `enterprisePayload` carries whatever the site passes to
        # `grecaptcha.enterprise.render` — most often an `s` value. Only the
        # action is available from detection here; a site that needs more
        # would need it read off the page and threaded through the same way.
        task["enterprisePayload"] = {"s": challenge.action}
    return task


def _solve_with_2captcha_v2(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 180) -> str:
    """Solve via API v2: createTask, then poll getTaskResult."""
    task = _v2_task_for(challenge, min_score)
    logger.info("createTask: %s (sitekey=%s)", task["type"], challenge.sitekey)

    created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                            json={"clientKey": api_key, "task": task},
                            timeout=30)
    created.raise_for_status()
    payload = created.json()
    if payload.get("errorId"):
        raise RuntimeError(
            f"createTask failed: {payload.get('errorCode')} — "
            f"{payload.get('errorDescription')}")
    task_id = payload["taskId"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                            json={"clientKey": api_key, "taskId": task_id},
                            timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("errorId"):
            raise RuntimeError(
                f"getTaskResult failed: {result.get('errorCode')} — "
                f"{result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            # Cloudflare binds a Challenge-page token to the user agent it
            # was solved against. Record it so a caller can match it before
            # submitting; ignoring it is how a paid-for token gets rejected.
            if solution.get("userAgent"):
                challenge.solved_user_agent = solution["userAgent"]
                if challenge.is_cloudflare_challenge:
                    logger.info(
                        "The token was minted for user agent %r — a Challenge "
                        "page checks it, so submit from a browser claiming "
                        "the same one.",
                        solution["userAgent"][:60] + "…")
            # v2 returns the same string under both names.
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            if not token:
                raise RuntimeError(f"task ready but no token in solution: {solution}")
            logger.info("2captcha solved %s in ~%ds.", challenge.kind, waited)
            return token
        # status == "processing"

    raise TimeoutError(f"2captcha did not return a token within {max_wait}s")


def get_balance(api_key: str) -> float:
    """Account balance via API v2 — handy for a preflight check."""
    r = requests.post(TWOCAPTCHA_BALANCE_URL, json={"clientKey": api_key}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("errorId"):
        raise RuntimeError(f"getBalance failed: {d.get('errorCode')}")
    return float(d["balance"])


def _solve_with_2captcha_v1(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 120) -> str:
    """Legacy API v1: submit to in.php, poll res.php.

    Kept as a fallback because every live result recorded in this project's
    README came through this path. Prefer v2 for new work.

    The parameters differ per variant and are NOT interchangeable — send v3
    parameters for a v2-invisible widget and you pay for a token the site
    then rejects:
      * v3            -> version=v3, action=..., min_score=...
      * v2 invisible  -> invisible=1, no action, no min_score
      * v2 checkbox   -> neither
    """
    payload = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }
    if challenge.is_v3:
        payload.update({"version": "v3", "action": challenge.action, "min_score": min_score})
    elif challenge.is_invisible_v2:
        payload["invisible"] = 1

    logger.info("Submitting to 2captcha as %s (sitekey=%s)", challenge.kind, challenge.sitekey)
    submit = requests.post(TWOCAPTCHA_IN_URL, data=payload, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            # The v1 result endpoint takes the key as a QUERY parameter, so a
            # connection error here would otherwise put it in the log.
            result = requests.get(TWOCAPTCHA_RES_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30).json()
        except requests.RequestException as exc:
            raise RuntimeError("2captcha polling request failed: %s"
                               % _redact(exc)) from None
        if result.get("status") == 1:
            logger.info("2captcha.com solved the reCAPTCHA v3 challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a token in time")


def solve_recaptcha(challenge: CaptchaChallenge, twocaptcha_api_key: Optional[str],
                     api_version: str = "v2", min_score: float = 0.7) -> str:
    """Public entry point: solve `challenge` through 2captcha and return the token.

    There used to be a `use_antidetect` branch here, behind a CLI flag of the
    same name, that POSTed to a hardcoded local "solve-captcha" endpoint.
    It was removed before publishing: that endpoint was a placeholder for a
    product that does not exist under that name, so the flag could not work for
    anyone who set it, and the real "the browser solves it for you" path is
    `Captcha.setAutoSolve` over the Scraping Browser API. A flag that cannot
    succeed is worse than a missing feature — it reads as an option.
    """
    if not twocaptcha_api_key:
        raise RuntimeError(
            "A captcha was detected but no 2captcha API key was given. Pass "
            "--twocaptcha-key, or set TWOCAPTCHA_KEY. Over the Scraping Browser "
            "API you may not need either: Captcha.setAutoSolve can clear it "
            "inside the browser."
        )
    if challenge.is_turnstile and api_version == "v1":
        # The legacy in.php/res.php pair has its own Turnstile method, and
        # this module does not implement it. Refusing is the honest answer:
        # falling through to the reCAPTCHA-shaped request would spend a
        # createTask on something that cannot come back right.
        raise CaptchaUnsolvable(
            "Turnstile is only implemented on the v2 API here. Drop "
            "--captcha-api v1 (v2 is the default) to solve it.")
    solver = _solve_with_2captcha_v1 if api_version == "v1" else _solve_with_2captcha_v2
    return solver(twocaptcha_api_key, challenge, min_score=min_score)




class CaptchaUnsolvable(RuntimeError):
    """2Captcha reported the challenge as unsolvable from this exit.

    Distinct from a generic failure because it calls for a DIFFERENT action:
    the documented remedy is to change the exit address, not to try again
    from the same one. The engines let their block-retry loop rotate on it
    rather than burning solves against an address the vendor has already
    refused.
    """




# 2Captcha's own words for "this will not solve from here". Kept as a set
# rather than a substring match so a new code is reported verbatim instead of
# being silently swallowed into the retry loop.
_UNSOLVABLE_CODES = frozenset((
    "ERROR_CAPTCHA_UNSOLVABLE",
    "ERROR_PROXY_CONNECT_REFUSED",
    "ERROR_PROXY_CONNECT_TIMEOUT",
    "ERROR_PROXY_READ_TIMEOUT",
    "ERROR_PROXY_BANNED",
    "ERROR_IP_BLOCKED",
))




# Installed on EVERY new document, before any of the page's own script runs.
#
# `turnstile.render(container, params)` is called once by Cloudflare's
# challenge and its `params` — which carry `cData`, `chlPageData` and
# `action` — are not kept anywhere afterwards. 2captcha needs all three to
# solve a Challenge page, so the only way to have them is to be there first.
#
# The wrapper records the parameters, keeps the page's own callback so a
# token can be handed back the way the page expects, and still calls through
# to the real `render` so the page behaves normally if no solve happens.
TURNSTILE_INTERCEPT_JS = """
(() => {
  if (window.__tsIntercepted) { return; }
  window.__tsIntercepted = true;
  window.__tsParams = null;
  window.__tsCallback = null;
  const grab = (params) => {
    try {
      window.__tsParams = {
        sitekey: params.sitekey,
        action: params.action || null,
        cData: params.cData || null,
        chlPageData: params.chlPageData || null,
        pageurl: window.location.href
      };
      if (typeof params.callback === 'function') {
        window.__tsCallback = params.callback;
      }
    } catch (e) { /* best effort, never break the page */ }
  };
  // The object may not exist yet, so watch for it rather than poll forever.
  let installed = false;
  const install = () => {
    if (installed || !window.turnstile || !window.turnstile.render) { return; }
    installed = true;
    const real = window.turnstile.render.bind(window.turnstile);
    window.turnstile.render = (container, params) => {
      grab(params || {});
      return real(container, params);
    };
  };
  const timer = setInterval(() => {
    install();
    if (installed) { clearInterval(timer); }
  }, 10);
  // Stop watching after 30s; a page that has not rendered one by then is not
  // going to, and a forever-timer in every document is its own bug.
  setTimeout(() => clearInterval(timer), 30000);
})()
"""

# Read back whatever the interception captured. Returns null when no
# Turnstile rendered, which is the ordinary case on a page that is fine.
TURNSTILE_DISCOVERY_JS = """
() => {
  if (window.__tsParams) { return window.__tsParams; }
  const el = document.querySelector('.cf-turnstile[data-sitekey], [data-sitekey][class*="cf-turnstile"]');
  if (el) {
    return {
      sitekey: el.getAttribute('data-sitekey'),
      action: el.getAttribute('data-action') || null,
      cData: null, chlPageData: null,
      pageurl: window.location.href
    };
  }
  return null;
}
"""

# Hand the token back the way Turnstile expects: the named input, the
# reCAPTCHA-compatibility input, and the page's own callback if the
# interception kept one.
TURNSTILE_INJECT_JS = """
(token) => {
  const setField = (name) => {
    let el = document.querySelector(`[name="${name}"]`);
    if (!el) {
      el = document.createElement('input');
      el.type = 'hidden';
      el.name = name;
      document.body.appendChild(el);
    }
    el.value = token;
  };
  setField('cf-turnstile-response');
  setField('g-recaptcha-response');
  let calledBack = false;
  try {
    if (typeof window.__tsCallback === 'function') {
      window.__tsCallback(token);
      calledBack = true;
    }
  } catch (e) { /* best effort */ }
  return calledBack;
}
"""


# How long to wait for Cloudflare to call `turnstile.render`. Measured: two
# navigations to the same Challenge page seconds apart, one capturing nothing
# and one capturing everything — so the render is not prompt and a single read
# is a coin toss. Six seconds at half-second steps covers it without making a
# page that has no Turnstile pay for the privilege.
TURNSTILE_WAIT_SECONDS = 6.0
TURNSTILE_POLL_SECONDS = 0.5


def wait_for_turnstile(evaluate, sleep, page_url: str = "",
                       timeout_s: float = TURNSTILE_WAIT_SECONDS
                       ) -> Optional[CaptchaChallenge]:
    """`detect_turnstile_in_page`, but give the render a chance to happen.

    Returns as soon as a challenge with a sitekey is available, so the wait
    costs nothing on a page that renders one promptly and nothing at all on a
    page that has none — the first pass already returns None and the caller
    decides whether to keep waiting.

    `sleep` takes SECONDS, matching each engine's own idle primitive rather
    than importing time here.
    """
    waited = 0.0
    best = None
    while True:
        challenge = detect_turnstile_in_page(evaluate, page_url=page_url)
        if challenge and challenge.sitekey:
            # A Challenge page needs the intercepted parameters; a standalone
            # widget does not. Keep waiting for the former, take the latter.
            if challenge.is_cloudflare_challenge or waited >= timeout_s:
                return challenge
            best = challenge
        if waited >= timeout_s:
            return best
        sleep(TURNSTILE_POLL_SECONDS)
        waited += TURNSTILE_POLL_SECONDS


def detect_turnstile_in_page(evaluate, page_url: str = "") -> Optional[CaptchaChallenge]:
    """A Turnstile read out of the LIVE page, parameters included.

    `evaluate` runs `TURNSTILE_DISCOVERY_JS` and returns its result — the
    same contract as `detect_recaptcha_in_page`, so each engine spells it in
    its own driver's dialect and no JavaScript crosses a module boundary in
    the other direction.

    This is the only path that can produce a solvable CHALLENGE PAGE, and it
    can only do so if `TURNSTILE_INTERCEPT_JS` was installed on the document
    before Cloudflare's script ran.
    """
    try:
        info = evaluate(TURNSTILE_DISCOVERY_JS)
    except Exception as e:  # noqa: BLE001 - any engine's evaluate can raise
        logger.debug("In-page Turnstile discovery failed: %s", e)
        return None
    if not info or not info.get("sitekey"):
        return None
    challenge = CaptchaChallenge(
        kind="turnstile",
        sitekey=info["sitekey"],
        action=info.get("action") or "",
        page_url=info.get("pageurl") or page_url,
        source="runtime",
        cdata=info.get("cData"),
        pagedata=info.get("chlPageData"),
    )
    logger.info("Turnstile found at runtime: sitekey=%s action=%s "
                "cData=%s chlPageData=%s (%s)",
                challenge.sitekey, challenge.action or None,
                "yes" if challenge.cdata else "no",
                "yes" if challenge.pagedata else "no",
                "Cloudflare Challenge page" if challenge.is_cloudflare_challenge
                else "standalone widget")
    return challenge


def turnstile_task_for(challenge: CaptchaChallenge) -> dict:
    """Build the 2captcha task for a Turnstile.

    `action`, `data` and `pagedata` are optional for a standalone widget and
    REQUIRED for a Challenge page — so they are sent when the interception
    captured them and omitted when it did not, rather than sent empty.
    """
    if not challenge.sitekey:
        # A Challenge page publishes no sitekey in its markup — the widget is
        # rendered by script — so a static detection can legitimately come
        # back without one. 2captcha rejects a task with an empty
        # `websiteKey`, so building it anyway would be a request that cannot
        # succeed. Refuse here instead, and say what would fix it.
        raise CaptchaUnsolvable(
            "a Turnstile was detected but no sitekey is available. On a "
            "Cloudflare Challenge page the key arrives as an argument to "
            "turnstile.render, so TURNSTILE_INTERCEPT_JS has to be installed "
            "on the document BEFORE Cloudflare's script runs — see the "
            "engines' context setup.")
    task = {
        "type": "TurnstileTaskProxyless",
        "websiteURL": challenge.page_url,
        "websiteKey": challenge.sitekey,
    }
    if challenge.action:
        task["action"] = challenge.action
    if challenge.cdata:
        task["data"] = challenge.cdata
    if challenge.pagedata:
        task["pagedata"] = challenge.pagedata
    return task


INJECT_TOKEN_JS = """
(token) => {
  let el = document.getElementById('g-recaptcha-response');
  if (!el) {
    el = document.createElement('textarea');
    el.id = 'g-recaptcha-response';
    el.name = 'g-recaptcha-response';
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      Object.values(window.___grecaptcha_cfg.clients).forEach((client) => {
        Object.values(client).forEach((prop) => {
          if (prop && typeof prop === 'object') {
            Object.values(prop).forEach((cb) => {
              if (typeof cb === 'function') { try { cb(token); } catch (e) {} }
            });
          }
        });
      });
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""


# Kept as an alias: this function used to be reCAPTCHA-v3-only, and the three
# scrapers plus both modal diagnostics import it under the old name. The
# solver now branches on challenge.kind, so the name is a misnomer — the
# alias exists so older call sites keep working rather than to encourage it.
solve_recaptcha_v3 = solve_recaptcha


# ===========================================================================
# What is deliberately NOT here
# ===========================================================================
# The sibling repo in this family carries a whole second solver for its
# site's OWN first-party image captcha ("Enter the characters you see below",
# a JPEG of distorted text and a GET form). Roughly 190 lines of it, and none
# of it is ported here, because this site has no such page.
#
# What this site does instead is refuse a HEADLESS browser. Measured
# 2026-09-10 from five different addresses, four of them residential: Akamai
# answers with HTTP 403 and a 394-byte "Access Denied" page carrying a
# reference id — no form, no image, no widget, nothing for a solver to
# answer. And the trigger is the CLIENT rather than the address: the very
# same addresses were served HTTP 200 and the full catalogue by a browser
# with a real window. So the response to a block here is `--headful` or
# `--cdp-endpoint`, not a solve and not a better proxy, and
# product_parser.detect_page_state reports it as "blocked" rather than
# "challenge" precisely so no solve is attempted and nothing is charged.
#
# The reCAPTCHA / hCaptcha / Turnstile machinery above IS kept, and that is a
# deliberate asymmetry rather than an inconsistency. Detection stays broad
# because which challenge a visitor meets depends on the exit country and on
# what the address has been doing — a narrow list is how a challenge gets
# reported as an empty page months later. A solver for a challenge this site
# has never been observed to serve is dead code; a DETECTOR for one is cheap
# insurance: a detection that fires on a page whose lots have
# already rendered guards nothing, which is why the default is
# `when-blocked` and why it counts lot links before it spends.
