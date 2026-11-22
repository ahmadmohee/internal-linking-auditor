import csv
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import server
from audit import audit
from link_report import XLSX_MIME_TYPE, workbook_worklist
from link_worklist import OUTSIDE, read_worklist, verify, worklist_csv

DEMO = Path(__file__).parent / "examples" / "sprint"
HOST = "https://example.com"
TARGET = HOST + "/services/heat-pump-installation"
NEW = HOST + "/services/heat-pump-maintenance"
FIRST = HOST + "/guides/heat-pump-running-costs"
SECOND = HOST + "/guides/heat-pump-sizing"


def sample(name):
    return (DEMO / name).read_text()


def demo(**kwargs):
    options = dict(
        website_url=HOST, priority_urls=[TARGET, NEW], snippets_csv=sample("snippets.csv"),
        redirects_csv=sample("redirects.csv"), page_queries_csv=sample("queries.csv"),
    )
    options.update(kwargs)
    return audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"),
                 sample("crawl-inlinks-before.csv"), **options)


class WorklistTests(unittest.TestCase):
    def test_demo_placement_priority_repair_and_verification(self):
        report = demo()
        target, new_page = report["contextual_link_opportunities"][:2]
        self.assertEqual(target["target_url"], TARGET)
        self.assertEqual(target["search_impressions"], 18400)
        self.assertEqual(target["gsc_reported_internal_links"], 276)
        self.assertEqual(target["crawl_content_linking_urls"], 0)
        self.assertEqual(target["crawl_navigation_linking_urls"], 9)
        self.assertEqual(new_page["target_url"], NEW)
        self.assertIsNone(new_page["search_impressions"])
        tasks = report["worklist_tasks"]
        additions = [task for task in tasks if task["action"] == "add_link"]
        self.assertEqual({task["source_url"] for task in additions}, {FIRST, SECOND})
        self.assertTrue(all(task["priority"] == "High" and task["baseline_pair"] == "yes"
                            and task["anchor_edit"] == "already in sentence"
                            and task["anchor"] in task["sentence"] for task in additions))
        self.assertIn("heat pump installation for your home", {task["anchor"] for task in additions})
        repair = next(task for task in tasks if task["action"] == "repair_broken")
        self.assertEqual(repair["affected_urls"], [FIRST, SECOND])
        redirect = next(task for task in tasks if task["action"] == "replace_redirect")
        self.assertEqual((redirect["replacement_url"], redirect["final_http_status"]), (TARGET, 200))
        self.assertIn("shared footer", redirect["section"])
        worklist = worklist_csv(tasks).decode("utf-8-sig")
        self.assertEqual([task["task_id"] for task in read_worklist(worklist)],
                         [task["task_id"] for task in demo()["worklist_tasks"]])
        verified = verify(worklist, sample("crawl-pages.csv"), sample("crawl-inlinks-after.csv"))
        statuses = {task["source_url"]: task["verification_status"] for task in verified if task["action"] == "add_link"}
        self.assertEqual(statuses, {FIRST: "verified", SECOND: "still present"})
        self.assertEqual(sum(task["verification_status"] == "verified" for task in verified), 1)

    def test_pasted_snippet_can_propose_small_copy_edit(self):
        report = demo(snippets_csv=None, main_content=[{
            "url": FIRST, "heading": "Costs",
            "text": "Ask about installation costs when comparing heat pump options.",
        }], page_queries_csv=None)
        task = next(task for task in report["worklist_tasks"] if task["action"] == "add_link")
        self.assertEqual(task["anchor_edit"], "small edit")
        self.assertEqual(task["sentence"], "Ask about installation costs when comparing heat pump options.")

    def test_verification_distinguishes_missing_source_and_unknown_position(self):
        worklist = worklist_csv([task for task in demo()["worklist_tasks"]
                                 if task["action"] == "add_link"]).decode("utf-8-sig")
        bare = "Source,Destination\n" + f"{FIRST},{TARGET}\n"
        tasks = verify(worklist, sample("crawl-pages.csv"), bare)
        self.assertTrue(all(task["verification_status"] == OUTSIDE for task in tasks))
        menus = verify(worklist, sample("crawl-pages.csv"), sample("crawl-inlinks-before.csv"))
        self.assertTrue(all(task["verification_status"] == "still present" for task in menus))

    def test_missing_position_stays_unknown_and_two_csv_audit_works(self):
        links = "Source,Destination\n" + f"{FIRST},{TARGET}\n"
        report = audit(sample("links.csv"), sample("pages.csv"), sample("crawl-pages.csv"), links,
                       priority_urls=[TARGET], include_worklist=True)
        self.assertIsNone(report["contextual_link_opportunities"][0]["crawl_content_linking_urls"])
        report = audit(sample("links.csv"), sample("pages.csv"), include_worklist=True)
        self.assertEqual(report["mode"], "search_console_only")
        self.assertTrue(all(task["action"] == "review_candidate" for task in report["worklist_tasks"]))

    def test_csv_escapes_spreadsheet_formulas_and_round_trips(self):
        task = next(task for task in demo()["worklist_tasks"] if task["action"] == "add_link")
        task["section"] = "=1+1"
        task["anchor"] = "'=1+1"
        output = worklist_csv([task]).decode("utf-8-sig")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["section"], "'=1+1")
        self.assertEqual(read_worklist(output)[0]["anchor"], "'=1+1")

    def test_single_page_query_and_invalid_redirect_destination(self):
        report = demo(page_queries_csv="Top queries,Clicks,Impressions\nheat pump installation services,1,70\n",
                      queries_page_url=TARGET,
                      redirects_csv=sample("redirects.csv").replace(",200,OK,", ",404,Not Found,"))
        target = report["contextual_link_opportunities"][0]
        self.assertEqual(target["supporting_queries"], ["heat pump installation services"])
        redirect = next(task for task in report["worklist_tasks"] if task["action"] == "replace_redirect")
        self.assertEqual((redirect["replacement_url"], redirect["final_url"], redirect["final_http_status"]),
                         ("", TARGET, 404))


class WorkflowTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_and_verification_routes(self):
        files = {
            key: {"download_url": f"https://files.oaiusercontent.com/{name}", "file_id": f"file-{key}"}
            for key, name in {
                "links_file": "links.csv", "performance_file": "pages.csv",
                "crawl_pages_file": "crawl-pages.csv", "crawl_inlinks_file": "crawl-inlinks-before.csv",
                "snippets_file": "snippets.csv",
            }.items()
        }

        uploaded = {}

        async def attachments(_, file, label, *, binary=False):
            if not file:
                return None
            filename = urlsplit(file["download_url"]).path[1:]
            return uploaded[filename] if binary else sample(filename)

        app = server.create_app()
        with patch.object(server, "PUBLIC_HOST", "reports.example"), patch.object(server, "get_attachment", attachments):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://localhost:8000", trust_env=False) as http:
                    async with streamable_http_client("http://localhost:8000/mcp", http_client=http) as (
                        receive, send, _
                    ):
                        async with ClientSession(receive, send) as client:
                            await client.initialize()
                            listed = await client.list_tools()
                            self.assertEqual(len(listed.tools), 5)
                            tool = next(tool for tool in listed.tools if tool.name == "audit_internal_link_files")
                            self.assertIn("snippets_file", tool.meta["openai/fileParams"])
                            response = await client.call_tool("audit_internal_link_files", {
                                **files, "website_url": HOST, "priority_urls": [TARGET], "include_excel": True,
                            })
                            self.assertFalse(response.isError)
                            result = response.structuredContent
                            self.assertNotIn("worklist_download", result)
                            path = urlsplit(result["excel_download"]["url"]).path
                            workbook = await http.get(path)
                            self.assertEqual(workbook.headers["content-type"], XLSX_MIME_TYPE)
                            self.assertTrue(workbook.content.startswith(b"PK"))
                            self.assertEqual((await http.get(path.replace(".xlsx", ".csv"))).status_code, 404)
                            tasks = read_worklist(workbook_worklist(workbook.content))
                            self.assertEqual(sum(task["action"] == "add_link" for task in tasks), 2)
                            uploaded["audit.xlsx"] = workbook.content
                            checked = await client.call_tool("verify_internal_link_edits_files", {
                                "worklist_file": {"download_url": "https://files.oaiusercontent.com/audit.xlsx", "file_id": "workbook"},
                                "crawl_pages_file": files["crawl_pages_file"],
                                "crawl_inlinks_file": {"download_url": "https://files.oaiusercontent.com/crawl-inlinks-after.csv", "file_id": "fresh"},
                            })
                            self.assertFalse(checked.isError)
                            outcome = checked.structuredContent
                            self.assertEqual(outcome["verification_summary"]["verified"], 1)
                            downloaded = await http.get(urlsplit(outcome["verification_download"]["url"]).path)
                            self.assertEqual(downloaded.headers["content-type"], XLSX_MIME_TYPE)
                            verified_tasks = read_worklist(workbook_worklist(downloaded.content))
                            self.assertEqual([task["task_id"] for task in tasks], [task["task_id"] for task in verified_tasks])
                            broken = next(task for task in verified_tasks if task["action"] == "repair_broken")
                            self.assertEqual(broken["affected_urls"], [FIRST, SECOND])
