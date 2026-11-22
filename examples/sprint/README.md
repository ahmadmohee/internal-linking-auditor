# Internal linking sprint demo

This synthetic case starts with a high-priority heat pump installation service page. The
Search Console exports show 18,400 impressions and 276 sampled internal links. The supplied
crawl observes navigation links from nine distinct pages and zero links from main content.

## Generate the Excel report

Run from the project root.

```sh
python3 audit.py --website https://example.com \
  --links examples/sprint/links.csv --performance examples/sprint/pages.csv \
  --crawl-pages examples/sprint/crawl-pages.csv \
  --crawl-inlinks examples/sprint/crawl-inlinks-before.csv \
  --priority-url https://example.com/services/heat-pump-installation \
  --priority-url https://example.com/services/heat-pump-maintenance \
  --snippets examples/sprint/snippets.csv \
  --page-queries examples/sprint/queries.csv \
  --redirects examples/sprint/redirects.csv --excel sprint-audit.xlsx
```

| Source | Section | Anchor |
| --- | --- | --- |
| Heat pump running costs guide | Upfront costs | heat pump installation |
| Heat pump sizing guide | Choose the right capacity | heat pump installation for your home |

The sentences and editing instructions appear on the Audit and fixes worksheet. The maintenance page also appears as a client
priority with no Performance Pages row. The workbook includes a broken destination linked by two
guides and a redirect linked from a possible shared footer.

## Verify on a fresh crawl

```sh
python3 audit.py --verify sprint-audit.xlsx \
  --crawl-pages examples/sprint/crawl-pages.csv \
  --crawl-inlinks examples/sprint/crawl-inlinks-after.csv \
  --verification-excel sprint-verification.xlsx
```

The running costs guide has a new **Content** link with the expected anchor, so its task is
**verified**. The sizing guide’s old navigation link is still the only observed link on that
source to the service page, so its content-link task is **Still open**. The same files can
be attached in ChatGPT to demonstrate the two workflows.
