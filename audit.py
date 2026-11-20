"""Audit internal linking with Search Console and optional crawl exports."""

import argparse
import csv
import io
import json
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 150_000
MAX_PAGES = 25_000
WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
LOCALE = re.compile(r"(?:ar|de|en|es|fr|it|ja|ko|nl|pl|pt|ru|sr|tr|uk|zh)(?:-[a-z]{2,4})?")
STOPWORDS = set("""
about all and are best blog buy category collection collections contact content guide guides
home index language learn more new news official page pages product products sale search
shop site store the this top website what where with your how for from info our service
services html php htm link online articles article that help www arabe العربية الموقع
على هذا هذه من غير دليل الرئيسية جميع شراء كيف إلى
""".split())

PERFORMANCE = {
    "url": ("top pages", "page", "url"),
    "clicks": ("clicks",),
    "impressions": ("impressions",),
    "position": ("position", "average position"),
}
LINKS = {
    "url": ("target page", "page", "url"),
    "links": ("internal links", "linking pages", "links"),
}
PAGES = {
    "url": ("address", "url", "page"),
    "status": ("status code", "http status", "status"),
    "indexability": ("indexability",),
    "title": ("title 1", "title", "page title"),
    "h1": ("h1-1", "h1"),
    "canonical": ("canonical link element 1", "canonical", "canonical url"),
    "content_type": ("content type",),
}
INLINKS = {
    "source": ("source", "source url", "from"),
    "target": ("destination", "destination url", "target", "target url", "to"),
    "type": ("type", "link type"),
}


@dataclass(frozen=True)
class Performance:
    clicks: int
    impressions: int
    position: float | None


@dataclass(frozen=True)
class Page:
    title: str
    h1: str
    status: int
    indexability: str
    canonical: str
    content_type: str

    def eligible(self, url: str) -> bool:
        return (
            self.status == 200
            and self.indexability == "indexable"
            and (not self.canonical or self.canonical == url)
            and (not self.content_type or "html" in self.content_type)
        )


def csv_rows(content: str, label: str, columns: dict, required: set):
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"{label} exceeds the 8 MiB limit. Export a focused subset.")
    stream = io.StringIO(content.lstrip("\ufeff"), newline="")
    header_line = stream.readline()
    stream.seek(0)
    delimiter = max((",", ";", "\t"), key=header_line.count)
    reader = csv.reader(stream, delimiter=delimiter, strict=True)
    try:
        header = [" ".join(value.casefold().split()) for value in next(reader, [])]
        index = {}
        for field, aliases in columns.items():
            matches = [i for i, name in enumerate(header) if name in aliases]
            if len(matches) > 1:
                raise ValueError(f"{label} has ambiguous columns for {field}.")
            if matches:
                index[field] = matches[0]
        missing = required - index.keys()
        if missing:
            expected = ", ".join(columns[field][0] for field in sorted(missing))
            raise ValueError(f"{label} is missing expected columns: {expected}.")
        for row in reader:
            if reader.line_num > MAX_ROWS + 1:
                raise ValueError(f"{label} exceeds {MAX_ROWS:,} rows. Export a focused subset.")
            if not any(value.strip() for value in row):
                continue
            if len(row) != len(header):
                raise ValueError(f"{label} line {reader.line_num} has the wrong column count.")
            yield reader.line_num, {name: row[i].strip() for name, i in index.items()}
    except csv.Error as error:
        raise ValueError(f"{label} line {reader.line_num} contains invalid CSV.") from error


def normal_url(value: str) -> str:
    if not value or len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("Use a valid absolute HTTP or HTTPS URL.")
    try:
        address = urlsplit(value)
        if address.scheme not in {"http", "https"} or not address.hostname or address.username is not None:
            raise ValueError
        host = address.hostname.encode("idna").decode().lower()
        port = address.port
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (address.scheme, port) not in {("http", 80), ("https", 443)}:
            host = f"{host}:{port}"
        return urlunsplit((address.scheme, host, address.path or "/", address.query, ""))
    except (UnicodeError, ValueError) as error:
        raise ValueError("Use a valid absolute HTTP or HTTPS URL.") from error


def whole(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,15}|[0-9]{1,3}(?:,[0-9]{3}){1,4}", value):
        raise ValueError("Counts and status codes must be non-negative whole numbers.")
    return int(value.replace(",", ""))


