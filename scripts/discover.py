#!/usr/bin/env python3
"""Discover papers from public metadata feeds and maintain a local review inbox."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
import xml.etree.ElementTree as ET

from papers import ROOT, read_json, write_json

API = {"crossref": "https://api.crossref.org/works", "openalex": "https://api.openalex.org/works"}
DEFAULTS = {
    "initial_lookback_days": 30, "overlap_days": 7, "max_pages_per_query": 2,
    "page_size": 25, "max_candidates": 100, "recommendation_limit": 5,
}
BOUNDS = {
    "initial_lookback_days": (1, 3650), "overlap_days": (0, 365),
    "max_pages_per_query": (1, 10), "page_size": (1, 100),
    "max_candidates": (1, 500), "recommendation_limit": (1, 25),
}
CACHE_TTL = timedelta(hours=24)
RESPONSE_LIMIT = 4 * 1024 * 1024
MAX_ABSTRACT = 16_000
MAX_NETWORK_SECONDS = 90
REVIEW_STATES = ("recommended", "read", "dismissed")


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(now):
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_doi(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)
    value = unquote(value).casefold()
    return value if re.fullmatch(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]+', value) else None


def doi_url(doi):
    return "https://doi.org/" + quote(doi, safe="/()")


def title_key(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "sec", "title", "jats:p", "jats:sec", "jats:title"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        else:
            self.handle_starttag(tag, ())

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit=MAX_ABSTRACT):
    if not isinstance(value, str):
        return ""
    parser = PlainText()
    parser.feed(value[:limit * 4])
    parser.close()
    return " ".join("".join(parser.parts).split())[:limit]


def web_url(value):
    if not isinstance(value, str) or re.search(r"\s", value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"https", "http"} and url.hostname and url.username is None:
            return value
    except ValueError:
        pass
    return None


def strings(value, label, max_items=100, max_length=300):
    if (not isinstance(value, list) or len(value) > max_items
            or any(not isinstance(v, str) or not v.strip() or len(v) > max_length for v in value)):
        raise ValueError(f"{label} must be a list of up to {max_items} nonempty strings")
    return list(dict.fromkeys(v.strip() for v in value))


def load_profile(path):
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"Missing or unsupported discovery profile at {path}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("Profile name must be a nonempty string")
    topics = data.get("topics")
    if not isinstance(topics, list) or not 1 <= len(topics) <= 12:
        raise ValueError("Profile needs 1 to 12 topics")
    ids = set()
    queries = set()
    for topic in topics:
        if (not isinstance(topic, dict) or not isinstance(topic.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", topic["id"])
                or topic["id"] in ids or not isinstance(topic.get("label"), str)
                or not topic["label"].strip()):
            raise ValueError("Each topic needs a unique id and a nonempty label")
        ids.add(topic["id"])
        topic["queries"] = strings(topic.get("queries"), f"{topic['id']}.queries", max_items=12)
        topic["terms"] = strings(topic.get("terms"), f"{topic['id']}.terms")
        queries.update(topic["queries"])
    if not 1 <= len(queries) <= 24:
        raise ValueError("Profile needs 1 to 24 distinct queries")
    data["region_terms"] = strings(data.get("region_terms", []), "region_terms")
    data["required_terms"] = strings(data.get("required_terms", []), "required_terms")
    data["seed_dois"] = strings(data.get("seed_dois", []), "seed_dois", max_items=20)
    data["seed_dois"] = list(dict.fromkeys(map(normalize_doi, data["seed_dois"])))
    if None in data["seed_dois"]:
        raise ValueError("seed_dois contains an invalid DOI")
    sources = data.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("sources must be an object")
    for provider in API:
        config = sources.setdefault(provider, {"enabled": False})
        if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
            raise ValueError(f"sources.{provider}.enabled must be true or false")
    crossref_search = sources["crossref"].setdefault("search_enabled", True)
    if type(crossref_search) is not bool:
        raise ValueError("sources.crossref.search_enabled must be true or false")
    openalex_scope = sources["openalex"].setdefault("search_scope", "title_and_abstract")
    if openalex_scope not in ("title_and_abstract", "fulltext"):
        raise ValueError("sources.openalex.search_scope must be title_and_abstract or fulltext")
    feeds = sources.setdefault("rss", [])
    if not isinstance(feeds, list) or len(feeds) > 12:
        raise ValueError("sources.rss must contain at most 12 feeds")
    feed_ids = set()
    for feed in feeds:
        if (not isinstance(feed, dict) or not isinstance(feed.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", feed["id"])
                or feed["id"] in feed_ids or type(feed.get("enabled")) is not bool
                or not web_url(feed.get("url"))):
            raise ValueError("Each RSS feed needs a unique id, HTTP(S) URL and enabled flag")
        feed_ids.add(feed["id"])
    if not (sources["openalex"]["enabled"] or sources["crossref"]["enabled"]
            and (crossref_search or data["seed_dois"]) or any(f["enabled"] for f in feeds)):
        raise ValueError("Enable at least one discovery source")
    monitor = data.get("monitor", {})
    if not isinstance(monitor, dict):
        raise ValueError("monitor must be an object")
    data["monitor"] = {**DEFAULTS, **monitor}
    for key, (minimum, maximum) in BOUNDS.items():
        value = data["monitor"][key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"monitor.{key} must be between {minimum} and {maximum}")
    return data


def local_index(root):
    data = read_json(root / "library/manifest.json")
    if data is None:
        return {}, {}, "missing"
    dois, titles = {}, defaultdict(set)
    for paper in data["papers"]:
        existing = next((path for name in paper["source_paths"]
                         if (path := (root / name).resolve()).is_relative_to(root.resolve())
                         and path.is_file()), None)
        if existing is None:
            continue
        doi = normalize_doi(paper.get("doi"))
        if doi:
            dois[doi] = paper["id"]
        key = title_key(paper["title"])
        if key:
            titles[key].add((doi, paper["id"]))
    return dois, titles, "available"


def local_match(item, local):
    dois, titles, _ = local
    if item["doi"] and item["doi"] in dois:
        return dois[item["doi"]]
    matches = [paper_id for doi, paper_id in titles.get(title_key(item["title"]), ())
               if not doi or not item["doi"]]
    return sorted(matches)[0] if matches else None


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, cache):
        self.cache = cache
        self.deadline = time.monotonic() + MAX_NETWORK_SECONDS
        self.openalex_key = os.getenv("OPENALEX_API_KEY", "").strip()

    def get(self, url, provider):
        key = self.openalex_key if provider == "openalex" else ""
        cache_path = self.cache / (sha(url + "\n" + sha(key)) + ".json")
        try:
            cached = read_json(cache_path)
            retrieved = datetime.fromisoformat(cached["retrieved_at"].replace("Z", "+00:00"))
            if timedelta(0) <= utcnow() - retrieved < CACHE_TTL:
                return cached["body"], cached["retrieved_at"], True
        except (OSError, ValueError, TypeError, KeyError):
            pass
        request = Request(url, headers={
            "User-Agent": "Reference-Paper-Discovery/1.0",
            "Accept": "application/json" if provider in API else "application/rss+xml, application/atom+xml, text/xml, application/xml",
        })
        if key:
            request.add_unredirected_header("Authorization", "Bearer " + key)
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("run network time limit reached")
            try:
                with urlopen(request, timeout=min(8, remaining)) as response:
                    chunks = []
                    size = 0
                    while True:
                        if time.monotonic() > self.deadline:
                            raise FetchError("run network time limit reached")
                        chunk = response.read1(min(64 * 1024, RESPONSE_LIMIT + 1 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > RESPONSE_LIMIT:
                            raise FetchError("response exceeds size limit")
                    body = b"".join(chunks)
                    if provider in API:
                        body = json.loads(body)
                    else:
                        body = body.decode(response.headers.get_content_charset() or "utf-8")
                    retrieved = timestamp(utcnow())
                write_json(cache_path, {"body": body, "retrieved_at": retrieved})
                return body, retrieved, False
            except HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After")
                exc.close()
                if code not in {429, 500, 502, 503, 504} or attempt:
                    raise FetchError(f"HTTP {code}") from None
                delay = 1
                if retry_after:
                    try:
                        delay = max(0, int(retry_after))
                    except ValueError:
                        try:
                            delay = max(0, (parsedate_to_datetime(retry_after) - utcnow()).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            pass
                if delay > 4 or time.monotonic() + delay >= self.deadline:
                    raise FetchError(f"HTTP {code}, retry delay exceeds run budget") from None
                time.sleep(delay)
            except (TimeoutError, socket.timeout, URLError, ConnectionError):
                raise FetchError("network connection or timeout") from None
            except (UnicodeError, LookupError, ValueError):
                raise FetchError("malformed or unsupported response") from None


def publication_date(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}(?:-\d{2})?", value):
            date.fromisoformat(value + ("-01-01" if len(value) == 4 else "-01"))
            return value
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (TypeError, ValueError, OverflowError, AttributeError):
            return None


def crossref_date(row, fields):
    for field in fields:
        parts = (row.get(field) or {}).get("date-parts", [[]])[0]
        if parts and all(type(part) is int for part in parts[:3]):
            value = "-".join(f"{part:04d}" if i == 0 else f"{part:02d}"
                             for i, part in enumerate(parts[:3]))
            parsed = publication_date(value)
            if parsed:
                return parsed
    return None


def reconstruct_abstract(index):
    if not isinstance(index, dict):
        return ""
    words = {}
    for word, positions in index.items():
        if isinstance(positions, list):
            for pos in positions:
                if type(pos) is int and 0 <= pos < 8000:
                    words[pos] = word
    return " ".join(words[pos] for pos in sorted(words))


def observation(source, retrieved, *, title, doi=None, url=None, published=None, abstract=None,
                kind=None, status=None, relations=None):
    title, abstract, doi = plain(title, 1200), plain(abstract), normalize_doi(doi)
    url = web_url(url) or (doi_url(doi) if doi else None)
    if not title_key(title):
        return None
    if not url:
        raise ValueError("record has no source URL")
    return {
        "source": source, "source_url": url, "retrieved_at": retrieved,
        "title": title, "doi": doi, "publication_date": published, "abstract": abstract or None,
        "evidence_level": "abstract_only" if abstract else "metadata_only",
        "work_type": plain(kind, 120) or None, "publication_status": status,
        "related_dois": sorted(set(relations or ())),
    }


def crossref_record(row, retrieved):
    titles = row.get("title") or []
    doi = normalize_doi(row.get("DOI"))
    kind = row.get("type")
    status = "journal_article" if kind == "journal-article" else None
    if kind == "posted-content":
        subtype = row.get("subtype", "preprint")
        status = "preprint" if subtype == "preprint" else "posted_content"
    if any(update.get("type") in {"retraction", "withdrawal"} for update in row.get("update-to") or []):
        # update-to describes this notice's relation to the original work.
        status = "retraction_notice"
    relations = [related for name, values in (row.get("relation") or {}).items()
                 if name in {"is-preprint-of", "has-preprint", "is-version-of", "has-version"}
                 for value in values if (related := normalize_doi(value.get("id")))]
    return observation(
        "crossref", retrieved, title=titles[0] if titles else None,
        doi=doi, url=doi_url(doi) if doi else row.get("URL"),
        published=crossref_date(row, ("published", "published-online", "published-print", "issued")),
        abstract=row.get("abstract"), kind=kind, status=status, relations=relations,
    )


def openalex_record(row, retrieved):
    kind = row.get("type")
    status = "retracted" if row.get("is_retracted") else "preprint" if kind == "preprint" else None
    return observation(
        "openalex", retrieved, title=row.get("display_name"), doi=row.get("doi"),
        url=row.get("id"), published=publication_date(row.get("publication_date")),
        abstract=reconstruct_abstract(row.get("abstract_inverted_index")),
        kind=kind, status=status,
    )


def rss_entries(xml, feed_url):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, flags=re.I):
        raise ValueError("feed XML declarations are unsupported")
    root = ET.fromstring(xml)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag not in {"rss", "rdf", "feed"}:
        raise ValueError("response is not RSS or Atom")
    nodes = root.findall(".//{*}entry" if tag == "feed" else ".//{*}item")
    for node in nodes:
        fields = {}
        for child in node:
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name == "link" and child.attrib.get("rel", "alternate") != "alternate":
                continue
            value = child.attrib.get("href") or " ".join(child.itertext()).strip()
            if value:
                fields.setdefault(name, value)
        link = fields.get("link")
        doi = next((doi for name in ("doi", "identifier", "guid", "id", "link")
                    if (doi := normalize_doi(fields.get(name)))), None)
        yield {
            "title": fields.get("title"), "doi": doi,
            "url": web_url(link) or (doi_url(doi) if doi else None) or web_url(fields.get("id")) or feed_url,
            "published": publication_date(fields.get("published") or fields.get("pubdate") or fields.get("date")),
            # Feed descriptions may be editor summaries or article blurbs.
            "abstract": fields.get("abstract"),
        }


def connect(root):
    path = root / "discovery/state.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS works (
            id TEXT PRIMARY KEY, doi TEXT, title_key TEXT NOT NULL,
            review_status TEXT NOT NULL, content_hash TEXT NOT NULL, data TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS work_doi ON works(doi) WHERE doi IS NOT NULL;
        CREATE INDEX IF NOT EXISTS work_title ON works(title_key);
        CREATE TABLE IF NOT EXISTS progress (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, query TEXT NOT NULL,
            watermark TEXT, attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, summary TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backlog (
            id TEXT PRIMARY KEY, from_date TEXT NOT NULL
        );
    """)
    return db


