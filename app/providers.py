"""Market-intelligence providers: economic calendar, news, macro series.

Every provider is an adapter with an explicit availability state. Nothing here
invents an event, a headline or a macro value: an unconfigured provider reports
UNAVAILABLE and the desk treats that as missing evidence, not as "no news".

Source research (2026-11-07, probed from this machine) is recorded in
ECONOMIC_CALENDAR_REPORT.md. In short: FRED's release dates, BLS, Financial Modeling
Prep and Trading Economics all need a (free or paid) credential or block non-browser
clients, and the Federal Reserve's own calendar is Fed-only. The one public calendar
that answered without a credential is the weekly Forex Factory JSON below, kept behind
an explicit opt-in because it is not a licensed feed.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Protocol

from . import config, db

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
STALE = "STALE"

# How old a fetched calendar snapshot may be before it is treated as stale.
CALENDAR_MAX_AGE_MINUTES = config.CALENDAR_MAX_AGE_MINUTES_DEFAULT
# News is consumed, not just fetched: a feed that has not been ingested recently is
# STALE, and a headline older than the window is not presented as current.
NEWS_MAX_AGE_MINUTES = 15
NEWS_MAX_PUBLISHED_AGE_HOURS = 24
IMPACTS = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
# Impact vocabulary of the public weekly feed, mapped onto the desk's own scale. A
# "Holiday" row (a bank holiday or a daylight-saving note) is not a release and must
# not black out trading; it is stored as LOW so it is still visible.
FF_IMPACT_MAP = {"High": "HIGH", "Medium": "MEDIUM", "Low": "LOW", "Holiday": "LOW"}


class ProviderUnavailable(RuntimeError):
    pass


def _parse_utc(value: str) -> datetime:
    """Accept ISO-8601 with or without an offset; anything naive is treated as UTC."""
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


class CalendarProvider(Protocol):
    name: str
    # How long a snapshot from this source stays usable, and how often the scheduler
    # should refresh it. A provider whose data changes weekly can be refreshed hourly.
    max_age_minutes: int
    refresh_interval_s: int

    def available(self) -> bool: ...
    def fetch(self, days_ahead: int = 7) -> list[dict]: ...


class NewsProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    def fetch(self, limit: int = 20) -> list[dict]: ...


class UnconfiguredCalendar:
    """No calendar source is configured. Reports itself as unavailable."""

    name = "unconfigured"
    max_age_minutes = CALENDAR_MAX_AGE_MINUTES
    refresh_interval_s = 6 * 3600

    def available(self) -> bool:
        return False

    def fetch(self, days_ahead: int = 7) -> list[dict]:
        raise ProviderUnavailable(
            "no economic-calendar source configured; set TRADER_CALENDAR_SOURCE="
            "forexfactory for the free public calendar, or TRADER_CALENDAR_FEED_URL "
            "for a licensed feed (see docs/DATA_SOURCES.md)")


class ForexFactoryCalendar:
    """The free public weekly economic calendar, behind an explicit opt-in.

    Payload: a JSON list of {title, country, date, impact, forecast, previous}, where
    `country` is the currency code and `date` carries its own UTC offset (the feed
    publishes exchange time), so conversion is arithmetic rather than a guess. The
    horizon is the current week only.

    This is not a licensed feed: it is an undocumented public endpoint of a public
    calendar site, which is why selection is explicit (`TRADER_CALENDAR_SOURCE=
    forexfactory`) and every row it produces is stamped `quality="UNOFFICIAL_PUBLIC"`
    so nothing downstream can mistake it for a licensed source. Rows are validated
    individually; a malformed row is dropped, never repaired.
    """

    name = "forexfactory"
    # The weekly payload changes rarely, so an hourly refresh keeps it comfortably
    # inside the freshness window while staying light on the endpoint.
    max_age_minutes = CALENDAR_MAX_AGE_MINUTES
    refresh_interval_s = 3600
    quality = "UNOFFICIAL_PUBLIC"

    def __init__(self, url: str | None = None):
        self.url = url or config.free_calendar_url("forexfactory")

    def available(self) -> bool:
        return bool(self.url)

    def fetch(self, days_ahead: int = 7) -> list[dict]:
        if not self.available():
            raise ProviderUnavailable("no free calendar URL configured")
        import httpx
        response = httpx.get(self.url, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ProviderUnavailable("free calendar payload was not a list of events")
        retrieved = db.now_iso()
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        horizon = datetime.now(timezone.utc) + timedelta(days=max(days_ahead, 7))
        events = []
        for row in payload:
            try:
                event = self._normalise(row, retrieved, cutoff, horizon)
            except (KeyError, TypeError, ValueError):
                continue
            if event is not None:
                events.append(event)
        return events

    def _normalise(self, row: dict, retrieved: str, cutoff: datetime,
                   horizon: datetime) -> dict | None:
        currency = str(row["country"]).strip().upper()
        title = str(row["title"]).strip()
        if currency not in config.CALENDAR_CURRENCIES or not title:
            return None                       # "ALL", a currency we do not reason about
        when = _parse_utc(row["date"])
        if when < cutoff or when > horizon:
            return None                       # old copy, or beyond the horizon
        impact = FF_IMPACT_MAP.get(str(row.get("impact", "")).strip(), "UNKNOWN")
        # Identity is stable across fetches, so re-ingesting updates a row instead of
        # creating a second copy of the same release.
        key = f"{currency}|{title}|{when.isoformat()}"
        return {
            "event_id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
            "title": title[:300],
            "currency": currency,
            "ts_utc": when.isoformat(),
            "actual": None,
            "forecast": _maybe_str(row.get("forecast")),
            "previous": _maybe_str(row.get("previous")),
            "impact": impact,
            "source_url": self.url,
            "retrieved_at": retrieved,
            "quality": self.quality,
        }


class HttpJsonCalendar:
    """Generic adapter for a licensed JSON calendar feed.

    Expected payload: a list of objects with event_id/title/currency/ts_utc/impact and
    optional actual/forecast/previous/source_url. Times may carry any offset and are
    converted to UTC. The response is validated per row and bad rows are dropped, never
    guessed.
    """

    name = "http-json"
    max_age_minutes = CALENDAR_MAX_AGE_MINUTES
    refresh_interval_s = 6 * 3600

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = url or os.getenv("TRADER_CALENDAR_FEED_URL", "")
        self.api_key = api_key or os.getenv("TRADER_CALENDAR_API_KEY", "")

    def available(self) -> bool:
        return bool(self.url)

    def fetch(self, days_ahead: int = 7) -> list[dict]:
        if not self.available():
            raise ProviderUnavailable("TRADER_CALENDAR_FEED_URL is not configured")
        import httpx
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = httpx.get(self.url, params={"days": days_ahead}, headers=headers,
                             timeout=20)
        response.raise_for_status()
        retrieved = db.now_iso()
        events = []
        for row in response.json().get("events", []):
            try:
                events.append(self._normalise(row, retrieved))
            except (KeyError, TypeError, ValueError):
                continue
        return events

    def _normalise(self, row: dict, retrieved: str) -> dict:
        impact = str(row.get("impact", "UNKNOWN")).upper()
        return {
            "event_id": str(row["event_id"]),
            "title": str(row["title"])[:300],
            "currency": str(row["currency"]).upper()[:8],
            "ts_utc": _parse_utc(row["ts_utc"]).isoformat(),
            "actual": _maybe_str(row.get("actual")),
            "forecast": _maybe_str(row.get("forecast")),
            "previous": _maybe_str(row.get("previous")),
            "impact": impact if impact in IMPACTS else "UNKNOWN",
            "source_url": str(row.get("source_url") or self.url),
            "retrieved_at": retrieved,
            "quality": "CONFIRMED",
        }


class UnconfiguredNews:
    name = "unconfigured"

    def available(self) -> bool:
        return False

    def fetch(self, limit: int = 20) -> list[dict]:
        raise ProviderUnavailable(
            "no news provider configured; set TRADER_NEWS_FEED_URL after reviewing "
            "the provider's licensing terms (see docs/DATA_SOURCES.md)")


class HttpJsonNews:
    """Generic adapter for a licensed headline feed. Headlines are never rewritten."""

    name = "http-json"

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = url or os.getenv("TRADER_NEWS_FEED_URL", "")
        self.api_key = api_key or os.getenv("TRADER_NEWS_API_KEY", "")

    def available(self) -> bool:
        return bool(self.url)

    def fetch(self, limit: int = 20) -> list[dict]:
        if not self.available():
            raise ProviderUnavailable("TRADER_NEWS_FEED_URL is not configured")
        import httpx
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = httpx.get(self.url, params={"limit": limit}, headers=headers, timeout=20)
        response.raise_for_status()
        out = []
        for row in response.json().get("articles", [])[:limit]:
            try:
                out.append({
                    "headline": str(row["headline"])[:400],
                    "source": str(row["source"])[:120],
                    "published_at_utc": _parse_utc(row["published_at"]).isoformat(),
                    # Every item is attributed to the provider, never presented as
                    # something this system observed or summarised itself.
                    "origin": "PROVIDER_FEED",
                    "url": str(row.get("url", "")),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out


def _maybe_str(value) -> str | None:
    return None if value in (None, "") else str(value)


def calendar_provider() -> CalendarProvider:
    """The calendar source, chosen explicitly and reported as such.

    A licensed feed URL wins whenever one is configured. Otherwise an explicitly named
    free source is used. Nothing is picked by default: the desk's policy is that only a
    calendar the operator has chosen can gate trading, so an unconfigured install keeps
    reporting UNAVAILABLE and the risk engine keeps refusing trades.
    """
    if os.getenv("TRADER_CALENDAR_FEED_URL"):
        return HttpJsonCalendar()
    source = (os.getenv("TRADER_CALENDAR_SOURCE") or "").strip().lower()
    if source in ("forexfactory", "ff", "free"):
        return ForexFactoryCalendar()
    return UnconfiguredCalendar()


def refresh_interval_s() -> int:
    return int(getattr(calendar_provider(), "refresh_interval_s", 6 * 3600))


def provider_notes() -> str:
    """The provenance sentence that must travel with a status or an event list."""
    provider = calendar_provider()
    if isinstance(provider, ForexFactoryCalendar):
        return ("free public weekly calendar, unofficial endpoint: not a licensed feed "
                "and not for redistribution")
    if isinstance(provider, HttpJsonCalendar):
        return "licensed feed configured by the operator"
    return "no calendar source selected"


def news_provider() -> NewsProvider:
    return HttpJsonNews()


def calendar_status() -> dict:
    """What the desk may believe about the calendar right now."""
    provider = calendar_provider()
    max_age = int(getattr(provider, "max_age_minutes", CALENDAR_MAX_AGE_MINUTES))
    if not provider.available():
        return {"provider": provider.name, "state": UNAVAILABLE, "events": 0,
                "reason": "no calendar source configured",
                "note": provider_notes()}
    newest = db.query("SELECT MAX(retrieved_at) AS ts FROM events")[0]["ts"]
    if not newest:
        return {"provider": provider.name, "state": UNAVAILABLE, "events": 0,
                "reason": "source configured but never ingested",
                "note": provider_notes()}
    age = (datetime.now(timezone.utc) - _parse_utc(newest)).total_seconds() / 60
    state = STALE if age > max_age else AVAILABLE
    count = db.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
    return {"provider": provider.name, "state": state, "events": count,
            "last_retrieved_utc": newest, "age_minutes": round(age, 1),
            "max_age_minutes": max_age, "note": provider_notes()}


def _article_id(item: dict) -> str:
    """Stable identity for deduplication: same headline, source and publication time."""
    key = f"{item['headline']}|{item['source']}|{item['published_at_utc']}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def ingest_news(limit: int = 20) -> dict:
    """Fetch, validate and store headlines. Duplicate articles are ignored."""
    provider = news_provider()
    items = provider.fetch(limit)
    now = db.now_iso()
    newest_allowed = (datetime.now(timezone.utc)
                      - timedelta(hours=NEWS_MAX_PUBLISHED_AGE_HOURS)).isoformat()
    stored = 0
    with db.tx() as con:
        for item in items:
            if item["published_at_utc"] < newest_allowed:
                continue  # old copy is not current news
            article_id = _article_id(item)
            cursor = con.execute(
                "INSERT OR IGNORE INTO news(article_id, headline, source, url, "
                "published_at, retrieved_at, content_hash) VALUES(?,?,?,?,?,?,?)",
                (article_id, item["headline"], item["source"], item.get("url", ""),
                 item["published_at_utc"], now, article_id),
            )
            stored += cursor.rowcount
        db.audit(con, "news", "news_ingested",
                 {"provider": provider.name, "fetched": len(items), "new": stored})
    return {"provider": provider.name, "fetched": len(items), "new": stored,
            **news_status()}


def stored_news(limit: int = 20) -> list[dict]:
    """Headlines inside the freshness window, newest first. Never rewritten."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=NEWS_MAX_PUBLISHED_AGE_HOURS)).isoformat()
    rows = db.query(
        "SELECT * FROM news WHERE published_at >= ? ORDER BY published_at DESC LIMIT ?",
        (cutoff, limit))
    return [{
        "headline": row["headline"], "source": row["source"],
        "published_at_utc": row["published_at"], "retrieved_at_utc": row["retrieved_at"],
        "origin": "PROVIDER_FEED", "url": row["url"],
    } for row in rows]


