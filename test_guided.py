import asyncio
import io
import unittest
from collections import OrderedDict
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx
from starlette.applications import Starlette
from starlette.requests import Request

from audit import audit
from link_report import LOGO_PATH, SPREADSHEET_NS, XLSX_MIME_TYPE, action_rows, create_workbook
import server
from test_audit import KNOWN_SOURCE, TARGET, sample


class GuidedAuditTests(unittest.TestCase):
    def test_generic_anchor_review_uses_only_content_text_links(self):
        links = sample("crawl-inlinks.csv")
        links += f"Hyperlink,https://example.com/,{TARGET},Read more,Navigation\n"
        result = audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"), links)
        self.assertEqual(result["profile"]["crawl_anchor_improvements"], 1)
        fix = result["crawl_anchor_improvements"][0]
        self.assertEqual((fix["source_url"], fix["target_url"]), (KNOWN_SOURCE, TARGET))
        self.assertEqual((fix["current_anchor"], fix["suggested_anchor"]),
                         ("Read more", "Waterproof Trail Running Shoes"))
        image_links = (
            "Source,Destination,Anchor Text,Link Position,Alt Text\n"
            f"{KNOWN_SOURCE},{TARGET},اضغط هنا,Content,Waterproof trail running shoes\n"
        )
        result = audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"), image_links)
        self.assertEqual(result["profile"]["crawl_anchor_improvements"], 0)
        text_links = image_links.replace(",Waterproof trail running shoes", ",")
        result = audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"), text_links)
        self.assertEqual(result["crawl_anchor_improvements"][0]["current_anchor"], "اضغط هنا")

    def test_start_here_distinguishes_evidence(self):
        result = server.audit_internal_link_csv(sample("links.csv"), sample("pages.csv"))
        self.assertEqual(result["start_here"][0]["action"], "review_candidate")
        self.assertEqual(result["start_here"][0]["priority"], "Medium")
        result = server.audit_internal_link_csv(sample("links.csv"), sample("pages.csv"),
                                               sample("crawl-pages.csv"), sample("crawl-inlinks.csv"))
        self.assertEqual([item["action"] for item in result["start_here"][:3]],
                         ["repair_broken", "replace_redirect", "improve_anchor"])

        legacy = server.audit_internal_link_csv(sample("links.csv"), sample("pages.csv"),
                                              include_excel=False, include_worklist=False)
        self.assertEqual(legacy["start_here"][0]["action"], "Verify source first")

    def test_setup_asks_only_for_missing_answers(self):
        setup = server.start_internal_linking_audit("example.com")
        self.assertEqual(setup["website_url"], "https://example.com/")
        self.assertEqual(len(setup["ask_only_if_missing"]), 1)
        self.assertEqual(len(setup["required_uploads"]), 2)
        self.assertEqual(server.start_internal_linking_audit("example.com", [])["ask_only_if_missing"], [])

    def test_scope_and_focus_with_cross_directory_sources(self):
        result = audit(sample("links.csv"), sample("pages.csv"),
                       website_url="https://www.example.com/shoes/", focus_keywords=[
                           "waterproof trail running shoes", "luxury handbags",
                       ])
        target = result["priority_targets_with_observed_low_counts"][0]
        self.assertEqual(target["target_url"], TARGET)
        self.assertEqual(target["matched_focus_keywords"], ["waterproof trail running shoes"])
        self.assertIn("/guides/", target["candidate_sources"][0]["source_url"])
        self.assertFalse(any("road-running-shoes" in candidate["source_url"]
                             for candidate in target["candidate_sources"]))
        self.assertEqual(result["focus_keywords_without_a_visible_page_match"], ["luxury handbags"])
        self.assertEqual(action_rows(result)[0][4], "waterproof trail running shoes")
        with self.assertRaisesRegex(ValueError, "no URLs under"):
            audit(sample("links.csv"), sample("pages.csv"), website_url="other.example")
        with self.assertRaisesRegex(ValueError, "Internal Links export contains no URLs"):
            audit(sample("links.csv").replace("example.com", "other.example"), sample("pages.csv"),
                  website_url="example.com")

    def test_arabic_focus_preserves_natural_anchor(self):
        target = "https://example.com/أرخص-تذاكر-طيران"
        source = "https://example.com/دليل-تذاكر-طيران"
        links = f"Target page,Internal links\n{target},1\n{source},10\n"
        pages = f"Top pages,Clicks,Impressions\n{target},20,400\n{source},50,1000\n"
        result = audit(links, pages, website_url="example.com", focus_keywords=["أرخص تذاكر طيران"])
        first = action_rows(result)[0]
        self.assertEqual((first[2], first[3], first[4]), (target, source, "أرخص تذاكر طيران"))

    def test_workbook_rows_and_literal_user_text(self):
        result = audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"),
                       sample("crawl-inlinks.csv"), focus_keywords=["=1+1"], include_worklist=True)
        rows = action_rows(result)
        self.assertNotIn(KNOWN_SOURCE, [row[3] for row in rows if row[2] == TARGET])
        self.assertTrue(any(row[8] is None for row in rows))
        namespaces = {"s": SPREADSHEET_NS}
        tasks = result["worklist_tasks"]
        tasks[0]["evidence"] = "=1+1"
        with ZipFile(io.BytesIO(create_workbook(result, tasks))) as workbook:
            self.assertIsNone(workbook.testzip())
            for name in workbook.namelist():
                if name.endswith(('.xml', '.rels')):
                    ET.fromstring(workbook.read(name))
            manifest = ET.fromstring(workbook.read("xl/workbook.xml"))
            self.assertEqual(len(manifest.findall(".//s:sheet", namespaces)), 1)
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            values = [node.text for node in sheet.findall(".//s:t", namespaces)]
            self.assertTrue(any(value and "=1+1" in value for value in values))
            self.assertIn("How to fix it", values)
            self.assertEqual(sheet.findall(".//s:f", namespaces), [])
            links = ET.fromstring(workbook.read("xl/worksheets/_rels/sheet1.xml.rels"))
            self.assertTrue(any(link.get("Target") == TARGET for link in links))
            self.assertTrue(any(link.get("Target") == "mailto:ahmadmohee@gmail.com"
                                and link.get("TargetMode") == "External" for link in links))
            self.assertTrue(any(link.get("Target") == "../drawings/drawing1.xml" for link in links))
            self.assertEqual(workbook.read("xl/media/logo.png"), LOGO_PATH.read_bytes())
            self.assertEqual(sheet.findtext(".//s:c[@r='A3']/s:is/s:t", namespaces=namespaces),
                             "This plugin was developed by Ahmad Mohee")
            expert_contact = sheet.findall(".//s:c[@r='A4']/s:is/s:r", namespaces)
            self.assertEqual("".join(run.findtext("s:t", namespaces=namespaces) for run in expert_contact),
                             "Need an expert to perform your audit? Email me at ahmadmohee@gmail.com "
                             "with your website URL and the pages you want to improve.")
            self.assertTrue(all(expert_contact[i].find("s:rPr/s:b", namespaces) is not None for i in (0, 2)))

    def test_workbook_start_here_with_medium_priority_only(self):
        result = audit(sample("links.csv"), sample("pages.csv"), include_worklist=True)
        tasks = result["worklist_tasks"]
        self.assertEqual({task["priority"] for task in tasks}, {"Medium"})
        with ZipFile(io.BytesIO(create_workbook(result, tasks))) as workbook:
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        start_here = sheet.findtext(".//s:c[@r='C7']/s:is/s:t", namespaces={"s": SPREADSHEET_NS})
        self.assertEqual(start_here, "Start at the top of the table. Open Page to edit and follow How to fix it.")


class ExcelDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(server, "PUBLIC_HOST", "reports.example"))
        self.enterContext(patch.object(server, "REPORTS", OrderedDict()))
        self.enterContext(patch.object(server.mcp, "streamable_http_app", side_effect=Starlette))

    async def test_download_headers_and_expiry(self):
        with patch("server.monotonic", return_value=100.0):
            result = server.audit_internal_link_csv(
                sample("links.csv"), sample("pages.csv"), website_url="example.com",
                focus_keywords=["trail running shoes"], include_excel=True,
            )
        path = urlsplit(result["excel_download"]["url"]).path
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()),
                                     base_url="http://127.0.0.1") as client:
            with patch("server.monotonic", return_value=101.0):
                response = await client.get(path)
                self.assertEqual(response.headers["content-type"], XLSX_MIME_TYPE)
                self.assertIn(".xlsx", response.headers["content-disposition"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(int(response.headers["content-length"]), len(response.content))
                self.assertTrue(response.content.startswith(b"PK"))
            with patch("server.monotonic", return_value=1001.0):
                self.assertEqual((await client.get(path)).status_code, 404)

    async def test_report_count_and_size_limits_close_evicted_files(self):
        app = server.create_app()
        async with app.router.lifespan_context(app):
            for _ in range(100):
                server.attach_report({}, "excel", b"sample", "audit.xlsx", XLSX_MIME_TYPE)
            self.assertEqual(len(server.REPORTS), 100)
            oldest = next(iter(server.REPORTS.values()))
            server.attach_report({}, "excel", b"new", "audit.xlsx", XLSX_MIME_TYPE)
            self.assertTrue(oldest.file.closed)
            self.assertEqual(len(server.REPORTS), 100)
            with patch.object(server, "MAX_REPORT_BYTES", 10):
                server.attach_report({}, "excel", b"x" * 8, "audit.xlsx", XLSX_MIME_TYPE)
                self.assertEqual(sum(item.size for item in server.REPORTS.values()), 8)

    async def test_open_downloads_complete_after_eviction(self):
        payload = b"A" * server.REPORT_CHUNK_SIZE + b"B" * server.REPORT_CHUNK_SIZE + b"end"
        app = server.create_app()
        self.enterContext(patch.object(server, "MAX_REPORTS", 1))
        async with app.router.lifespan_context(app):
            server.attach_report({}, "excel", payload, "audit.xlsx", XLSX_MIME_TYPE)
            token, cached = next(iter(server.REPORTS.items()))
            request = Request({"type": "http", "path_params": {"token": token, "extension": "xlsx"}})
            first = server.download_report(request)
            second = server.download_report(request)
            server.attach_report({}, "excel", b"replacement", "audit.xlsx", XLSX_MIME_TYPE)
            self.assertTrue(cached.file.closed)

            async def collect(response):
                received = []

                async def send(message):
                    if message["type"] == "http.response.body":
                        received.append(message["body"])
                    await asyncio.sleep(0)

                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
                self.assertTrue(response.file.closed)
                return b"".join(received)

            self.assertEqual(await asyncio.gather(collect(first), collect(second)), [payload, payload])

    async def test_expired_reports_close_when_idle_and_at_shutdown(self):
        app = server.create_app()
        with patch.object(server, "REPORT_CLEANUP_INTERVAL", 0.005), patch("server.monotonic", return_value=100.0) as clock:
            async with app.router.lifespan_context(app):
                server.attach_report({}, "excel", b"expired", "audit.xlsx", XLSX_MIME_TYPE)
                expired = next(iter(server.REPORTS.values()))
                clock.return_value = 1001.0

                async def wait_for_expiry():
                    while not expired.file.closed:
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(wait_for_expiry(), timeout=1)
                self.assertFalse(server.REPORTS)
                server.attach_report({}, "excel", b"live", "audit.xlsx", XLSX_MIME_TYPE)
                live = next(iter(server.REPORTS.values()))
                self.assertFalse(live.file.closed)
            self.assertTrue(live.file.closed)
