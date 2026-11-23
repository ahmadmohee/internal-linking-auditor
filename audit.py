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
ARABIC_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ـ": None})
LOCALE = re.compile(r"(?:ar|de|en|es|fr|it|ja|ko|nl|pl|pt|ru|sr|tr|uk|zh)(?:-[a-z]{2,4})?")
STOPWORDS = set("""
about all and are best blog buy category collection collections contact content guide guides
home index language learn more new news official page pages product products sale search
shop site store the this top website what where with your how for from info our service
services html php htm link online articles article that help www arabe العربية الموقع
على هذا هذه من غير دليل الرئيسية جميع شراء كيف إلى
""".split())
GENERIC_ANCHORS = {
    "click here", "here", "read more", "learn more", "more", "see more", "view more",
    "find out more", "more details", "اضغط هنا", "انقر هنا", "هنا", "المزيد",
    "اقرا المزيد", "اعرف المزيد", "عرض المزيد", "اظهر المزيد",
}

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
    "anchor": ("anchor", "anchor text"),
    "position": ("link position", "position"),
    "alt": ("alt text", "image alt text"),
    "path": ("link path",),
}
CONTENT, NAVIGATION, FOOTER, OTHER, UNKNOWN = 1, 2, 4, 8, 16
LINK_POSITIONS = {
    "content": CONTENT, "main content": CONTENT, "navigation": NAVIGATION,
    "nav": NAVIGATION, "footer": FOOTER, "header": OTHER, "sidebar": OTHER, "aside": OTHER,
}
# The file size limit also bounds individual CSV fields, including grouped worklist URLs.
csv.field_size_limit(MAX_FILE_BYTES)


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
            # Screaming Frog exports include both a numeric Status Code and a
            # descriptive Status, and both an URL Destination and HTML Target
            # (_blank / _self). These are different attributes, not duplicate
            # representations of the same field. Prefer the semantic column;
            # still reject genuinely competing URL/code columns.
            if label == "Crawl Pages CSV" and field == "status":
                if any(header[i] in {"status code", "http status"} for i in matches):
                    matches = [i for i in matches if header[i] != "status"]
            elif label == "Crawl Inlinks CSV" and field == "target":
                if any(header[i] in {"destination", "destination url"} for i in matches):
                    matches = [i for i in matches if header[i] != "target"]
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


def read_performance(content: str, allow_empty: bool = False) -> dict[str, Performance]:
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
    if not rows and not allow_empty:
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


@dataclass
class CrawlInlinks:
    edges: set[tuple[str, str]]
    generic_anchors: set[tuple[str, str, str]]
    has_anchor_columns: bool = False
    positions: dict | None = None
    templates: dict | None = None
    observations: dict | None = None


def read_inlinks(content: str, repair_targets=(), inspect_pairs=()) -> CrawlInlinks:
    inlinks = CrawlInlinks(set(), set(), positions={}, templates={}, observations={})
    label = "Crawl Inlinks CSV"
    for line, data in csv_rows(content, label, INLINKS, {"source", "target"}):
        inlinks.has_anchor_columns |= "anchor" in data and "position" in data
        if data.get("type", "").casefold() not in {"", "hyperlink", "anchor", "a"}:
            continue
        try:
            source, target = normal_url(data["source"]), normal_url(data["target"])
        except ValueError as error:
            raise ValueError(f"{label} line {line}. {error}") from error
        if source == target:
            continue
        pair = (source, target)
        inlinks.edges.add(pair)
        anchor = data.get("anchor", "")
        position = LINK_POSITIONS.get(data.get("position", "").casefold(), UNKNOWN)
        inlinks.positions[pair] = inlinks.positions.get(pair, 0) | position
        if position in {NAVIGATION, FOOTER} and target in repair_targets and data.get("path"):
            signature = (target, position, data["path"][:512], anchor_key(anchor))
            inlinks.templates.setdefault(signature, set()).add(source)
        if pair in inspect_pairs:
            inlinks.observations.setdefault(pair, set()).add(
                (position, anchor_key(anchor), bool(data.get("alt")))
            )
        if (position == CONTENT and not data.get("alt")
                and anchor_key(anchor) in GENERIC_ANCHORS):
            inlinks.generic_anchors.add((source, target, anchor))
    return inlinks


