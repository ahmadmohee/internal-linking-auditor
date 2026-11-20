# Internal Linking Auditor

A ChatGPT Plugin to help you audit and optimize your website's internal linking profile.

Run a technical SEO internal linking audit with Google Search Console. Upload the Internal Links CSV and Performance Pages.csv from the same property. Find important pages with few reported internal links, prioritize targets by search impressions and clicks, and discover relevant pages to link from. Add Screaming Frog crawl and All Inlinks exports to exclude links already observed, suggest descriptive anchor text, and flag internal links that point to redirects or error URLs.

## Publisher and support

- **Publisher** — Ahmad Mohee
- **Support email** — [ahmadmohee@gmail.com](mailto:ahmadmohee@gmail.com)
- **LinkedIn** — [Ahmad Mohee](https://www.linkedin.com/in/ahmadmohee/)

## Questions you can ask in ChatGPT

- Which important pages on my website have too few internal links?
- Where should I add internal links, and what anchor text should I use?
- Which internal links point to a 404 or a redirect?

Looking for orphan pages? The tool lists URLs missing from the Search Console Links report for follow-up. If a URL is absent from that sampled report, its link count is unknown. Review the suggested links on your live pages before editing.

A read-only ChatGPT plugin prototype that joins Search Console internal link counts with search performance data, then returns prioritized targets and editorial link candidates. It accepts CSV attachments or CSV text through MCP. The command-line analyzer also runs locally without third-party packages.

## Search Console exports

Use two UTF-8 CSV files from the same property.

1. **Links → Internal links → Top linked pages → More → Export.** The expected columns are `Target page` and `Internal links`. Export from the internal links detail table.
2. **Performance → Search results → Export → Download CSV.** Choose a single date range for Web search, then extract `Pages.csv` from the ZIP. The expected columns are `Top pages`, `Clicks`, `Impressions`, and optional `Position`. A recent 90-day window is a useful starting point.

Use English headers or rename the relevant columns to the names above. Comma, semicolon and tab delimiters are accepted. Google limits exported tables and samples link data. URLs absent from the Internal Links export are reported as **unreported** with an unknown count. The Links report can contain older links and canonicalized target URLs.

## Optional crawl evidence

Use two exports from the same completed crawl with internal hyperlinks enabled.

- **Screaming Frog → Internal → All → Export.** Include `Address`, `Status Code`, and `Indexability`. Include `Content Type`, `Title 1`, `H1-1`, and `Canonical Link Element 1` when available. Use the combined Internal export so redirect and error destinations are available for the repair list.
- **Screaming Frog → Bulk Export → Links → All Inlinks.** Include `Source`, `Destination`, and `Type` when present. When there is no type column, provide hyperlink rows only.

With these exports, the tool excludes source-to-target links already observed in the crawl, filters suggestions to applicable indexable pages, and flags observed links to redirect and error URLs. A crawl may miss links or pages. Verify the live page and placement before editing.

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

The synthetic example prioritizes a product category with 13,620 impressions and one reported internal link. The crawl confirms an existing link from one guide, excludes that guide from suggestions, surfaces other related pages, and identifies one observed link to a redirect and another to a 404.

## How prioritization works

- On pages with search impressions, an observed count of 0 or 1 triggers a review. With at least five matched pages, counts at or below the sample’s lower quartile also trigger a review when below its median.
- Targets are ordered by search impressions, then clicks, within the reported and unreported lists. Counts and thresholds are specific to the export. Consider page type, navigation, and business value before applying the recommendations.
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

Connect MCP Inspector over Streamable HTTP at `http://127.0.0.1:8000/mcp`. The server advertises `audit_internal_link_files` and `audit_internal_link_csv`.

For a private ChatGPT test, make the server reachable on HTTPS or use a supported Secure MCP Tunnel connection. For an HTTPS reverse proxy, set `SEO_PUBLIC_MCP_HOST` to the exact public hostname and preserve the original Host header when proxying to the server. Use **Plugins → Add custom MCP server** and provide its `/mcp` URL. Attach the two unpacked CSVs and try this prompt:

> Audit these Search Console exports. Show the five most important pages with low reported internal link counts. List missing-from-export pages separately and give me relevant source pages to review.

The file tool declares the OpenAI file parameter metadata. Attachment retrieval is restricted to HTTPS, with no redirects, and defaults to `files.oaiusercontent.com`. If a deployment uses another confirmed OpenAI attachment host, add the exact `.oaiusercontent.com` hostname via `SEO_ATTACHMENT_HOSTS`. Paste small CSVs with the text tool or use the local CLI if your deployment’s attachment host is unsupported.

The source was tested locally with synthetic CSVs, the attachment schema and mocked streaming downloads, and the MCP Streamable HTTP transport. The attachment flow still needs a test with real uploads in ChatGPT. Inputs are processed in memory. The implementation does not persist CSV contents, alter websites, or request Search Console access.

## Packaging for submission

The `plugin/` folder contains starter manifests. Replace the sample endpoint in `plugin/mcp.json` with your deployed HTTPS endpoint. Finish the listing metadata and review materials required by the submission flow, including a privacy policy reflecting the actual deployment. The outer source ZIP is for development. Use the configured plugin directory when preparing the submission.

- [Google Search Console Links report](https://support.google.com/webmasters/answer/9049606)
- [Export Search Console data](https://support.google.com/webmasters/answer/12919797)
- [Google’s link and anchor text guidance](https://developers.google.com/search/docs/crawling-indexing/links-crawlable)
- [Connect and test a plugin in ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [OpenAI plugin submission](https://developers.openai.com/plugins/deploy/submission)