def news_status() -> dict:
    """AVAILABLE requires a configured feed AND a recent successful ingestion.

    A configured URL proves nothing on its own: the old version of this function said
    AVAILABLE the moment TRADER_NEWS_FEED_URL was set, before any headline existed.
    """
    provider = news_provider()
    if not provider.available():
        return {"provider": provider.name, "state": UNAVAILABLE, "items": 0,
                "reason": "no news provider configured"}
    newest = db.query("SELECT MAX(retrieved_at) AS ts FROM news")[0]["ts"]
    if not newest:
        return {"provider": provider.name, "state": UNAVAILABLE, "items": 0,
                "reason": "feed configured but never ingested"}
    age = (datetime.now(timezone.utc) - _parse_utc(newest)).total_seconds() / 60
    published_cutoff = (datetime.now(timezone.utc)
                        - timedelta(hours=NEWS_MAX_PUBLISHED_AGE_HOURS)).isoformat()
    items = db.query("SELECT COUNT(*) AS n FROM news WHERE published_at >= ?",
                     (published_cutoff,))[0]["n"]
    state = STALE if age > NEWS_MAX_AGE_MINUTES else AVAILABLE
    return {"provider": provider.name, "state": state, "items": items,
            "last_retrieved_utc": newest, "age_minutes": round(age, 1)}