def locality(url: str) -> tuple[str, str, str]:
    address = urlsplit(url)
    segment = address.path.split("/")[1].casefold()
    tag = segment if LOCALE.fullmatch(segment) else ""
    return address.scheme, address.netloc, tag


def normalized_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char)).translate(ARABIC_FORMS)
    return WORD.findall(text)


def tokens(text: str) -> set[str]:
    return {word for word in normalized_words(text) if len(word) >= 3 and word not in STOPWORDS}


def anchor_key(text: str) -> str:
    return " ".join(normalized_words(text))


def slug(url: str) -> str:
    path = unquote(urlsplit(url).path, errors="replace").strip("/")
    return re.sub(r"[-_]+", " ", path.rsplit("/", 1)[-1]).strip()


def anchor_suggestion(url: str, page: Page | None, focus_keywords=()) -> tuple[str | None, str | None]:
    options = [(keyword, "focus keyword matching target wording") for keyword in focus_keywords]
    if page:
        options.extend([(page.h1, "target H1"), (page.title, "target title")])
    options.append((slug(url), "target URL wording"))
    for option, source in options:
        anchor = re.split(r"\s+[|–—]\s+", " ".join(option.split()), maxsplit=1)[0]
        if len(anchor) > 85:
            anchor = anchor[:86].rsplit(" ", 1)[0][:85]
        if tokens(anchor) and anchor_key(anchor) not in GENERIC_ANCHORS:
            return anchor, source
    return None, None


@dataclass(frozen=True)
class SiteScope:
    url: str

    def includes(self, url: str, include_path: bool = True) -> bool:
        site, page = urlsplit(self.url), urlsplit(url)
        path = site.path.rstrip("/")
        base_host = site.hostname.removeprefix("www.")
        same_host = page.hostname in {base_host, f"www.{base_host}"} and page.port == site.port
        return same_host and (
            not include_path or not path or page.path == path or page.path.startswith(path + "/")
        )


def get_site_scope(website_url: str) -> SiteScope:
    value = website_url.strip()
    if "://" not in value:
        value = f"https://{value}"
    try:
        url = normal_url(value)
        address = urlsplit(url)
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]{2,63}", address.hostname or "") or address.query:
            raise ValueError
    except ValueError as error:
        raise ValueError("Enter a website URL or domain, for example https://example.com/.") from error
    return SiteScope(url)


def clean_keywords(values: list[str]) -> list[str]:
    if len(values) > 10:
        raise ValueError("Enter up to 10 focus keywords or phrases.")
    keywords = {}
    for value in values:
        phrase = " ".join(value.split())
        if len(phrase) > 100:
            raise ValueError("Keep each focus keyword or phrase to 100 characters or fewer.")
        if phrase:
            keywords.setdefault(phrase.casefold(), phrase)
    return list(keywords.values())


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

    def suggest(self, target: str, count: int, focus_keywords: list[str]) -> list[dict]:
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
        topic_terms = [tokens(keyword) for keyword in focus_keywords]
        ranked = []
        for source, shared in overlap.items():
            if len(shared) < 2:
                continue
            if topic_terms and not any(
                len(shared & topic) >= math.ceil(0.6 * len(topic)) for topic in topic_terms
            ):
                continue
            relevance = len(shared) / math.sqrt(len(terms_by_url[source]) * len(terms))
            if relevance < 0.28:
                continue
            rarity = sum(math.log((size + 1) / (len(postings[word]) + 1)) for word in sorted(shared))
            clicks = self.performance[source].clicks if source in self.performance else -1
            ranked.append((-relevance, -rarity, -clicks, source, sorted(shared)))
        suggestions = []
        anchor, anchor_basis = anchor_suggestion(target, self.pages.get(target), focus_keywords)
        for _, _, _, source, shared in sorted(ranked)[:count]:
            topic = focus_keywords[0] if focus_keywords else " / ".join(shared[:3])
            suggestions.append({
                "source_url": source,
                "target_url": target,
                "shared_terms": shared[:6],
                "existing_source_to_target_link": (
                    "not_observed_in_supplied_crawl" if self.pages else "unknown"
                ),
                "evidence": "crawl_titles_headings_and_urls" if self.pages else "url_slug_similarity",
                "suggested_anchor": anchor,
                "anchor_basis": anchor_basis,
                "placement_hint": f"Find a relevant paragraph or section about {topic}.",
                "editorial_check": "Confirm the page is relevant and choose a natural placement and anchor.",
            })
        return suggestions


