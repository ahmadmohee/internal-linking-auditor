import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from audit import audit, normal_url
from server import allowed_attachment_url, get_attachment, mcp

EXAMPLES = Path(__file__).with_name("examples")
TARGET = "https://example.com/shoes/waterproof-trail-running-shoes"
KNOWN_SOURCE = "https://example.com/guides/waterproof-trail-running-shoes"
CHECKLIST = "https://example.com/guides/trail-running-waterproof-checklist"


def sample(name: str) -> str:
    return (EXAMPLES / name).read_text(encoding="utf-8")


class AuditTests(unittest.TestCase):
    def test_console_only_has_separate_unknown_counts(self):
        result = audit(sample("links.csv"), sample("pages.csv"))
        self.assertEqual(result["mode"], "search_console_only")
        top = result["priority_targets_with_observed_low_counts"][0]
        self.assertEqual((top["target_url"], top["gsc_reported_internal_links"]), (TARGET, 1))
        self.assertTrue(top["candidate_sources"])
        self.assertTrue(all(s["existing_source_to_target_link"] == "unknown"
                            for s in top["candidate_sources"]))
        missing = result["coverage_checks_for_unreported_targets"][0]
        self.assertIsNone(missing["gsc_reported_internal_links"])

    def test_crawl_excludes_observed_links_and_other_locales(self):
        result = audit(sample("links.csv"), sample("pages.csv"),
                       sample("crawl-pages.csv"), sample("crawl-inlinks.csv"))
        top = result["priority_targets_with_observed_low_counts"][0]
        self.assertEqual(top["crawl_distinct_linking_urls"], 1)
        sources = {entry["source_url"] for entry in top["candidate_sources"]}
        self.assertIn(CHECKLIST, sources)
        self.assertNotIn(KNOWN_SOURCE, sources)
        self.assertFalse(any("/fr/" in source for source in sources))
        self.assertEqual([issue["issue"] for issue in result["crawl_source_link_fixes"]],
                         ["link_to_error", "link_to_redirect"])

    def test_crawl_suppresses_recommendations_to_redirected_targets(self):
        pages = sample("crawl-pages.csv").replace(
            f"{TARGET},text/html,200,Indexable,", f"{TARGET},text/html,301,Non-Indexable,",
        )
        result = audit(sample("links.csv"), sample("pages.csv"), pages, sample("crawl-inlinks.csv"))
        top = result["priority_targets_with_observed_low_counts"][0]
        self.assertFalse(top["eligible_in_crawl"])
        self.assertEqual(top["candidate_sources"], [])

    def test_report_formats_and_guardrails(self):
        with self.assertRaisesRegex(ValueError, "both crawl exports"):
            audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"))
        with self.assertRaisesRegex(ValueError, "Internal Links CSV is missing"):
            audit("Target page,Incoming links,Linking sites\n", sample("pages.csv"))
        with self.assertRaisesRegex(ValueError, "Conflicting rows"):
            audit(sample("links.csv") + f"{TARGET},10\n", sample("pages.csv"))
        self.assertEqual(normal_url("https://EXAMPLE.com:443/a/?b=2&a=1#first"),
                         "https://example.com/a/?b=2&a=1")
        self.assertNotEqual(normal_url("https://example.com/a"), normal_url("https://example.com/a/"))


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_schema(self):
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        tool = tools["audit_internal_link_files"]
        self.assertIn("links_file", tool.meta["openai/fileParams"])
        descriptor = tool.inputSchema["$defs"]["ChatGPTFile"]
        self.assertEqual(set(descriptor["required"]), {"download_url", "file_id"})
        self.assertEqual(set(descriptor["properties"]),
                         {"download_url", "file_id", "mime_type", "file_name"})

    async def test_download_stream_and_deny_redirects(self):
        url = "https://files.oaiusercontent.com/test?sig=private"
        file = {"download_url": url, "file_id": "file-example"}

        def sample_response(request):
            self.assertEqual(str(request.url), url)
            return httpx.Response(200, stream=httpx.ByteStream(sample("links.csv").encode()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(sample_response)) as client:
            self.assertEqual(await get_attachment(client, file, "Test CSV"), sample("links.csv"))

        def redirect_response(request):
            return httpx.Response(302, headers={"Location": "https://example.org/forbidden"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_response)) as client:
            with self.assertRaisesRegex(ValueError, "could not be opened") as error:
                await get_attachment(client, file, "Test CSV")
            self.assertNotIn("private", str(error.exception))

    async def test_attachment_hosts(self):
        for region in ("ukwest", "denmarkeast", "eastus2"):
            self.assertTrue(allowed_attachment_url(f"https://sdmntpr{region}.oaiusercontent.com/test"))
        for url in (
            "http://files.oaiusercontent.com/test",
            "https://files.oaiusercontent.com.evil.example/test",
            "https://user@files.oaiusercontent.com/test",
            "https://files.oaiusercontent.com:8443/test",
            "https://127.0.0.1/test",
            "http://sdmntprukwest.oaiusercontent.com/test",
            "https://sdmntprukwest.oaiusercontent.com:8443/test",
            "https://sdmntprukwest.oaiusercontent.com.example.org/test",
            "https://nested.sdmntprukwest.oaiusercontent.com/test",
            "https://sdmntpr.oaiusercontent.com/test",
            "https://other.oaiusercontent.com/test",
        ):
            self.assertFalse(allowed_attachment_url(url))
        with patch.dict(os.environ, {"SEO_ATTACHMENT_HOSTS": "test.oaiusercontent.com,example.org"}):
            self.assertTrue(allowed_attachment_url("https://test.oaiusercontent.com/file"))
            self.assertFalse(allowed_attachment_url("https://example.org/file"))


if __name__ == "__main__":
    unittest.main()
