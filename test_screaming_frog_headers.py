"""Regression tests for standard Screaming Frog export headers."""

import unittest

from audit import read_pages, read_inlinks


class ScreamingFrogHeaderTests(unittest.TestCase):
    def test_internal_export_status_code_takes_precedence_over_status(self):
        data = ("Address,Content Type,Status Code,Status,Indexability,Title 1,H1-1\n"
                "https://example.com/a,text/html,200,OK,Indexable,Example,Example\n"
                "https://example.com/b,text/html,404,Not Found,Non-Indexable,Oops,Oops\n")
        pages = read_pages(data)
        self.assertEqual(pages["https://example.com/a"].status, 200)
        self.assertEqual(pages["https://example.com/b"].status, 404)

    def test_all_inlinks_destination_takes_precedence_over_html_target(self):
        data = ("Type,Source,Destination,Target,Anchor Text,Link Position\n"
                "Hyperlink,https://example.com/a,https://example.com/b,_blank,Example,Content\n"
                "Hyperlink,https://example.com/c,https://example.com/b,_self,More,Navigation\n")
        links = read_inlinks(data)
        self.assertEqual(links.edges, {("https://example.com/a", "https://example.com/b"),
                                       ("https://example.com/c", "https://example.com/b")})

    def test_full_header_crawl_round_trip(self):
        from pathlib import Path
        from audit import audit
        from link_worklist import verify, worklist_csv
        root = Path(__file__).with_name("examples") / "sprint"
        def sample(name):
            return (root / name).read_text(encoding="utf-8")
        report = audit(sample("links.csv"), sample("pages.csv"),
                       sample("crawl-pages-full-headers.csv"),
                       sample("crawl-inlinks-before-full-headers.csv"),
                       priority_urls=["https://example.com/services/heat-pump-installation"],
                       snippets_csv=sample("snippets.csv"))
        additions = [t for t in report["worklist_tasks"] if t["action"] == "add_link"]
        self.assertEqual(len(additions), 2)
        result = verify(worklist_csv(report["worklist_tasks"]).decode("utf-8-sig"),
                        sample("crawl-pages-full-headers.csv"),
                        sample("crawl-inlinks-after-full-headers.csv"))
        self.assertEqual(sum(t["verification_status"] == "verified" for t in result), 1)

    def test_legacy_fallback_and_genuine_ambiguity(self):
        pages = read_pages("Address,Status,Indexability\nhttps://example.com/a,200,Indexable\n")
        self.assertEqual(pages["https://example.com/a"].status, 200)
        links = read_inlinks("Source,Target\nhttps://example.com/a,https://example.com/b\n")
        self.assertIn(("https://example.com/a", "https://example.com/b"), links.edges)
        with self.assertRaisesRegex(ValueError, "ambiguous columns for status"):
            read_pages("Address,Status Code,HTTP Status,Status,Indexability\n"
                       "https://example.com/a,200,200,OK,Indexable\n")
        with self.assertRaisesRegex(ValueError, "ambiguous columns for target"):
            read_inlinks("Source,Destination,Destination URL,Target\n"
                         "https://example.com/a,https://example.com/b,https://example.com/b,_blank\n")


if __name__ == "__main__":
    unittest.main()