def audit(links_csv: str, performance_csv: str, crawl_pages_csv: str | None = None,
          crawl_inlinks_csv: str | None = None, max_targets: int = 20,
          sources_per_target: int = 3, *, website_url: str | None = None,
          focus_keywords: list[str] | None = None, priority_urls: list[str] | None = None,
          snippets_csv: str | None = None, main_content: list[dict] | None = None,
          page_queries_csv: str | None = None, queries_page_url: str | None = None,
          redirects_csv: str | None = None, include_worklist: bool = False) -> dict:
    from link_worklist import Worklist

    if not 1 <= max_targets <= 50 or not 1 <= sources_per_target <= 5:
        raise ValueError("Choose 1–50 targets and 1–5 source candidates per target.")
    if (crawl_pages_csv is None) != (crawl_inlinks_csv is None):
        raise ValueError("Provide both crawl exports together, or omit both.")
    performance = read_performance(performance_csv, allow_empty=bool(website_url and priority_urls))
    links = read_links(links_csv)
    pages = read_pages(crawl_pages_csv) if crawl_pages_csv is not None else {}
    keywords = clean_keywords(focus_keywords or [])
    scope = get_site_scope(website_url) if website_url else None
    if scope:
        if performance and not any(scope.includes(url) for url in performance) and not any(
            scope.includes(url, include_path=False) for url in performance
            if priority_urls
        ):
            raise ValueError("The Performance Pages export has no URLs under the selected website or directory.")
        if links and not any(scope.includes(url, include_path=False) for url in links):
            raise ValueError("The Internal Links export contains no URLs from the selected website.")
        if pages and not any(scope.includes(url, include_path=False) for url in pages):
            raise ValueError("The Crawl Pages export contains no URLs from the selected website.")
        performance = {url: data for url, data in performance.items()
                       if scope.includes(url, include_path=False)}
        links = {url: count for url, count in links.items() if scope.includes(url, include_path=False)}
        pages = {url: page for url, page in pages.items() if scope.includes(url, include_path=False)}
    repair_targets = {
        url for url, page in pages.items() if page.status in {301, 302, 303, 307, 308} or 400 <= page.status <= 599
    }
    inlinks = (read_inlinks(crawl_inlinks_csv, repair_targets=repair_targets)
               if crawl_inlinks_csv is not None else CrawlInlinks(set(), set()))
    edges = {(source, target) for source, target in inlinks.edges if source in pages}
    visible = {url: data for url, data in performance.items()
               if data.impressions > 0 and (not scope or scope.includes(url))}
    matched = visible.keys() & links.keys()
    counts = sorted(links[url] for url in matched)
    median = statistics.median(counts) if counts else None
    lower_quartile = counts[math.ceil(len(counts) / 4) - 1] if counts else None

    inbound = Counter(target for _, target in edges)
    crawl_eligible = {url for url, page in pages.items() if page.eligible(url)}
    # A source needs evidence its outgoing links were captured before the missing edge is actionable.
    crawled_sources = {source for source, _ in edges} & crawl_eligible
    source_urls = crawled_sources if pages else set(performance) | set(links)
    matcher = CandidateMatcher(source_urls, pages, edges, performance)
    worklist = None
    if (include_worklist or priority_urls or snippets_csv is not None or main_content
            or page_queries_csv is not None or redirects_csv is not None or queries_page_url):
        worklist = Worklist(
            scope, pages, performance, links, inlinks, edges, matcher, crawl_eligible, crawled_sources,
            priority_urls or [], snippets_csv, main_content or [], page_queries_csv, queries_page_url,
            redirects_csv,
        )
    focus_terms = [(keyword, tokens(keyword)) for keyword in keywords]
    matches = {}
    if keywords:
        for url in visible.keys() | (worklist.priorities if worklist else set()):
            terms = matcher.page_terms(url)
            matches[url] = [phrase for phrase, query_terms in focus_terms if query_terms and query_terms <= terms]

    def rank(url: str):
        data = performance.get(url, Performance(0, 0, None))
        return (url not in worklist.priorities if worklist else False,
                -len(matches.get(url, [])), -data.impressions, -data.clicks, url)

    low = sorted((
        url for url in matched
        if links[url] <= 1 or (len(counts) >= 5 and links[url] <= lower_quartile and links[url] < median)
    ), key=rank)
    unreported = sorted(visible.keys() - links.keys(), key=rank)

    def describe(url: str, unlisted: bool) -> dict:
        data = performance.get(url)
        eligible = url in crawl_eligible if pages else None
        focus_matches = matches.get(url, [])
        if eligible is False:
            action = "Check crawl coverage, HTTP status, indexability and canonical before adding links."
        elif unlisted:
            action = "Verify actual inbound links, then add contextual links if coverage is weak."
        else:
            action = "Review contextual links from relevant pages and add a link where it helps the reader."
        return {
            "target_url": url,
            "search_impressions": data.impressions if data is not None else None,
            "search_clicks": data.clicks if data is not None else None,
            "average_position": data.position if data is not None else None,
            "gsc_reported_internal_links": links.get(url),
            "crawl_distinct_linking_urls": inbound[url] if url in pages else None,
            "eligible_in_crawl": eligible,
            "recommended_action": action,
            "matched_focus_keywords": focus_matches,
            "candidate_sources": (
                matcher.suggest(url, sources_per_target, focus_matches) if eligible is not False else []
            ),
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
            if scope and not (scope.includes(source) or scope.includes(target)):
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
    anchor_fixes = []
    for source, target, current in inlinks.generic_anchors:
        if (source not in crawl_eligible or target not in crawl_eligible or target not in visible
                or locality(source) != locality(target)):
            continue
        suggested, basis = anchor_suggestion(target, pages[target])
        if suggested is None:
            continue
        anchor_fixes.append({
            "source_url": source,
            "target_url": target,
            "current_anchor": current,
            "suggested_anchor": suggested,
            "anchor_basis": basis,
            "evidence": "Generic anchor observed on a content link in the supplied crawl.",
            "suggested_fix": (
                "Find the existing link in the body of the source page, read the sentence, and replace the "
                "generic anchor with descriptive text that fits. Keep the destination and check the link after publishing."
            ),
        })
    anchor_fixes.sort(key=lambda item: (rank(item["target_url"]), item["source_url"], item["current_anchor"]))

    notes = [
        "Use both Search Console exports from the same property. Performance data covers your chosen date range.",
        "The Google Links report is sampled, may be stale and groups by canonical URL. "
        "An unreported count is unknown. A low count is a review signal.",
        "Targets are ordered by focus keyword matches, then impressions and clicks. "
        "Review business importance and page type before linking.",
        "URLs are matched by scheme, host, path and query after removing fragments and default ports. "
        "Review redirects and canonical variants when exports do not match.",
    ]
    if pages:
        notes.append("Crawl coverage is limited to the supplied exports. Zero observed inlinks "
                     "is not proof that a target is orphaned. Verify candidates on the live pages.")
        if not inlinks.has_anchor_columns:
            notes.append("Include Anchor Text and Link Position in All Inlinks to review generic content anchors.")
    else:
        notes.append("Candidate sources use URL wording only. Check relevance, indexability and existing links by hand.")
    if links and not performance.keys() & links.keys():
        notes.append("The exports share no exact URLs. Verify property scope and canonical URL forms.")
    if keywords:
        notes.append("Focus keyword matches use URL wording and any supplied titles or headings. "
                     "The Pages export does not contain keyword-level ranking data.")
    result = {
        "status": "ok",
        "mode": "with_crawl" if pages else "search_console_only",
        "website_url": scope.url if scope else None,
        "focus_keywords": keywords,
        "focus_keywords_without_a_visible_page_match": [
            keyword for keyword in keywords if not any(keyword in matches.get(url, []) for url in visible)
        ],
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
            "crawl_anchor_improvements": len(anchor_fixes) if pages and inlinks.has_anchor_columns else None,
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
        "crawl_anchor_improvements": anchor_fixes[:max_targets],
        "interpretation_notes": notes,
        "references": [
            "https://support.google.com/webmasters/answer/9049606",
            "https://developers.google.com/search/docs/crawling-indexing/links-crawlable",
        ],
    }
    if worklist:
        worklist.enrich(result, visible, describe, rank, anchor_fixes, source_fixes, max_targets, sources_per_target)
    return result


def read_file(path: Path, *, binary=False):
    with path.open("rb") as file:
        content = file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} exceeds the 8 MiB limit. Export a focused subset.")
    return content if binary else content.decode("utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", type=Path)
    parser.add_argument("--performance", type=Path)
    parser.add_argument("--crawl-pages", type=Path)
    parser.add_argument("--crawl-inlinks", type=Path)
    parser.add_argument("--max-targets", type=int, default=20)
    parser.add_argument("--website", help="Website URL, domain or target subdirectory")
    parser.add_argument("--keyword", action="append", default=[], help="Focus phrase. Repeat for multiple phrases.")
    parser.add_argument("--excel", type=Path, help="Write an Excel action plan to this path.")
    parser.add_argument("--worklist", type=Path, help="Write the sprint worklist CSV.")
    parser.add_argument("--priority-url", action="append", default=[], help="Repeat for each client priority URL.")
    parser.add_argument("--snippets", type=Path, help="Optional Main Content CSV.")
    parser.add_argument("--page-queries", type=Path, help="Optional Page and Query CSV.")
    parser.add_argument("--queries-page-url", help="Page for a single-page Queries.csv.")
    parser.add_argument("--redirects", type=Path, help="Optional All Redirects CSV.")
    parser.add_argument("--verify", type=Path, help="Saved Excel audit report or legacy worklist CSV.")
    parser.add_argument("--verification-excel", type=Path, help="Write the follow-up report as one Excel worksheet.")
    parser.add_argument("--verification-csv", type=Path, help="Write the annotated verification CSV.")
    args = parser.parse_args()
    try:
        if args.verify:
            if not args.crawl_pages or not args.crawl_inlinks:
                parser.error("--verify needs --crawl-pages and --crawl-inlinks.")
            from link_report import create_workbook, workbook_worklist
            from link_worklist import verify, worklist_csv

            saved = read_file(args.verify, binary=True)
            worklist = workbook_worklist(saved) if saved.startswith(b"PK\x03\x04") else saved.decode("utf-8-sig")
            tasks = verify(worklist, read_file(args.crawl_pages), read_file(args.crawl_inlinks))
            if args.verification_excel:
                args.verification_excel.write_bytes(create_workbook({}, tasks, verification=True))
            if args.verification_csv:
                args.verification_csv.write_bytes(worklist_csv(tasks, ("verification_status", "verification_evidence")))
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
            return
        if not args.links or not args.performance:
            parser.error("an audit needs --links and --performance.")
        if args.verification_csv or args.verification_excel:
            parser.error("Verification outputs need --verify.")
        result = audit(
            read_file(args.links), read_file(args.performance),
            read_file(args.crawl_pages) if args.crawl_pages else None,
            read_file(args.crawl_inlinks) if args.crawl_inlinks else None,
            args.max_targets,
            website_url=args.website,
            focus_keywords=args.keyword,
            priority_urls=args.priority_url,
            snippets_csv=read_file(args.snippets) if args.snippets else None,
            page_queries_csv=read_file(args.page_queries) if args.page_queries else None,
            queries_page_url=args.queries_page_url,
            redirects_csv=read_file(args.redirects) if args.redirects else None,
            include_worklist=bool(args.worklist or args.excel),
        )
        if args.worklist or args.excel:
            tasks = result.pop("worklist_tasks", [])
        if args.worklist:
            from link_worklist import worklist_csv

            args.worklist.write_bytes(worklist_csv(tasks))
        if args.excel:
            from link_report import create_workbook

            args.excel.write_bytes(create_workbook(result, tasks))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