def saved_progress(db):
    rows = db.execute("SELECT progress.*, backlog.from_date FROM progress LEFT JOIN backlog USING (id)")
    return {row["id"]: row for row in rows}


def tasks(profile, progress, today, days, query):
    sources, monitor = profile["sources"], profile["monitor"]
    queries = [query] if query else list(dict.fromkeys(q for t in profile["topics"] for q in t["queries"]))
    planned = []

    def add(source, search, basis, feed=None):
        scope = sources["openalex"]["search_scope"] if source == "openalex" else None
        identity = [source, feed or "", search]
        if scope:
            identity.append(scope)
        key = sha("\n".join(identity))
        previous = progress.get(key)
        watermark = previous["watermark"] if previous else None
        lower = today - timedelta(days=days or monitor["initial_lookback_days"])
        if watermark:
            resumed = date.fromisoformat(watermark) - timedelta(days=monitor["overlap_days"])
            lower = min(lower, resumed) if days else resumed
        if previous and previous["from_date"]:
            lower = min(lower, date.fromisoformat(previous["from_date"]))
        planned.append({
            "key": key, "source": source, "query": search, "basis": basis,
            "from": lower.isoformat() if basis != "tracked_doi" else None,
            "to": today.isoformat() if basis != "tracked_doi" else None,
            "previous_watermark": watermark, "feed_url": feed,
            "previous_attempt": previous["attempted_at"] if previous else "",
            **({"search_scope": scope} if scope else {}),
        })

    for search in queries:
        if sources["crossref"]["enabled"] and sources["crossref"]["search_enabled"]:
            add("crossref", search, "created_date")
        if sources["openalex"]["enabled"]:
            add("openalex", search, "publication_date")
    for feed in sources["rss"]:
        if feed["enabled"]:
            add("rss:" + feed["id"], query or "", "feed_snapshot", feed["url"])
    if not query and sources["crossref"]["enabled"]:
        for doi in profile["seed_dois"]:
            add("crossref", doi, "tracked_doi")
    return sorted(planned, key=lambda task: task["previous_attempt"])


