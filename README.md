# Internal Linking Auditor

<img src="plugin/assets/logo.png" alt="Internal Linking Auditor logo" width="84">

A ChatGPT Plugin to help you audit and optimize your website internal linking profile.

Run a guided internal linking audit using Google Search Console and optional Screaming Frog exports. Prioritize the pages that matter to the business, find contextual links with suggested anchors, and give the editor or developer one Excel sheet of specific instructions. Re-upload the workbook with a fresh crawl to see which edits were confirmed.

[LinkedIn Pulse guide to calling the plugin, uploading exports, and using the report](https://www.linkedin.com/pulse/internal-link-audit-search-console-chatgpt-linking-auditor-mohee-0clle)

## Developer and expert audits

Developed by **Ahmad Mohee**, an SEO specialist.

**Need your audit performed by an expert?** Contact Ahmad Mohee at [ahmadmohee@gmail.com](mailto:ahmadmohee@gmail.com).
I can help review your internal-link opportunities, choose the pages worth linking to first, and turn the findings into an editing plan for your team.

- [Ahmad Mohee on LinkedIn](https://www.linkedin.com/in/ahmadmohee/)

## Questions you can ask in ChatGPT

- Which important pages on my website have too few internal links?
- Where should I add internal links, and what anchor text should I use?
- Which internal links point to a 404 or a redirect, and what should I replace them with?
- Which important pages have plenty of menu links and few links from main content?
- Which internal linking fixes from my last Excel report are in the new crawl?

Looking for orphan pages? The tool lists URLs missing from the Search Console Links report for follow-up. If a URL is absent from that sampled report, its link count is unknown. Review the suggested links on your live pages before editing.

## Start a guided audit

Select Internal Linking Auditor in ChatGPT and ask **“Start an internal linking audit.”** Provide
your website URL, up to 10 focus phrases or a general audit, and the two CSVs below. Use a
section URL such as `https://example.com/shoes/` to limit targets while considering source
pages elsewhere on the same site.

ChatGPT returns one Excel workbook with one worksheet, **Audit and fixes**. The first rows explain
what the audit found and how to use the report. Start with **High** priority, open **Page to edit**,
and follow **How to fix it**. The report shows the destination and, when supplied, the exact section,
sentence and words to link. **What the audit found** explains the evidence. Update **Status** after
publishing. For shared repairs, check the affected pages listed below the main table.
The worksheet shows the plugin logo and developer credit at the top, with a clickable email for expert audit support directly below the title. ChatGPT includes the same contact at the end of its findings.

## Search Console exports

Use two UTF-8 CSV files from the same property.

1. **Links → Internal links → Top linked pages → More → Export.** The expected columns are `Target page` and `Internal links`. Export from the internal links detail table.
2. **Performance → Search results → Export → Download CSV.** Choose a single date range for Web search, then extract `Pages.csv` from the ZIP. The expected columns are `Top pages`, `Clicks`, `Impressions`, and optional `Position`. A recent 90-day window is a useful starting point.

Use English headers or rename the relevant columns to the names above. Comma, semicolon and tab delimiters are accepted. Google limits exported tables and samples link data. URLs absent from the Internal Links export are reported as **unreported** with an unknown count. The Links report can contain older links and canonicalized target URLs.

## Optional crawl evidence

Use two exports from the same completed crawl with internal hyperlinks enabled.

- **Screaming Frog → Internal → All → Export.** Include `Address`, `Status Code`, and `Indexability`. Include `Content Type`, `Title 1`, `H1-1`, and `Canonical Link Element 1` when available. Use the combined Internal export so redirect and error destinations are available for the repair list.
- **Screaming Frog → Bulk Export → Links → All Inlinks.** Include `Source`, `Destination`, and `Type` when present. Include `Anchor Text` (or `Anchor`) and `Link Position` to review generic anchors in page content. Keep `Alt Text` when present so image links can be excluded. When there is no type column, provide hyperlink rows only.

Keep `Link Path` when available. Repeated navigation or footer links sharing a path and anchor
are flagged as possible shared component edits, with affected source URLs listed in the workbook.
Validate the component in your CMS before making the change.

### Give the audit more context

| Optional input | How to provide it |
| --- | --- |
| Client priority URLs | Paste up to 50 absolute category, product or service URLs inside the audited scope. New pages can be included before they appear in Performance Pages. |
| Main-content snippets | Use a UTF-8 CSV with `URL`, optional `Heading`, and `Text`. Use one row per section, quoting multiline cells. Screaming Frog Custom Extraction with `Address` and a field named `Main Content` is also accepted. Extract only the relevant main body as text. You can also paste short sections alongside their source URLs and headings in chat. |
| Search Console page queries | Upload a CSV with `Page` and `Query`, with `Clicks` and `Impressions` if available. For a standard `Queries.csv`, filter Performance to a single exact page before exporting and tell ChatGPT that page URL. |
| Redirect destinations | Under `Reports → Redirects → All Redirects`, export the CSV with `Address`, `Final Address` and `Final Status Code`. A replacement is proposed when the report agrees with the Internal crawl on an eligible same-origin HTTP 200 destination. Confirm the final URL matches the page’s intent. |

Distinct crawl source counts are shown for main content, navigation, footer and unclassified
links next to Google’s sampled count. A source can count in multiple positions. Unknown
positions and gaps in crawl coverage can hide contextual links. Snippet-supported tasks
can recommend a body link when the pair already occurs in navigation or a footer.

### Verify edits after deployment

Save the Excel audit report. After publishing, upload the workbook and fresh **Internal pages**
and **All Inlinks** CSVs from the same crawl. Include link text and link position when possible.
Ask **“Verify the fixes in this report against the new crawl.”** The follow-up Excel report marks
each task **Verified**, **Still open**, or **Check manually** and explains what the crawl found.
The tool also accepts legacy worklist CSVs for verification.

For additions, a crawl can confirm the link between the two pages and the anchor or content position
when supplied. Open the source page to confirm the precise sentence placement. If the same link
already existed in a menu or footer, include link-position evidence in the new crawl.

## Run the example locally

Requires Python 3.10 or later.

```sh
python3 audit.py --links examples/links.csv --performance examples/pages.csv
```

To add crawl evidence:

```sh
python3 audit.py --links examples/links.csv --performance examples/pages.csv \
  --crawl-pages examples/crawl-pages.csv --crawl-inlinks examples/crawl-inlinks.csv
```

For the Excel report and follow-up crawl demo, see [Sprint demo](examples/sprint/README.md).

The original synthetic example prioritizes a product category with 13,620 impressions and one reported internal link. The crawl confirms an existing link from one guide, excludes that guide from suggestions, surfaces other related pages, and identifies one observed link to a redirect and another to a 404.

To try the workbook locally:

```sh
python3 audit.py --website https://example.com --keyword "waterproof trail running shoes" \
  --links examples/links.csv --performance examples/pages.csv \
  --crawl-pages examples/crawl-pages.csv --crawl-inlinks examples/crawl-inlinks.csv \
  --excel internal-linking-action-plan.xlsx
```

## How prioritization works

- On pages with search impressions, an observed count of 0 or 1 triggers a review. With at least five matched pages, counts at or below the sample’s lower quartile also trigger a review when below its median.
- Client-priority URLs lead the list. The worklist also surfaces eligible pages with at most one observed main-content source when link-position coverage supports that finding, even when Google’s reported count is high. Focus matches, impressions and clicks order targets within each group.
- Search Console query phrases support anchor and passage selection. The page-level Performance export supplies total clicks and impressions.
- With Search Console alone, candidate sources are ranked by common words in URL slugs. With a crawl, titles and H1s add evidence. Candidate sources must share at least two useful terms with the target. They are limited to the same origin and the same recognized locale folder. Proposed anchor text comes from the target heading or title when available.
- The tool preserves differences in scheme, hostname, path, and query. It removes URL fragments and default ports. It does not merge canonical or redirect variants. Keep Search Console and crawler exports scoped consistently.
- Files are limited to 8 MiB each and 150,000 rows. The crawl page inventory is limited to 25,000 unique URLs. The MCP text fallback accepts up to 150,000 characters per input.

## Run the MCP server

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
python server.py
```

Connect MCP Inspector over Streamable HTTP at `http://127.0.0.1:8000/mcp`. The server advertises attachment and pasted-CSV tools for both the audit and follow-up
verification. The `start_internal_linking_audit` tool provides the guided setup.

For a private ChatGPT test, make the server reachable on HTTPS or use a supported Secure MCP Tunnel connection. For an HTTPS reverse proxy, set `SEO_PUBLIC_MCP_HOST` to the exact public hostname and preserve the original Host header when proxying to the server. Use **Plugins → Add custom MCP server** and provide its `/mcp` URL. Attach the two unpacked CSVs and try this prompt:

> Audit these Search Console exports. Show the five most important pages with low reported internal link counts. List missing-from-export pages separately and give me relevant source pages to review.

The file tool declares the OpenAI file parameter metadata. Attachment retrieval uses HTTPS without redirects. It accepts `files.oaiusercontent.com` and regional `sdmntpr<region>.oaiusercontent.com` endpoints. If a deployment uses another confirmed OpenAI attachment host, add the exact `.oaiusercontent.com` hostname via `SEO_ATTACHMENT_HOSTS`. Paste small CSVs with the text tool or use the local CLI if your deployment’s attachment host is unsupported.

Keep using `uvicorn server:create_app --factory --host 0.0.0.0 --port 8000` on Northflank.
A public deployment with `SEO_PUBLIC_MCP_HOST` set serves the single-sheet Excel audit and follow-up reports. Downloads are held in memory on the serving instance for up to 15 minutes.
Run one worker per endpoint so requests reach the instance holding the report.
Anyone with the URL can download the report while it is active. An instance restart or cache eviction
may end availability sooner. Run the audit again to regenerate the link.

Audit inputs are processed in memory. The server does not persist raw CSVs, edit websites,
or request access to Search Console. When replacing a production deployment, ensure your
privacy disclosures cover the temporary report download and any hosting logs you collect.

## Packaging for submission

The `plugin/` folder contains the manifests and a guided onboarding workflow for v0.5.3.
`plugin/mcp.json` points to the Oracle endpoint at `https://gpt.dbc21.com/mcp`. If you use a different public hostname,
update the URL and the corresponding runtime hostname. Finish the listing metadata and review
materials required by the submission flow, including a privacy policy reflecting the deployment.
The outer source ZIP is for development. Use the configured plugin directory when preparing the submission.

- [Google Search Console Links report](https://support.google.com/webmasters/answer/9049606)
- [Export Search Console data](https://support.google.com/webmasters/answer/12919797)
- [Google’s link and anchor text guidance](https://developers.google.com/search/docs/crawling-indexing/links-crawlable)
- [Connect and test a plugin in ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [OpenAI plugin submission](https://developers.openai.com/plugins/deploy/submission)