def average_position(value: str) -> float | None:
    if value in {"", "-", "—", "0", "0.0", "0,0"}:
        return None
    if not re.fullmatch(r"[0-9]{1,7}(?:[.,][0-9]{1,8})?", value):
        raise ValueError("Average position must be numeric or blank.")
    return float(value.replace(",", ".")) or None


def store_row(rows: dict, url: str, value):
    if url in rows and rows[url] != value:
        raise ValueError("Conflicting rows for the same URL. Use an unsegmented export.")
    rows[url] = value


def read_performance(content: str) -> dict[str, Performance]:
    rows = {}
    label = "Performance Pages CSV"
    for line, data in csv_rows(content, label, PERFORMANCE, {"url", "clicks", "impressions"}):
        try:
            store_row(
                rows, normal_url(data["url"]),
                Performance(whole(data["clicks"]), whole(data["impressions"]),
                            average_position(data.get("position", ""))),
            )
        except ValueError as error:
            raise ValueError(f"{label} line {line}. {error}") from error
    if not rows:
        raise ValueError("Performance Pages CSV has no page rows.")
    return rows


def read_links(content: str) -> dict[str, int]:
    rows = {}
    label = "Internal Links CSV"
    for line, data in csv_rows(content, label, LINKS, {"url", "links"}):
        try:
            store_row(rows, normal_url(data["url"]), whole(data["links"]))
        except ValueError as error:
            raise ValueError(f"{label} line {line}. {error}") from error
    return rows


def read_pages(content: str) -> dict[str, Page]:
    rows = {}
    label = "Crawl Pages CSV"
    for line, data in csv_rows(content, label, PAGES, {"url", "status", "indexability"}):
        try:
            canonical = data.get("canonical", "")
            store_row(
                rows, normal_url(data["url"]),
                Page(data.get("title", "")[:200], data.get("h1", "")[:200],
                     whole(data["status"]) if data["status"] else 0, data["indexability"].casefold(),
                     normal_url(canonical) if canonical else "",
                     data.get("content_type", "").casefold()),
            )
        except ValueError as error:
            raise ValueError(f"{label} line {line}. {error}") from error
    if not rows:
        raise ValueError("Crawl Pages CSV has no page rows.")
    if len(rows) > MAX_PAGES:
        raise ValueError(f"Crawl Pages CSV exceeds {MAX_PAGES:,} distinct URLs.")
    return rows


def read_inlinks(content: str) -> set[tuple[str, str]]:
    edges = set()
    label = "Crawl Inlinks CSV"
    for line, data in csv_rows(content, label, INLINKS, {"source", "target"}):
        if data.get("type", "").casefold() not in {"", "hyperlink", "anchor", "a"}:
            continue
        try:
            source, target = normal_url(data["source"]), normal_url(data["target"])
        except ValueError as error:
            raise ValueError(f"{label} line {line}. {error}") from error
        if source != target:
            edges.add((source, target))
    return edges


def locality(url: str) -> tuple[str, str, str]:
    address = urlsplit(url)
    segment = address.path.split("/")[1].casefold()
    tag = segment if LOCALE.fullmatch(segment) else ""
    return address.scheme, address.netloc, tag


def tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    return {word for word in WORD.findall(text) if len(word) >= 3 and word not in STOPWORDS}


def slug(url: str) -> str:
    path = unquote(urlsplit(url).path, errors="replace").strip("/")
    return re.sub(r"[-_]+", " ", path.rsplit("/", 1)[-1]).strip()


