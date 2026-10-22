# Vendored from ~/Projects/omarchy-plugin-ai (vendor.py): do not edit here.
"""plugin_costs: what AI tokens cost, for all of Uwe Bruckhaus's plugins: API prices by model, the cost
of token counts (in all, and split into input, output, cache read and cache write), the cost in US
dollars or in euros at the European Central Bank's daily rate, and a cost's colour against a day's limit.

Shared, vendored copy: the source is ~/Projects/omarchy-plugin-ai/plugin_costs.py; `vendor.py <plugin
folder>` copies it into a plugin together with plugin_ai.py. Python standard library only, no GTK, and
no network of its own: usd_per_eur() fetches through the function the plugin passes (its own capped
https fetcher), at most once a day, and keeps the rate in the shared state folder for every plugin.
A price update is made here once and reaches each plugin with its next update. API version in API.

Prices: a model name beginning -> (provider, input, output, cache read, cache write) in US dollars per
million tokens, the order agents' session files count tokens in; where a provider has no price of its
own for cache writes they cost what input does. From the providers' own pages on the dates in AS_OF;
where a price has tiers (long prompts, peak hours) the one most requests pay: an estimate. A model that
is not here (a local LLM's) costs nothing.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

API = 1

AS_OF = {
    "anthropic": "2026-10-23", "openai": "2026-10-23", "deepseek": "2026-10-23", "xai": "2026-10-23",
    "google": "2026-10-23",
}
SOURCES = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai": "https://developers.openai.com/api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing/",     # peak hours (off-peak: half)
    "xai": "https://docs.x.ai/docs/models",                               # prompts under 200k tokens
    "google": "https://ai.google.dev/gemini-api/docs/pricing",            # prompts up to 200k tokens
}

PRICES = {
    # Anthropic: cache writes at the 5-minute cache's price
    "claude-fable-5-1": ("anthropic", 10, 50, 0.25, 12.5),
    "claude-mythos-5-1": ("anthropic", 10, 50, 0.25, 12.5),
    "claude-fable-5": ("anthropic", 10, 50, 1, 12.5),
    "claude-mythos-5": ("anthropic", 10, 50, 1, 12.5),
    "claude-opus-5-5": ("anthropic", 4, 20, 0.2, 5),
    "claude-opus-5": ("anthropic", 5, 25, 0.5, 6.25),
    "claude-opus-4-8": ("anthropic", 5, 25, 0.5, 6.25),
    "claude-opus-4-7": ("anthropic", 5, 25, 0.5, 6.25),
    "claude-opus-4-6": ("anthropic", 5, 25, 0.5, 6.25),
    "claude-opus-4-5": ("anthropic", 5, 25, 0.5, 6.25),
    "claude-opus-4": ("anthropic", 15, 75, 1.5, 18.75),
    "claude-sonnet-5-5": ("anthropic", 2, 10, 0.1, 2.5),
    "claude-sonnet-5": ("anthropic", 2, 10, 0.2, 2.5),
    "claude-sonnet-4": ("anthropic", 3, 15, 0.3, 3.75),
    "claude-haiku-5-5": ("anthropic", 0.1, 0.5, 0.01, 0.125),
    "claude-haiku-4-5": ("anthropic", 1, 5, 0.1, 1.25),
    "claude-3-5-haiku": ("anthropic", 0.8, 4, 0.08, 1),
    # OpenAI: no cache write price (input's); "-" for cached input on the page: input's
    "gpt-6-astra": ("openai", 10, 50, 1, 10),
    "gpt-6.1-sol": ("openai", 2, 10, 0.1, 2),
    "gpt-6-luna": ("openai", 0.1, 0.5, 0.01, 0.1),
    "gpt-6-sol": ("openai", 2, 10, 0.2, 2),
    "gpt-5.6-sol": ("openai", 4, 20, 0.4, 4),
    "gpt-5.6-terra": ("openai", 2, 12, 0.2, 2),
    "gpt-5.6-luna": ("openai", 0.2, 1.2, 0.02, 0.2),
    "gpt-5.5-pro": ("openai", 30, 180, 30, 30),
    "gpt-5.5": ("openai", 5, 30, 0.5, 5),
    "gpt-5.4-pro": ("openai", 30, 180, 30, 30),
    "gpt-5.4-mini": ("openai", 0.75, 4.5, 0.075, 0.75),
    "gpt-5.4-nano": ("openai", 0.2, 1.25, 0.02, 0.2),
    "gpt-5.4": ("openai", 2.5, 15, 0.25, 2.5),
    "gpt-5.3-codex": ("openai", 1.75, 14, 0.175, 1.75),
    "gpt-5.2-pro": ("openai", 21, 168, 21, 21),
    "gpt-5.2": ("openai", 1.75, 14, 0.175, 1.75),
    "gpt-5.1": ("openai", 1.25, 10, 0.125, 1.25),
    "gpt-5-pro": ("openai", 15, 120, 15, 15),
    "gpt-5-mini": ("openai", 0.25, 2, 0.025, 0.25),
    "gpt-5-nano": ("openai", 0.05, 0.4, 0.005, 0.05),
    "gpt-5": ("openai", 1.25, 10, 0.125, 1.25),
    "gpt-4.1-mini": ("openai", 0.4, 1.6, 0.1, 0.4),
    "gpt-4.1-nano": ("openai", 0.1, 0.4, 0.025, 0.1),
    "gpt-4.1": ("openai", 2, 8, 0.5, 2),
    "gpt-4o-mini": ("openai", 0.15, 0.6, 0.075, 0.15),
    "gpt-4o": ("openai", 2.5, 10, 1.25, 2.5),
    "o4-mini": ("openai", 1.1, 4.4, 0.275, 1.1),
    "o3-mini": ("openai", 1.1, 4.4, 0.55, 1.1),
    "o3-pro": ("openai", 20, 80, 20, 20),
    "o3": ("openai", 2, 8, 0.5, 2),
    "o1-pro": ("openai", 150, 600, 150, 150),
    "o1": ("openai", 15, 60, 7.5, 15),
    # DeepSeek: input = cache miss, cache read = cache hit; peak hours (off-peak is half)
    "deepseek-flash": ("deepseek", 0.3, 1.2, 0.006, 0.3),
    "deepseek-v4-flash": ("deepseek", 0.3, 1.2, 0.006, 0.3),       # retired name, billed as flash
    "deepseek-v4-pro": ("deepseek", 1.32, 3.96, 0.044, 1.32),
    # xAI: prompts under 200k tokens
    "grok-4.7": ("xai", 2, 6, 0.5, 2),
    "grok-4.6": ("xai", 2, 6, 0.5, 2),
    "grok-4.5": ("xai", 2, 6, 0.3, 2),
    "grok-4.3": ("xai", 1.25, 2.5, 0.2, 1.25),
    "grok-4.20": ("xai", 1.25, 2.5, 0.2, 1.25),
    "grok-build-0.1": ("xai", 1, 2, 0.2, 1),
    # Google: prompts up to 200k tokens; text input
    "gemini-4-argon": ("google", 2, 10, 0.1, 2),                    # $4/$20 from 2026-12-01
    "gemini-3.8-flash": ("google", 0.75, 3.75, 0.075, 0.75),        # doubles on 2027-01-01
    "gemini-3.6-flash": ("google", 0.75, 3.75, 0.075, 0.75),
    "gemini-3.5-flash-lite": ("google", 0.3, 2.5, 0.03, 0.3),
    "gemini-3.1-flash-lite": ("google", 0.25, 1.5, 0.025, 0.25),
    "gemini-3.1-pro": ("google", 2, 12, 0.2, 2),
    "gemini-3-flash": ("google", 0.5, 3, 0.05, 0.5),
    "gemini-2.5-pro": ("google", 1.25, 10, 0.125, 1.25),
    "gemini-2.5-flash-lite": ("google", 0.1, 0.4, 0.01, 0.1),
    "gemini-2.5-flash": ("google", 0.3, 2.5, 0.03, 0.3),
}

KINDS = ("input", "output", "cache read", "cache write")


def price_for(model: str) -> tuple | None:
    """(provider, input, output, cache read, cache write) of a model, by its longest matching name
    beginning; a provider's prefix ("anthropic/", "openai/") and case do not matter. None: not here."""
    name = str(model or "").strip().lower().rpartition("/")[2]
    match = max((k for k in PRICES if name.startswith(k)), key=len, default=None)
    return PRICES[match] if match is not None else None