def build_url(task, size, cursor):
    source, basis = task["source"], task["basis"]
    if basis == "tracked_doi":
        return API[source] + "/" + quote(task["query"], safe="/")
    if source == "crossref":
        params = {
            "query.bibliographic": task["query"], "rows": size, "cursor": cursor,
            "filter": f"from-created-date:{task['from']},until-created-date:{task['to']}",
        }
    else:
        search_parameter = "search" if task["search_scope"] == "fulltext" else "search.title_and_abstract"
        params = {
            search_parameter: task["query"], "per_page": size, "cursor": cursor,
            "filter": f"from_publication_date:{task['from']},to_publication_date:{task['to']}",
            "select": "id,doi,display_name,publication_date,type,is_retracted,abstract_inverted_index",
        }
    return API[source] + "?" + urlencode(params)


def read_page(task, payload):
    source = task["source"]
    if source == "crossref":
        message = payload["message"]
        if task["basis"] == "tracked_doi":
            return [message], 1, None, crossref_record
        return message["items"], message.get("total-results"), message.get("next-cursor"), crossref_record
    if source == "openalex":
        meta = payload["meta"]
        return payload["results"], meta.get("count"), meta.get("next_cursor"), openalex_record
    entries = list(rss_entries(payload, task["feed_url"]))
    start, end = task["from"], task["to"]
    entries = [e for e in entries if not e["published"] or
               start[:len(e["published"])] <= e["published"] <= end[:len(e["published"])]]
    if task["query"]:
        words = title_key(task["query"]).split()
        entries = [e for e in entries if all(
            term_matches(word, title_key(plain(e["title"]) + " " + plain(e["abstract"]))) for word in words
        )]
    return entries, len(entries), None, lambda row, retrieved: observation(source, retrieved, **row)