class CandidateMatcher:
    def __init__(self, source_urls: set[str], pages: dict[str, Page], edges: set,
                 performance: dict[str, Performance]):
        self.pages = pages
        self.edges = edges
        self.performance = performance
        self.source_terms = defaultdict(dict)
        self.postings = defaultdict(lambda: defaultdict(set))
        for url in sorted(source_urls):
            group = locality(url)
            terms = self.page_terms(url)
            if terms:
                self.source_terms[group][url] = terms
                for term in terms:
                    self.postings[group][term].add(url)

    def page_terms(self, url: str) -> set[str]:
        page = self.pages.get(url)
        return tokens(f"{slug(url)} {page.h1} {page.title}" if page else slug(url))

    def suggest(self, target: str, count: int) -> list[dict]:
        group = locality(target)
        terms_by_url = self.source_terms.get(group, {})
        postings = self.postings.get(group, {})
        terms = self.page_terms(target)
        size = len(terms_by_url)
        if not size or len(terms) < 2:
            return []
        overlap = defaultdict(set)
        for term in terms:
            sources = postings.get(term, set())
            for source in sources:
                if source != target and (source, target) not in self.edges:
                    overlap[source].add(term)
        ranked = []
        for source, shared in overlap.items():
            if len(shared) < 2:
                continue
            relevance = len(shared) / math.sqrt(len(terms_by_url[source]) * len(terms))
            if relevance < 0.28:
                continue
            rarity = sum(math.log((size + 1) / (len(postings[word]) + 1)) for word in sorted(shared))
            clicks = self.performance[source].clicks if source in self.performance else -1
            ranked.append((-relevance, -rarity, -clicks, source, sorted(shared)))
        suggestions = []
        for _, _, _, source, shared in sorted(ranked)[:count]:
            page = self.pages.get(target)
            anchor = (page.h1 or page.title) if page else ""
            suggestions.append({
                "source_url": source,
                "target_url": target,
                "shared_terms": shared[:6],
                "existing_source_to_target_link": (
                    "not_observed_in_supplied_crawl" if self.pages else "unknown"
                ),
                "evidence": "crawl_titles_headings_and_urls" if self.pages else "url_slug_similarity",
                "suggested_anchor": (anchor.strip()[:85] or None),
                "editorial_check": "Confirm the page is relevant and choose a natural placement and anchor.",
            })
        return suggestions