def _counts(counts) -> list:
    return [n if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 else 0
            for n in (list(counts or ()) + [0, 0, 0, 0])[:4]]


def parts(tokens: dict) -> dict:
    """{model: (provider, [input, output, cache read, cache write] in US dollars)} of the models that
    have a price ({model: [the four token counts]} in)."""
    found = {}
    for model, counts in (tokens or {}).items():
        price = price_for(model)
        if price is not None:
            found[model] = (price[0], [n * p / 1_000_000 for n, p in zip(_counts(counts), price[1:])])
    return found


def priced(tokens: dict) -> float | None:
    """{model: [input, output, cache read, cache write]} in US dollars; None when no model has a price
    (a local LLM's tokens cost nothing and leave it None)."""
    found = parts(tokens)
    return sum(sum(money) for _provider, money in found.values()) if found else None


# --- currency, text and colour ------------------------------------------------------------------

LOW_SHARE = 0.75            # of a day's limit: from here a cost is yellow, over it red


def in_currency(usd: float, currency: str = "usd", usd_per_eur: float | None = None) -> tuple[float, str]:
    """(amount, currency) to show: euros when asked and a rate is known, else US dollars."""
    if currency == "eur" and usd_per_eur:
        return usd / usd_per_eur, "eur"
    return usd, "usd"