def fetch_task(task, settings, fetcher, budget):
    source = task["source"]
    result = {k: task[k] for k in ("key", "source", "query", "basis", "from", "to", "previous_watermark")}
    result.update(status="capped", reason="candidate_limit", pages=0, items=0, ignored_items=0, cached_pages=0)
    if task["feed_url"]:
        result["feed_url"] = task["feed_url"]
    if source == "openalex":
        result["search_scope"] = task["search_scope"]
    records = []
    if budget == 0:
        return result, records
    size = settings["page_size"]
    cursor = "*"
    seen_pages = set()
    try:
        for _ in range(settings["max_pages_per_query"]):
            remaining = budget - result["items"]
            if not remaining:
                break
            page_size = min(size, remaining)
            url = task["feed_url"] or build_url(task, page_size, cursor)
            result["attempted"] = True
            payload, retrieved, cached = fetcher.get(url, source)
            rows, total, next_cursor, parse = read_page(task, payload)
            if not isinstance(rows, list) or (total is not None and (type(total) is not int or total < 0)):
                raise ValueError("invalid page structure")
            result["source_total"] = total
            fingerprint = sha(json.dumps(rows, sort_keys=True))
            if rows and fingerprint in seen_pages:
                raise ValueError("repeated page")
            seen_pages.add(fingerprint)
            result["pages"] += 1
            result["cached_pages"] += int(cached)
            for row in rows[:remaining]:
                result["items"] += 1
                parsed = parse(row, retrieved)
                if parsed:
                    records.append(parsed)
                else:
                    result["ignored_items"] += 1
            if len(rows) > remaining:
                break
            if (total is not None and result["items"] >= total) or (not next_cursor and total is None):
                result.update(status="complete", reason=None)
                break
            if not next_cursor or not rows:
                raise ValueError("pagination ended before reported result count")
            cursor = next_cursor
            result["reason"] = "candidate_limit" if result["items"] >= budget else "page_limit"
            if source == "openalex" and isinstance(fetcher, Fetcher) and not fetcher.openalex_key:
                if result["items"] < budget:
                    result["reason"] = "openalex_api_key_required_for_pagination"
                break
    except FetchError as exc:
        result.update(status="failed", reason=str(exc))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, ET.ParseError):
        result.update(status="failed", reason="malformed source response or inconsistent pagination")
    return result, records