def audit(links_csv: str, performance_csv: str, crawl_pages_csv: str | None = None,
          crawl_inlinks_csv: str | None = None, max_targets: int = 20,
          sources_per_target: int = 3) -> dict:
    if not 1 <= max_targets <= 50 or not 1 <= sources_per_target <= 5:
        raise ValueError("Choose 1–50 targets and 1–5 source candidates per target.")
    if (crawl_pages_csv is None) != (crawl_inlinks_csv is None):
        raise ValueError("Provide both crawl exports together, or omit both.")
    performance = read_performance(performance_csv)
    links = read_links(links_csv)
    pages = read_pages(crawl_pages_csv) if crawl_pages_csv is not None else {}
    edges = ({(source, target) for source, target in read_inlinks(crawl_inlinks_csv) if source in pages}
             if crawl_inlinks_csv is not None else set())
    visible = {url: data for url, data in performance.items() if data.impressions > 0}
    matched = visible.keys() & links.keys()
    counts = sorted(links[url] for url in matched)
    median = statistics.median(counts) if counts else None
    lower_quartile = counts[math.ceil(len(counts) / 4) - 1] if counts else None

    def rank(url: str):
        data = visible[url]
        return (-data.impressions, -data.clicks, url)

    low = sorted((
        url for url in matched
        if links[url] <= 1 or (len(counts) >= 5 and links[url] <= lower_quartile and links[url] < median)
    ), key=rank)
    unreported = sorted(visible.keys() - links.keys(), key=rank)
    inbound = Counter(target for _, target in edges)
    crawl_eligible = {url for url, page in pages.items() if page.eligible(url)}
    # A source needs evidence its outgoing links were captured before the missing edge is actionable.
    crawled_sources = {source for source, _ in edges} & crawl_eligible
    source_urls = crawled_sources if pages else set(performance) | set(links)
    matcher = CandidateMatcher(source_urls, pages, edges, performance)

    def describe(url: str, unlisted: bool) -> dict:
        data = visible[url]
        eligible = url in crawl_eligible if pages else None
        if eligible is False:
            action = "Check crawl coverage, HTTP status, indexability and canonical before adding links."
        elif unlisted:
            action = "Verify actual inbound links, then add contextual links if coverage is weak."
        else:
            action = "Review contextual links from relevant pages and add a link where it helps the reader."
        return {
            "target_url": url,
            "search_impressions": data.impressions,
            "search_clicks": data.clicks,
            "average_position": data.position,
            "gsc_reported_internal_links": links.get(url),
            "crawl_distinct_linking_urls": inbound[url] if url in pages else None,
            "eligible_in_crawl": eligible,
            "recommended_action": action,
            "candidate_sources": matcher.suggest(url, sources_per_target) if eligible is not False else [],
        }

    # Keep priority targets separate from pages missing from the sampled Google report.
    top_low = [describe(url, False) for url in low[:max_targets]]
    top_unreported = [describe(url, True) for url in unreported[:max_targets]]
    source_fixes = []
    if pages:
        for source, target in edges:
            source_page, target_page = pages.get(source), pages.get(target)
            if source_page is None or target_page is None or source not in crawl_eligible:
                continue
            if target_page.status in {301, 302, 303, 307, 308}:
                issue = "link_to_redirect"
                fix = "Check the redirect destination and replace the link with the intended final URL."
            elif 400 <= target_page.status <= 599:
                issue = "link_to_error"
                fix = "Verify the error and replace or remove the link after confirming the intended destination."
            else:
                continue
            source_fixes.append((source, target, target_page.status, issue, fix))
        source_fixes.sort(key=lambda row: (
            row[3] != "link_to_error",
            -(performance[row[0]].clicks if row[0] in performance else 0), row[0], row[1],
        ))

    notes = [
        "Use both Search Console exports from the same property. Performance data covers your chosen date range.",
        "The Google Links report is sampled, may be stale and groups by canonical URL. "
        "An unreported count is unknown. A low count is a review signal.",
        "Targets are ordered by impressions, then clicks. Review business importance and page type before linking.",
        "URLs are matched by scheme, host, path and query after removing fragments and default ports. "
        "Review redirects and canonical variants when exports do not match.",
    ]
    if pages:
        notes.append("Crawl coverage is limited to the supplied exports. Zero observed inlinks "
                     "is not proof that a target is orphaned. Verify candidates on the live pages.")
    else:
        notes.append("Candidate sources use URL wording only. Check relevance, indexability and existing links by hand.")
    if links and not performance.keys() & links.keys():
        notes.append("The exports share no exact URLs. Verify property scope and canonical URL forms.")
    return {
        "status": "ok",
        "mode": "with_crawl" if pages else "search_console_only",
        "profile": {
            "performance_pages": len(performance),
            "pages_with_impressions": len(visible),
            "targets_in_links_export": len(links),
            "exact_url_matches": len(performance.keys() & links.keys()),
            "matched_pages_with_impressions": len(matched),
            "sample_median_links_for_matched_visible_pages": median,
            "sample_lower_quartile_links_for_matched_visible_pages": lower_quartile,
            "observed_low_link_targets": len(low),
            "visible_targets_missing_from_links_export": len(unreported),
            "crawl_pages": len(pages) if pages else None,
            "crawl_distinct_hyperlink_pairs": len(edges) if pages else None,
            "crawl_source_link_fixes": len(source_fixes) if pages else None,
        },
        "top_linked_targets_in_gsc_sample": [
            {"url": url, "gsc_reported_internal_links": count}
            for url, count in sorted(links.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
        "priority_targets_with_observed_low_counts": top_low,
        "coverage_checks_for_unreported_targets": top_unreported,
        "crawl_source_link_fixes": [
            {"source_url": source, "target_url": target, "target_http_status": status,
             "issue": issue, "suggested_fix": fix}
            for source, target, status, issue, fix in source_fixes[:max_targets]
        ],
        "interpretation_notes": notes,
        "references": [
            "https://support.google.com/webmasters/answer/9049606",
            "https://developers.google.com/search/docs/crawling-indexing/links-crawlable",
        ],
    }


def read_file(path: Path) -> str:
    with path.open("rb") as file:
        content = file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} exceeds the 8 MiB limit. Export a focused subset.")
    return content.decode("utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", type=Path, required=True)
    parser.add_argument("--performance", type=Path, required=True)
    parser.add_argument("--crawl-pages", type=Path)
    parser.add_argument("--crawl-inlinks", type=Path)
    parser.add_argument("--max-targets", type=int, default=20)
    args = parser.parse_args()
    try:
        result = audit(
            read_file(args.links), read_file(args.performance),
            read_file(args.crawl_pages) if args.crawl_pages else None,
            read_file(args.crawl_inlinks) if args.crawl_inlinks else None,
            args.max_targets,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