def cost_text(usd: float, currency: str = "usd", usd_per_eur: float | None = None) -> str:
    """$3.40, $123, <$0.01; in euros 3.03 €, 110 €, <0.01 €."""
    amount, shown = in_currency(usd, currency, usd_per_eur)
    number = "<0.01" if amount < 0.01 else f"{amount:,.0f}" if amount >= 100 else f"{amount:,.2f}"
    return f"{number} €" if shown == "eur" else ("<$0.01" if number == "<0.01" else f"${number}")


def cost_colour(usd: float, limit: float, currency: str = "usd", usd_per_eur: float | None = None) -> str:
    """"green", "yellow" (from LOW_SHARE of the limit) or "red" (over it); limit in the shown currency."""
    amount, _shown = in_currency(usd, currency, usd_per_eur)
    share = amount / limit if limit > 0 else 0.0
    return "red" if share > 1 else "yellow" if share >= LOW_SHARE else "green"


# --- the euro rate --------------------------------------------------------------------------------

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"   # about 1.5 KiB
RATE_EVERY = 24 * 3600      # seconds between fetches
RATE_MAX_BYTES = 64 * 1024
RATE_FILE = Path.home() / ".local/state/omarchy-plugin-ai/exchange-rate.json"
_RATE_RE = re.compile(r"""currency=['"]USD['"]\s+rate=['"]([0-9]{1,2}\.[0-9]{1,6})['"]""")
_DAY_RE = re.compile(r"""time=['"]([0-9]{4}-[0-9]{2}-[0-9]{2})['"]""")


def parse_ecb(body: bytes) -> tuple[float, str] | None:
    """(US dollars per euro, the rate's day) from the ECB's daily file, or None. Only the US dollar
    rate is taken, by a pattern (no XML parser), and only a plausible one."""
    text = bytes(body or b"")[:RATE_MAX_BYTES].decode("utf-8", "replace")
    found = _RATE_RE.search(text)
    if not found:
        return None
    rate = float(found.group(1))
    day = _DAY_RE.search(text)
    return (rate, day.group(1) if day else "") if 0.5 < rate < 3 else None


def _kept(path: Path) -> dict:
    try:
        data = json.loads(path.read_text()[:4096])
    except (OSError, ValueError):
        return {}
    rate = data.get("usd_per_eur") if isinstance(data, dict) else None
    ok = isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.5 < rate < 3
    return data if ok and isinstance(data.get("fetched"), (int, float)) else {}


def _keep(path: Path, data: dict):
    """Owner-only, written whole (a temporary file, then renamed)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def usd_per_eur(fetch=None, path: Path | None = None, now: float | None = None) -> tuple[float, str] | None:
    """(US dollars per euro, its day): the kept rate while it is less than a day old; else, with
    fetch(url, max_bytes=, timeout=) -> bytes (the plugin's own capped https fetcher), a fresh one,
    kept; else the last kept one; None when there has never been one. May block for the fetch: call it
    from a background thread."""
    path = path or RATE_FILE
    now = time.time() if now is None else now
    kept = _kept(path)
    if kept and now - kept["fetched"] < RATE_EVERY:
        return kept["usd_per_eur"], str(kept.get("day", ""))
    found = None
    if fetch is not None:
        try:
            found = parse_ecb(fetch(ECB_URL, max_bytes=RATE_MAX_BYTES, timeout=10))
        except Exception:      # no network, a refused answer: the last rate kept
            found = None
    if found is None:
        return (kept["usd_per_eur"], str(kept.get("day", ""))) if kept else None
    _keep(path, {"usd_per_eur": found[0], "day": found[1], "fetched": now})
    return found