def collect(plan, settings, fetcher):
    coverage, rows = [], []
    remaining = settings["max_candidates"]
    for task in plan:
        item, records = fetch_task(task, settings, fetcher, remaining)
        coverage.append(item)
        rows.extend(records)
        remaining -= item["items"]
    return coverage, rows


def term_matches(term, corpus):
    normalized = title_key(term)
    if not normalized:
        return False
    if re.search(r"[\u3400-\u9fff]", normalized):
        return normalized in corpus
    return " " + normalized + " " in " " + corpus + " "


def rank(work, profile, query):
    title, abstract = title_key(work["title"]), title_key(work["abstract"] or "")
    hits = []
    weights = {}
    for topic in profile["topics"]:
        matched = [term for term in topic["terms"] if term_matches(term, title) or term_matches(term, abstract)]
        if matched:
            hits.append({"id": topic["id"], "label": topic["label"], "terms": matched})
            for term in matched:
                weights[title_key(term)] = 4 if term_matches(term, title) else 2
    regions = [term for term in profile["region_terms"] if term_matches(term, title) or term_matches(term, abstract)]
    total = sum(weights.values()) + len(regions) * 2
    if query and term_matches(query, title):
        total += 8
    return total, hits, regions


def provider_order(source):
    return ({"crossref": 0, "openalex": 1}.get(source, 2), source)


def merge_records(previous, observations, now):
    existing = previous["sources"] if previous else []
    by_source = {row["source"]: row for row in existing}
    for row in observations:
        prior = by_source.get(row["source"])
        if prior is None or row["retrieved_at"] >= prior["retrieved_at"]:
            by_source[row["source"]] = row
    sources = sorted(by_source.values(), key=lambda record: provider_order(record["source"]))

    def first(field):
        return next((row[field] for row in sources if row[field]), None)

    statuses = {row["publication_status"] for row in sources}
    status = next((name for name in ("retracted", "retraction_notice") if name in statuses),
                  first("publication_status"))
    abstract = first("abstract")
    return {
        "title": first("title"), "doi": first("doi"), "abstract": abstract,
        "publication_date": first("publication_date"), "publication_status": status,
        "work_type": first("work_type"),
        "related_dois": sorted({doi for row in sources for doi in row["related_dois"]}),
        "evidence_level": "abstract_only" if abstract else "metadata_only", "full_text_read": False,
        "first_seen": previous["first_seen"] if previous else now,
        "last_seen": max(previous["last_seen"], now) if previous else now,
        "sources": sources,
    }