def ingest_calendar(days_ahead: int = 7) -> dict:
    """Fetch and upsert calendar events. Returns a summary; raises if not configured."""
    provider = calendar_provider()
    events = provider.fetch(days_ahead)
    now = db.now_iso()
    with db.tx() as con:
        for event in events:
            if _parse_utc(event["ts_utc"]) < datetime.now(timezone.utc) - timedelta(days=2):
                continue  # old history is not needed for a blackout window
            con.execute(
                "INSERT INTO events(event_id,title,currency,ts_utc,actual,forecast,previous,"
                "impact,source_url,retrieved_at,quality) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET actual=excluded.actual, "
                "forecast=excluded.forecast, previous=excluded.previous, "
                "impact=excluded.impact, retrieved_at=excluded.retrieved_at, "
                "quality=excluded.quality",
                (event["event_id"], event["title"], event["currency"], event["ts_utc"],
                 event["actual"], event["forecast"], event["previous"], event["impact"],
                 event["source_url"], now, event["quality"]),
            )
        by_impact = {level: sum(1 for e in events if e["impact"] == level)
                     for level in IMPACTS if any(e["impact"] == level for e in events)}
        db.audit(con, "calendar", "calendar_ingested",
                 {"provider": provider.name, "events": len(events),
                  "by_impact": by_impact, "quality": getattr(provider, "quality", None)})
    return {"provider": provider.name, "ingested": len(events), "by_impact": by_impact,
            **calendar_status()}


def fetch_news(limit: int = 20) -> list[dict]:
    return news_provider().fetch(limit)


def macro_series(series_id: str = "DFF") -> dict:
    """FRED is wired but only reads live when a key exists; otherwise it says so."""
    from .data import fetch_fred_series
    if not os.getenv("FRED_API_KEY"):
        return {"series_id": series_id, "state": UNAVAILABLE,
                "reason": "FRED_API_KEY is not configured"}
    try:
        observations = fetch_fred_series(series_id)
    except Exception as exc:  # noqa: BLE001 - reported as unavailable, never raised
        db.log("macro", "fred_unavailable", {"error": type(exc).__name__})
        return {"series_id": series_id, "state": UNAVAILABLE,
                "reason": f"FRED request failed ({type(exc).__name__})"}
    return {"series_id": series_id, "state": AVAILABLE, "observations": observations}
