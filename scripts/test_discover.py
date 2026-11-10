"""Offline tests for discovery sources and monitor state."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

import discover

NOW = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
STAMP = discover.timestamp(NOW)
DOI = "10.1234/abc"
QUERY = "North Atlantic marine heatwave"
TITLE = "North Atlantic marine heatwave dynamics"


def crossref(doi=DOI, title=TITLE, **fields):
    return {
        "DOI": doi, "title": [title], "type": "journal-article",
        "published": {"date-parts": [[2026, 10, 21]]}, **fields,
    }


def crossref_page(*items, total=None, cursor=None):
    return {"message": {"items": list(items), "total-results": len(items) if total is None else total,
                        "next-cursor": cursor}}


def openalex(doi=DOI, title=TITLE):
    return {
        "id": "https://openalex.org/W1234", "doi": "https://doi.org/" + doi.upper() if doi else None,
        "display_name": title, "publication_date": "2026-10-21", "type": "article",
        "abstract_inverted_index": {"changes": [2], "heat": [1], "Ocean": [0]},
    }


def openalex_page(*items, total=None, cursor=None):
    return {"meta": {"count": len(items) if total is None else total, "next_cursor": cursor}, "results": list(items)}


class FixtureFetcher:
    def __init__(self, sources):
        self.sources = sources
        self.urls = []

    def get(self, url, provider):
        self.urls.append(url)
        response = self.sources[provider]
        if callable(response):
            response = response(url)
        if isinstance(response, Exception):
            raise response
        return response, STAMP, False


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.profile_path = self.root / "discovery/interests.json"
        self.raw = {
            "schema_version": 1, "name": "test",
            "topics": [{"id": "heatwaves", "label": "Heatwaves", "queries": [QUERY],
                        "terms": ["marine heatwave", "dynamics"]}],
            "region_terms": ["North Atlantic"],
            "sources": {"crossref": {"enabled": True}, "openalex": {"enabled": True}, "rss": []},
        }
        self.profile = self.configure()
        self.args = SimpleNamespace(days=None, limit=5)

    def configure(self):
        discover.write_json(self.profile_path, self.raw)
        return discover.load_profile(self.profile_path)

    def scan(self, sources):
        return discover.run_scan(self.root, self.profile, self.args,
                                 clock=lambda: NOW, fetcher=FixtureFetcher(sources))

    def test_cross_source_dedup_retains_provenance_and_abstract(self):
        result = self.scan({"crossref": crossref_page(crossref()), "openalex": openalex_page(openalex())})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["new"], 1)
        work = result["results"][0]
        self.assertEqual(work["doi"], DOI)
        self.assertEqual(work["first_seen"], STAMP)
        self.assertEqual(work["abstract"], "Ocean heat changes")
        self.assertEqual(work["evidence_level"], "abstract_only")
        self.assertFalse(work["full_text_read"])
        self.assertEqual([row["source"] for row in work["sources"]], ["crossref", "openalex"])
        self.assertEqual(work["sources"][0]["source_url"], "https://doi.org/" + DOI)
        self.assertEqual(work["sources"][1]["retrieved_at"], STAMP)
        self.assertEqual(work["topic_matches"][0]["terms"], ["marine heatwave", "dynamics"])
        self.assertEqual(work["region_matches"], ["North Atlantic"])

    def test_local_match_excludes_exact_doi_and_title_only_but_keeps_other_versions(self):
        pdf = self.root / "source.pdf"
        pdf.touch()
        discover.write_json(self.root / "library/manifest.json", {"papers": [
            {"id": "P-local", "doi": DOI.upper(), "title": TITLE, "source_paths": [pdf.name]},
        ]})
        local = discover.local_index(self.root)
        self.assertEqual(discover.local_match({"title": TITLE, "doi": None}, local), "P-local")
        version = "10.1234/version2"
        result = self.scan({"crossref": crossref_page(crossref(), crossref(version)),
                            "openalex": openalex_page()})
        self.assertEqual(result["counts"]["known_local_pending"], 1)
        self.assertEqual([work["doi"] for work in result["results"]], [version])

    def test_incomplete_watermarks_and_source_coverage(self):
        self.raw["monitor"] = {"max_pages_per_query": 1}
        self.profile = self.configure()
        result = self.scan({"crossref": crossref_page(crossref(), total=50, cursor="page2"),
                            "openalex": discover.FetchError("network connection or timeout")})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage_counts"], {"complete": 0, "failed": 1, "capped": 1})
        self.assertTrue(all(item["watermark"] is None for item in result["coverage"]))
        self.assertEqual(result["counts"]["pending"], 1)
        result = self.scan({"crossref": crossref_page(crossref()), "openalex": discover.FetchError("HTTP 503")})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"][0]["watermark"], NOW.date().isoformat())
        self.assertIsNone(result["coverage"][1]["watermark"])
        result = self.scan({"crossref": discover.FetchError("HTTP 429"), "openalex": discover.FetchError("HTTP 503")})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertEqual(result["coverage"][0]["watermark"], NOW.date().isoformat())

    def test_candidate_limit_and_fairness_leave_skipped_query_unadvanced(self):
        self.raw["monitor"] = {"max_candidates": 1}
        self.profile = self.configure()
        data = {"crossref": crossref_page(crossref()), "openalex": openalex_page(openalex())}
        first = self.scan(data)
        self.assertEqual([item["status"] for item in first["coverage"]], ["complete", "capped"])
        self.assertFalse(first["coverage"][1]["watermark_advanced"])
        second = self.scan(data)
        self.assertEqual(second["coverage"][0]["source"], "openalex")
        self.assertTrue(second["coverage"][0]["watermark_advanced"])
        self.assertEqual(second["counts"]["new"], 0)

    def test_failed_initial_window_does_not_age_out(self):
        fail = discover.FetchError("HTTP 503")
        first = self.scan({"crossref": fail, "openalex": openalex_page()})
        lower = first["coverage"][0]["from"]
        self.assertEqual(first["coverage"][0]["backlog_from"], lower)
        next_week = NOW + timedelta(days=7)
        fetcher = FixtureFetcher({"crossref": crossref_page(), "openalex": openalex_page()})
        second = discover.run_scan(self.root, self.profile, self.args, clock=lambda: next_week, fetcher=fetcher)
        self.assertEqual(second["coverage"][0]["from"], lower)
        self.assertIsNone(second["coverage"][0]["backlog_from"])
        self.assertEqual(second["coverage"][0]["watermark"], next_week.date().isoformat())

    def test_local_version_update_qualifies_once(self):
        (self.root / "local.pdf").touch()
        discover.write_json(self.root / "library/manifest.json", {"papers": [
            {"id": "P-local", "doi": DOI, "title": TITLE, "source_paths": ["local.pdf"]},
        ]})
        preprint = crossref(type="posted-content")
        data = {"crossref": crossref_page(preprint), "openalex": openalex_page()}
        self.assertEqual(self.scan(data)["results"], [])
        related = {"is-preprint-of": [{"id": "10.1234/publication"}]}
        data["crossref"] = crossref_page({**preprint, "relation": related})
        changed = self.scan(data)["results"][0]
        self.assertTrue(changed["known_local"])
        self.assertEqual(changed["changed_fields"], ["related_dois"])
        self.assertEqual(changed["related_dois"], ["10.1234/publication"])
        discover.mark(self.root, changed["id"], "read")
        self.assertEqual(self.scan(data)["results"], [])

    def test_repeat_and_metadata_update_preserve_then_reset_status(self):
        original = crossref(abstract="<jats:p>North Atlantic <b>heat</b>.</jats:p>", **{"is-referenced-by-count": 3})
        data = {"crossref": crossref_page(original), "openalex": openalex_page()}
        first = self.scan(data)["results"][0]
        self.assertEqual(first["abstract"], "North Atlantic heat.")
        discover.mark(self.root, first["id"], "dismissed")
        data["crossref"] = crossref_page({**original, "is-referenced-by-count": 999})
        repeated = self.scan(data)
        self.assertEqual(repeated["counts"]["unchanged"], 1)
        self.assertFalse(repeated["results"])
        data["crossref"] = crossref_page({**original, "abstract": "Updated North Atlantic heat."})
        updated = self.scan(data)
        self.assertEqual(updated["counts"]["changed"], 1)
        self.assertEqual(updated["results"][0]["id"], first["id"])
        self.assertEqual(updated["results"][0]["first_seen"], first["first_seen"])
        self.assertEqual(updated["results"][0]["discovery_event"], "changed")
        self.assertEqual(updated["results"][0]["status"], "pending")

    def test_doi_less_record_promoted_without_collapsing_distinct_versions(self):
        data = {"crossref": crossref_page(), "openalex": openalex_page(openalex(None))}
        work = self.scan(data)["results"][0]
        self.assertIsNone(work["doi"])
        version = "10.1234/preprint"
        data["crossref"] = crossref_page(crossref(), crossref(version, type="posted-content",
                                                             relation={"is-preprint-of": [{"id": DOI}]}))
        data["openalex"] = openalex_page(openalex())
        result = self.scan(data)
        self.assertEqual({item["doi"] for item in result["results"]}, {DOI, version})
        preprint = next(item for item in result["results"] if item["doi"] == version)
        self.assertEqual(preprint["publication_status"], "preprint")
        self.assertEqual(preprint["related_dois"], [DOI])

    def test_on_demand_search_leaves_monitor_state_untouched(self):
        data = {"crossref": crossref_page(crossref()), "openalex": openalex_page()}
        args = SimpleNamespace(days=30, query=QUERY, limit=10)
        fetched = FixtureFetcher(data)
        result = discover.run_search(self.root, self.profile, args, clock=lambda: NOW, fetcher=fetched)
        self.assertEqual(result["status"], "complete")
        self.assertFalse((self.root / "discovery/state.sqlite3").exists())
        self.assertTrue(Path(result["report_path"]).exists())
        self.assertIn("from-created-date", parse_qs(urlsplit(fetched.urls[0]).query)["filter"][0])
        self.scan(data)
        state = self.root / "discovery/state.sqlite3"
        before = state.read_bytes()
        discover.run_search(self.root, self.profile, args, clock=lambda: NOW, fetcher=fetched)
        self.assertEqual(state.read_bytes(), before)

    def test_openalex_scope_changes_url_and_checkpoint_identity(self):
        self.raw["sources"]["crossref"]["enabled"] = False
        self.profile = self.configure()
        fetcher = FixtureFetcher({"openalex": openalex_page()})
        first = discover.run_scan(self.root, self.profile, self.args, clock=lambda: NOW, fetcher=fetcher)
        title_abstract = first["coverage"][0]
        self.assertEqual(parse_qs(urlsplit(fetcher.urls[-1]).query)["search.title_and_abstract"], [QUERY])
        self.assertEqual(title_abstract["search_scope"], "title_and_abstract")
        self.assertTrue(title_abstract["watermark_advanced"])

        self.raw["sources"]["openalex"]["search_scope"] = "fulltext"
        self.profile = self.configure()
        second = discover.run_scan(self.root, self.profile, self.args, clock=lambda: NOW, fetcher=fetcher)
        fulltext = second["coverage"][0]
        self.assertEqual(parse_qs(urlsplit(fetcher.urls[-1]).query)["search"], [QUERY])
        self.assertEqual(fulltext["search_scope"], "fulltext")
        self.assertNotEqual(fulltext["key"], title_abstract["key"])
        self.assertIsNone(fulltext["previous_watermark"])

    def test_search_prioritizes_novel_and_requested_region(self):
        (self.root / "local.pdf").touch()
        discover.write_json(self.root / "library/manifest.json", {"papers": [
            {"id": "P-local", "doi": DOI, "title": TITLE, "source_paths": ["local.pdf"]},
        ]})
        other_region = crossref("10.1234/other", "Kuroshio marine heatwave dynamics")
        novel = crossref("10.1234/novel", "North Atlantic marine heatwave impacts")
        data = {"crossref": crossref_page(crossref(), other_region, novel), "openalex": openalex_page()}
        args = SimpleNamespace(days=30, query=QUERY, limit=10)
        result = discover.run_search(self.root, self.profile, args, clock=lambda: NOW, fetcher=FixtureFetcher(data))
        self.assertEqual([item["doi"] for item in result["results"]], [novel["DOI"], other_region["DOI"], DOI])
        self.assertEqual(result["counts"]["known_local"], 1)

    def test_rss_description_is_not_abstract(self):
        self.raw["sources"] = {
            "rss": [{"id": "journal", "enabled": True, "url": "https://example.org/feed.xml"}],
        }
        self.profile = self.configure()
        feed = """
        <rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
        <title>North Atlantic marine heatwave dynamics</title>
        <link>https://example.org/abstract</link><dc:identifier>10.1234/abc</dc:identifier>
        <pubDate>Wed, 21 Oct 2026 08:00:00 GMT</pubDate><description>Promotional text</description>
        </item></channel></rss>
        """
        result = self.scan({"rss:journal": feed})
        work = result["results"][0]
        self.assertEqual(work["doi"], DOI)
        self.assertEqual(work["publication_date"], "2026-10-21")
        self.assertEqual(work["evidence_level"], "metadata_only")
        self.assertEqual(work["sources"][0]["source_url"], "https://example.org/abstract")

    def test_seed_lookup_when_crossref_search_disabled(self):
        self.raw["sources"]["crossref"]["search_enabled"] = False
        self.raw["required_terms"] = ["not in either title"]
        self.raw["seed_dois"] = [DOI]
        self.profile = self.configure()
        result = self.scan({"crossref": {"message": crossref()}, "openalex": openalex_page(openalex())})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertEqual(result["coverage"][1]["basis"], "tracked_doi")
        self.assertTrue(result["results"][0]["tracked_doi"])
        saved = json.loads(Path(result["report_path"]).read_text())
        self.assertEqual(saved["results"][0]["id"], result["results"][0]["id"])


if __name__ == "__main__":
    unittest.main()