def grouped(rows):
    # Resolve title-only observations against DOI-bearing observations first.
    dois_by_title = defaultdict(set)
    groups = defaultdict(list)
    for row in rows:
        if row["doi"]:
            dois_by_title[title_key(row["title"])].add(row["doi"])
            groups[("doi", row["doi"])].append(row)
    for row in rows:
        if not row["doi"]:
            title = title_key(row["title"])
            dois = dois_by_title[title]
            key = ("doi", next(iter(dois))) if len(dois) == 1 else ("title", title)
            groups[key].append(row)
    return groups.items()


def stored_match(db, key, observations):
    kind, value = key
    if kind == "doi":
        exact = db.execute("SELECT * FROM works WHERE doi = ?", (value,)).fetchone()
        if exact:
            return exact
        for row in observations:
            title = title_key(row["title"])
            candidates = db.execute("SELECT * FROM works WHERE title_key = ?", (title,)).fetchall()
            if len(candidates) == 1 and candidates[0]["doi"] is None:
                return candidates[0]
        return None
    candidates = db.execute("SELECT * FROM works WHERE title_key = ?", (value,)).fetchall()
    return candidates[0] if len(candidates) == 1 else next(
        (row for row in candidates if row["doi"] is None), None
    )


CHANGE_FIELDS = ("title", "abstract", "doi", "publication_date", "publication_status", "related_dois")


def content_hash(work):
    return sha(json.dumps([work[field] for field in CHANGE_FIELDS], ensure_ascii=False))


def classify(work, profile, local, query=None):
    score, topics, regions = rank(work, profile, query)
    local_id = local_match(work, local)
    tracked = work["doi"] in profile["seed_dois"]
    required = profile["required_terms"]
    corpus = title_key(work["title"] + " " + (work["abstract"] or ""))
    return {
        **work, "score": score, "topic_matches": topics, "region_matches": regions,
        "tracked_doi": tracked, "known_local": local_id is not None,
        "eligible": bool(tracked or (any(term_matches(term, corpus) for term in required) if required else topics)),
        "local_paper_id": local_id,
    }


def save_works(db, rows, now):
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    for key, observations in grouped(rows):
        existing = stored_match(db, key, observations)
        previous = json.loads(existing["data"]) if existing else None
        work = merge_records(previous, observations, now)
        fingerprint = content_hash(work)
        fields = []
        if existing and existing["content_hash"] != fingerprint:
            fields = [field for field in CHANGE_FIELDS if previous[field] != work[field]]
        updated = bool(fields)
        if existing and not updated:
            event, state = "unchanged", existing["review_status"]
        else:
            event, state = ("changed" if updated else "new"), "pending"
        counts[event] += 1
        work["last_changed"] = now if event != "unchanged" else previous["last_changed"]
        work["discovery_event"] = event if event != "unchanged" else previous["discovery_event"]
        work["changed_fields"] = fields if event != "unchanged" else previous.get("changed_fields", [])
        work["id"] = existing["id"] if existing else "D" + uuid4().hex[:16]
        db.execute("""
            INSERT INTO works VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET doi=excluded.doi, title_key=excluded.title_key,
            review_status=excluded.review_status, content_hash=excluded.content_hash, data=excluded.data
        """, (work["id"], work["doi"], title_key(work["title"]), state, fingerprint,
              json.dumps(work, ensure_ascii=False)))
    return counts


def by_rank(work):
    return (-work["score"], -int(work["tracked_doi"]), work["abstract"] is None,
            tuple(-int(part) for part in (work["publication_date"] or "0").split("-")),
            work["title"].casefold(), work["id"])


def search_rank(work, requested_regions):
    return (
        work["known_local"],
        bool(requested_regions) and not work["region_matches"],
        bool(requested_regions) and not requested_regions.intersection(work["region_matches"]),
        by_rank(work),
    )


def pending(db, profile, local):
    rows = db.execute("SELECT data FROM works WHERE review_status = 'pending'")
    works = [classify(json.loads(row["data"]), profile, local) for row in rows]
    relevant = [work for work in works if work["eligible"]]
    available = [{**work, "status": "pending"} for work in relevant
                 if not work["known_local"] or work["discovery_event"] == "changed"]
    return sorted(available, key=lambda work: (not work["tracked_doi"],
                                               bool(profile["region_terms"]) and not work["region_matches"],
                                               by_rank(work))), len(relevant) - len(available)


def advance(db, coverage, now):
    progress = saved_progress(db)
    for item in coverage:
        current = progress.get(item["key"])
        previous = current["watermark"] if current else None
        backlog = current["from_date"] if current else None
        current_attempt = current["attempted_at"] if current else ""
        attempted = now if item.get("attempted") else ""
        prior_backlog = backlog and item["from"] and backlog < item["from"]
        complete = item["status"] == "complete" and not prior_backlog
        if item["status"] == "complete" and prior_backlog:
            item.update(status="capped", reason="earlier_backlog_from_concurrent_run")
        observed = item["to"] if complete else None
        latest = max(filter(None, (previous, observed)), default=None)
        db.execute("""
            INSERT INTO progress VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET watermark=excluded.watermark,
            attempted_at=MAX(progress.attempted_at, excluded.attempted_at)
        """, (item["key"], item["source"], item["query"], latest, attempted))
        if complete:
            db.execute("DELETE FROM backlog WHERE id = ?", (item["key"],))
            backlog = None
        elif item["from"] and now >= current_attempt:
            backlog = min(filter(None, (backlog, item["from"])))
            db.execute("""
                INSERT INTO backlog VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET from_date=excluded.from_date
            """, (item["key"], backlog))
        item["watermark"] = latest
        item["watermark_advanced"] = bool(latest and latest != previous)
        item["backlog_from"] = backlog


def report_status(coverage):
    if not coverage:
        return "failed"
    if all(item["status"] == "complete" for item in coverage):
        return "complete"
    return "partial" if any(item["status"] != "failed" or item["pages"] for item in coverage) else "failed"


def coverage_counts(coverage):
    return {state: sum(item["status"] == state for item in coverage)
            for state in ("complete", "capped", "failed")}


def run_report(root, mode, profile, started, coverage, counts, results, local, limit, run_id):
    path = root / "discovery/runs" / (run_id + ".json")
    data = {
        "mode": mode, "profile": profile["name"], "started_at": started,
        "finished_at": timestamp(utcnow()), "status": report_status(coverage),
        "local_index": local[2], "counts": counts, "coverage_counts": coverage_counts(coverage),
        "coverage": coverage, "results": results, "report_path": str(path),
        "results_shown": min(limit, len(results)), "results_omitted": max(0, len(results) - limit),
    }
    if not coverage:
        data["reason"] = "no_enabled_search_source"
    write_json(path, data)
    return {**data, "results": [compact(work) for work in results[:limit]]}


def compact(work):
    def trim(row):
        abstract = row["abstract"]
        return {**row, "abstract": abstract[:1600], "abstract_truncated": len(abstract) > 1600} if abstract else row

    return trim({**work, "sources": [trim(row) for row in work["sources"]]})


def run_search(root, profile, args, *, clock=utcnow, fetcher=None):
    now, local = clock(), local_index(root)
    started = timestamp(now)
    run_id = "search-" + uuid4().hex
    plan = tasks(profile, {}, now.date(), args.days, args.query)
    coverage, rows = collect(plan, profile["monitor"], fetcher or Fetcher(root / "discovery/cache"))
    works = []
    for (kind, value), observations in grouped(rows):
        work = merge_records(None, observations, started)
        work = classify({**work, "id": "S" + sha(kind + ":" + value)[:16]}, profile, local, args.query)
        works.append({**work, "status": "search_result"})
    requested_regions = {term for term in profile["region_terms"] if term_matches(term, title_key(args.query))}
    works.sort(key=lambda work: search_rank(work, requested_regions))
    counts = {"observations": len(rows), "unique_works": len(works),
              "known_local": sum(work["known_local"] for work in works)}
    return run_report(root, "search", profile, started, coverage, counts, works, local, args.limit, run_id)


def run_scan(root, profile, args, *, clock=utcnow, fetcher=None):
    now, local = clock(), local_index(root)
    started = timestamp(now)
    run_id = "scan-" + uuid4().hex
    limit = args.limit or profile["monitor"]["recommendation_limit"]
    with closing(connect(root)) as db:
        progress = saved_progress(db)
        plan = tasks(profile, progress, now.date(), args.days, None)
        coverage, rows = collect(plan, profile["monitor"], fetcher or Fetcher(root / "discovery/cache"))
        with db:
            db.execute("BEGIN IMMEDIATE")
            counts = save_works(db, rows, started)
            advance(db, coverage, started)
            works, suppressed = pending(db, profile, local)
            counts.update(observations=len(rows), pending=len(works), known_local_pending=suppressed)
            result = run_report(root, "scan", profile, started, coverage, counts, works, local, limit, run_id)
            summary = {key: result[key] for key in ("status", "report_path", "counts", "coverage_counts", "finished_at")}
            db.execute("INSERT INTO scans VALUES (?, ?, ?)", (run_id, started, json.dumps(summary)))
    return result


def inbox(root, profile, limit):
    local = local_index(root)
    with closing(connect(root)) as db:
        works, suppressed = pending(db, profile, local)
        latest = last_scan(db)
    return {
        "mode": "inbox", "last_scan": latest, "local_index": local[2],
        "counts": {"pending": len(works), "known_local_pending": suppressed},
        "results": [compact(work) for work in works[:limit]],
        "results_omitted": max(0, len(works) - limit),
    }


def mark(root, work_id, state):
    with closing(connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT review_status FROM works WHERE id = ?", (work_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown discovery ID: {work_id}")
        db.execute("UPDATE works SET review_status = ? WHERE id = ?", (state, work_id))
    return {"id": work_id, "previous_status": row["review_status"], "status": state}


def last_scan(db):
    row = db.execute("SELECT started_at, summary FROM scans ORDER BY started_at DESC LIMIT 1").fetchone()
    return {**json.loads(row["summary"]), "started_at": row["started_at"]} if row else None


def status(root, profile):
    local = local_index(root)
    with closing(connect(root)) as db:
        latest = last_scan(db)
        counts = {row[0]: row[1] for row in db.execute("SELECT review_status, count(*) FROM works GROUP BY review_status")}
        progress = saved_progress(db)
        configured = tasks(profile, progress, utcnow().date(), None, None)
        works, suppressed = pending(db, profile, local)
    return {
        "mode": "status", "profile": profile["name"], "local_index": local[2],
        "last_scan": latest, "counts": counts, "pending_relevant": len(works), "known_local_pending": suppressed,
        "watermarks": [{k: task[k] for k in
                        ("source", "query", "basis", "previous_watermark", "previous_attempt", "search_scope") if k in task}
                       for task in configured],
        "backlog_queries": [{"source": task["source"], "query": task["query"],
                             "from": progress[task["key"]]["from_date"]}
                            for task in configured if task["key"] in progress and progress[task["key"]]["from_date"]],
    }


def check(root, profile, profile_path):
    plan = tasks(profile, {}, utcnow().date(), None, None)
    local = local_index(root)
    return {
        "mode": "check", "status": "valid", "profile": profile["name"], "profile_path": str(profile_path),
        "local_index": local[2], "enabled_sources": list(dict.fromkeys(task["source"] for task in plan)),
        "query_tasks": len(plan), "openalex_key_configured": bool(os.getenv("OPENALEX_API_KEY", "").strip()),
        "openalex_search_scope": profile["sources"]["openalex"]["search_scope"],
        "state_path": str(root / "discovery/state.sqlite3"),
    }


def bounded(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number

    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--days", type=bounded(1, 3650), default=30)
    search.add_argument("--limit", type=bounded(1, 25), default=10)
    scan = sub.add_parser("scan")
    scan.add_argument("--days", type=bounded(1, 3650))
    scan.add_argument("--limit", type=bounded(1, 25))
    sub.add_parser("inbox").add_argument("--limit", type=bounded(1, 25), default=5)
    mark_parser = sub.add_parser("mark")
    mark_parser.add_argument("id")
    mark_parser.add_argument("--status", choices=REVIEW_STATES, required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    profile_path = (root / (args.profile or Path("discovery/interests.json"))).resolve()
    try:
        profile = load_profile(profile_path)
        if args.command == "search":
            args.query = args.query.strip()
            if not args.query or len(args.query) > 300:
                raise ValueError("query must contain 1 to 300 characters")
            result = run_search(root, profile, args)
        elif args.command == "scan":
            result = run_scan(root, profile, args)
        elif args.command == "inbox":
            result = inbox(root, profile, args.limit)
        elif args.command == "mark":
            result = mark(root, args.id, args.status)
        elif args.command == "status":
            result = status(root, profile)
        else:
            result = check(root, profile, profile_path)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
