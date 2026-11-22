"""Turn crawl evidence into an editorial worklist and verify it on a later crawl."""

import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict

from audit import (
    CONTENT, FOOTER, NAVIGATION, UNKNOWN, anchor_key, anchor_suggestion,
    csv_rows, locality, normal_url, read_inlinks, read_pages, tokens, whole,
)

SNIPPETS = {
    "url": ("url", "address", "source url"),
    "heading": ("heading", "section", "section heading", "heading 1"),
    "text": ("text", "main content", "main content 1", "snippet", "snippet 1"),
}
QUERIES = {
    "url": ("page", "url", "top pages"),
    "query": ("query", "top queries"),
    "impressions": ("impressions",),
    "clicks": ("clicks",),
}
REDIRECTS = {
    "old": ("address", "original address", "redirect address", "redirect url", "original redirect url"),
    "final": ("final address", "final url", "final destination"),
    "status": ("final status code", "final http status"),
    "loop": ("redirect loop",),
}
FIELDS = (
    "task_id", "priority", "action", "source_url", "destination_url", "section", "sentence",
    "anchor", "anchor_edit", "link_position", "previous_anchor", "replacement_url",
    "final_url", "final_http_status", "baseline_pair", "affected_urls", "evidence", "suggested_owner", "instructions",
)
ACTION_LABELS = {
    "add_link": "Add contextual link", "review_candidate": "Review candidate",
    "repair_broken": "Repair broken destination", "replace_redirect": "Replace redirect link",
    "improve_anchor": "Improve existing anchor",
}
CSV_MIME_TYPE = "text/csv; charset=utf-8"
OUTSIDE = "outside the new crawl’s coverage"
MAX_SNIPPETS = 200


def read_snippets(content, pasted):
    rows = list(pasted)
    if content is not None:
        for _, row in csv_rows(content, "Main Content CSV", SNIPPETS, {"url", "text"}):
            rows.append(row)
            if len(rows) > MAX_SNIPPETS:
                break
    if len(rows) > MAX_SNIPPETS:
        raise ValueError(f"Supply up to {MAX_SNIPPETS} main-content snippets.")
    snippets = defaultdict(list)
    for row in rows:
        url = normal_url(row["url"])
        heading = " ".join((row.get("heading") or "").split())[:200]
        text = row["text"].strip()
        sentences = re.split(r"(?<=[.!?؟。])\s+|[\r\n]+", text[:12_000])
        if len(text) > 12_000:
            sentences.pop()
        for sentence in sentences:
            sentence = sentence.strip()
            if 20 <= len(sentence) <= 550:
                snippets[url].append((heading, sentence, tokens(sentence)))
    return snippets


def read_queries(content, page_url):
    if page_url and content is None:
        raise ValueError("Provide a queries CSV with queries_page_url.")
    rows = defaultdict(dict)
    if content is not None:
        fixed_url = normal_url(page_url) if page_url else None
        for line, row in csv_rows(content, "Page Queries CSV", QUERIES, {"query"}):
            url = normal_url(row["url"]) if row.get("url") else fixed_url
            if not url:
                raise ValueError("Page Queries CSV needs a Page column or an explicitly identified page URL.")
            if fixed_url and fixed_url != url:
                raise ValueError("The queries export contains a different URL from the identified page.")
            phrase = " ".join(row["query"].split())
            if len(phrase) > 100 or not tokens(phrase):
                continue
            scores = tuple(whole(row.get(key) or "0") for key in ("impressions", "clicks"))
            key = phrase.casefold()
            entry = (*scores, phrase)
            rows[url][key] = max(entry, rows[url].get(key, entry))
    return {url: [entry[2] for entry in sorted(ranked.values(), reverse=True)[:5]]
            for url, ranked in rows.items()}


def read_redirects(content, known_redirects):
    rows = defaultdict(set)
    if content is None:
        return rows
    for _, row in csv_rows(content, "All Redirects CSV", REDIRECTS, {"old", "final", "status"}):
        if not row["final"] or not row["status"] or row.get("loop", "").casefold() in {"true", "yes", "1"}:
            continue
        old = normal_url(row["old"])
        if old not in known_redirects:
            continue
        final = normal_url(row["final"])
        rows[old].add((final, whole(row["status"])))
    return rows


def make_task(action, target, *, priority="Medium", source="", identity="", **values):
    key = json.dumps([action, source, target, identity], ensure_ascii=False, separators=(",", ":"))
    task_id = "ila-" + hashlib.sha256(key.encode()).hexdigest()[:20]
    return dict(task_id=task_id, priority=priority, action=action, source_url=source,
                destination_url=target, **values)


class Worklist:
    def __init__(self, scope, pages, performance, links, inlinks, edges, matcher, eligible,
                 crawled_sources, priority_urls, snippets_csv, main_content, page_queries_csv,
                 queries_page_url, redirects_csv):
        if len(priority_urls) > 50:
            raise ValueError("Supply up to 50 priority URLs.")
        self.priorities = {normal_url(url.strip()) for url in priority_urls}
        if scope and any(not scope.includes(url) for url in self.priorities):
            raise ValueError("Priority URLs must be inside the selected website or directory.")
        if not scope:
            hosts = {locality(url)[1].removeprefix("www.") for url in performance}
            if any(locality(url)[1].removeprefix("www.") not in hosts for url in self.priorities):
                raise ValueError("Priority URLs must belong to the audited property.")
        self.pages = pages
        self.performance = performance
        self.links = links
        self.edges = edges
        self.matcher = matcher
        self.inlinks = inlinks
        self.eligible = eligible
        self.crawled_sources = crawled_sources
        self.snippets = read_snippets(snippets_csv, main_content)
        self.queries = read_queries(page_queries_csv, queries_page_url)
        if redirects_csv is not None and not pages:
            raise ValueError("Supply the Internal pages and All Inlinks CSVs with the redirects export.")
        self.redirects = read_redirects(redirects_csv, {
            url for url, page in pages.items() if page.status in {301, 302, 303, 307, 308}
        })
        self.counts = defaultdict(Counter)
        self.position_available = any(flags & ~UNKNOWN for flags in (inlinks.positions or {}).values())
        for source, target in edges:
            flags = inlinks.positions[source, target]
            self.counts[target]["any"] += 1
            for label, bit in (("content", CONTENT), ("navigation", NAVIGATION),
                               ("footer", FOOTER), ("unknown", UNKNOWN)):
                if flags & bit:
                    self.counts[target][label] += 1

    def placement_candidates(self, target, focus, limit):
        phrases = []
        suggested, _ = anchor_suggestion(target, self.pages.get(target))
        for phrase in [*focus, *self.queries.get(target, []), suggested]:
            if phrase and len(tokens(phrase)) >= 2 and phrase not in phrases:
                phrases.append(phrase)
        if not phrases:
            return []
        core = self.matcher.page_terms(target)
        chosen = []
        for source, snippets in self.snippets.items():
            if source == target or locality(source) != locality(target):
                continue
            if source not in self.crawled_sources and (self.pages or source not in self.performance and source not in self.links):
                continue
            flags = (self.inlinks.positions or {}).get((source, target), 0)
            if flags & (CONTENT | UNKNOWN) or flags and flags & ~(NAVIGATION | FOOTER):
                continue
            possible = []
            for heading, sentence, words in snippets:
                shared = core & words
                if len(shared) < 2:
                    continue
                for phrase in phrases:
                    phrase_terms = tokens(phrase)
                    if not phrase_terms & core:
                        continue
                    match = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", sentence, re.I)
                    if not match and (len(shared) < 3 or not phrase_terms <= words | core):
                        continue
                    possible.append((
                        not bool(match), -len(shared), -len(phrase.split()), heading, sentence,
                        match[0] if match else phrase,
                    ))
            if possible:
                edit, score, specificity, heading, sentence, anchor = min(possible)
                chosen.append((edit, score, specificity, source, {
                    "source_url": source, "target_url": target, "section": heading, "sentence": sentence,
                    "suggested_anchor": anchor, "anchor_edit": "small edit" if edit else "already in sentence",
                    "existing_source_to_target_link": (
                        "navigation_or_footer_only" if flags else
                        "not_observed_in_supplied_crawl" if self.pages else "unknown"
                    ),
                    "evidence": "Supplied main-content snippet",
                    "placement_hint": f"Use the quoted sentence{f' in {heading}' if heading else ''}.",
                }))
        return [entry[-1] for entry in sorted(chosen)[:limit]]

    def context_counts(self, url):
        counts = self.counts.get(url, {})
        available = self.position_available and url in self.pages
        return {
            "crawl_distinct_linking_urls": counts.get("any", 0) if url in self.pages else None,
            "crawl_content_linking_urls": counts.get("content", 0) if available else None,
            "crawl_navigation_linking_urls": counts.get("navigation", 0) if available else None,
            "crawl_footer_linking_urls": counts.get("footer", 0) if available else None,
            "crawl_unclassified_linking_urls": counts.get("unknown", 0) if url in self.pages else None,
        }

    def enrich(self, report, visible, describe, rank, anchors, repairs, limit, sources_limit):
        low = report["priority_targets_with_observed_low_counts"]
        unreported = report["coverage_checks_for_unreported_targets"]
        contextual = []
        urls = sorted(visible.keys() | self.priorities, key=rank)
        for url in urls:
            counts = self.context_counts(url)
            weak = (url in self.eligible and counts["crawl_content_linking_urls"] is not None
                    and counts["crawl_content_linking_urls"] <= 1
                    and counts["crawl_unclassified_linking_urls"] == 0)
            if url in self.priorities or weak:
                contextual.append(describe(url, url not in self.links))
            if len(contextual) == limit:
                break
        report["contextual_link_opportunities"] = contextual
        tasks = self.repair_tasks(repairs)
        by_url = {target["target_url"]: target for target in [*unreported, *low, *contextual]}
        for url in sorted(by_url, key=rank):
            target = by_url[url]
            target.update(self.context_counts(url))
            chosen = self.placement_candidates(url, target["matched_focus_keywords"], sources_limit)
            if target["eligible_in_crawl"] is False:
                chosen = []
            candidates = [item for item in target["candidate_sources"]
                          if item["source_url"] not in {source["source_url"] for source in chosen}]
            target["candidate_sources"] = (chosen + candidates)[:sources_limit]
            target["client_priority"] = url in self.priorities
            target["supporting_queries"] = self.queries.get(url, [])
            content_count = target["crawl_content_linking_urls"]
            evidence = []
            if target["client_priority"]:
                evidence.append("Client priority URL")
            impressions = target["search_impressions"]
            if impressions is not None:
                evidence.append(f"{impressions:,} Search Console impressions")
            elif target["client_priority"]:
                evidence.append("No Performance Pages row supplied")
            if content_count is not None:
                evidence.append(f"{content_count} distinct main-content linking pages observed")
                if target["crawl_unclassified_linking_urls"]:
                    evidence.append("some linking pages have unclassified positions")
            gsc_count = target["gsc_reported_internal_links"]
            evidence.append(f"Google’s sampled link count is {gsc_count:,}" if gsc_count is not None
                            else "Google sampled link count unreported")
            if chosen:
                evidence.append(f"{len(chosen)} placement(s) supported by supplied text")
            target["priority_reason"] = ". ".join(evidence) + "."
            priority = "High" if target["client_priority"] or (content_count == 0 and chosen) else "Medium"
            for candidate in target["candidate_sources"] or [{}]:
                source = candidate.get("source_url", "")
                exact = candidate.get("sentence", "")
                add = bool(exact and self.pages)
                if add:
                    instruction = "Review the quoted sentence in the CMS, insert the link, and confirm the published destination."
                elif exact:
                    instruction = "Check for an existing contextual link and destination eligibility before editing the quoted sentence."
                elif source:
                    instruction = "Review the source in the CMS and find a suitable main-content passage before adding the link."
                elif target["eligible_in_crawl"] is False:
                    instruction = "Check target crawl coverage, status, indexability and canonical before choosing sources."
                else:
                    instruction = "Identify a relevant source page and check current links and destination eligibility."
                tasks.append(make_task(
                    "add_link" if add else "review_candidate", url, priority=priority, source=source,
                    section=candidate.get("section", ""), sentence=exact,
                    anchor=candidate.get("suggested_anchor") or "", anchor_edit=candidate.get("anchor_edit", ""),
                    baseline_pair="yes" if (source, url) in self.edges else "",
                    link_position="content" if source else "",
                    evidence=target["priority_reason"] + (
                        " " + candidate["evidence"].replace("_", " ") + "." if candidate else ""
                    ),
                    suggested_owner="Content editor" if source else "SEO specialist",
                    instructions=instruction,
                ))
        for finding in [*low, *unreported]:
            finding.update(by_url[finding["target_url"]])
        for edit in anchors[:limit]:
            tasks.append(make_task(
                "improve_anchor", edit["target_url"], source=edit["source_url"],
                identity=anchor_key(edit["current_anchor"]), previous_anchor=edit["current_anchor"],
                anchor=edit["suggested_anchor"], link_position="content",
                suggested_owner="Content editor", evidence=edit["evidence"],
                instructions=edit["suggested_fix"],
            ))
        priority_order = {"High": 0, "Medium": 1}
        action_order = {"add_link": 0, "repair_broken": 1, "replace_redirect": 2,
                        "improve_anchor": 3, "review_candidate": 4}
        tasks.sort(key=lambda task: (priority_order[task["priority"]], action_order[task["action"]]))
        report["worklist_tasks"] = tasks
        report["worklist_summary"] = dict(Counter(task["action"] for task in tasks))
        report["top_10_for_this_sprint"] = [task_preview(task) for task in tasks[:10]]
        report["interpretation_notes"].append(
            "Link-position counts are distinct source pages in each category. A source can appear in more than "
            "one category. Query exports guide relevance only. Page totals come from Performance Pages."
        )

    def repair_tasks(self, repairs):
        groups = defaultdict(list)
        for source, target, status, issue, _ in repairs:
            groups[target, status, issue].append(source)
        templates_by_target = defaultdict(list)
        for (target, position, path, _), sources in sorted((self.inlinks.templates or {}).items()):
            templates_by_target[target].append((position, path, sources))
        tasks = []
        for (target, status, issue), sources in groups.items():
            source_set = set(sources)
            hints = []
            positions = set()
            for position, path, matches in templates_by_target.get(target, []):
                affected = source_set & matches
                if len(affected) >= 2:
                    place = "navigation" if position == NAVIGATION else "footer"
                    positions.add(place)
                    hints.append(f"Possible shared {place} at {path} on {len(affected)} affected pages.")
            final = self.redirects.get(target, set())
            replacement, final_status = next(iter(final)) if len(final) == 1 else ("", "")
            valid_replacement = (
                replacement in self.eligible and replacement != target and final_status == 200
                and locality(target)[:2] == locality(replacement)[:2]
            )
            broken = issue == "link_to_error"
            if valid_replacement:
                instruction = "Confirm the final URL is the intended destination, then update every affected link."
            elif broken:
                instruction = "Choose the intended replacement or remove the broken links, then check every affected page."
            else:
                instruction = "Confirm the redirect’s intended destination before updating every affected link."
            if hints:
                instruction += " Check the indicated shared component and any other occurrences."
            tasks.append(make_task(
                "repair_broken" if broken else "replace_redirect", target,
                priority="High" if broken else "Medium",
                source=sources[0] if len(sources) == 1 else "",
                section=" ".join(hints)[:2500],
                link_position=", ".join(sorted(positions)),
                affected_urls=sorted(source_set), final_url=replacement,
                replacement_url=replacement if valid_replacement else "",
                final_http_status=final_status, suggested_owner="Developer",
                evidence=f"HTTP {status} observed. {len(source_set):,} affected source pages."
                         + (" Multiple final URLs or statuses appear in All Redirects." if len(final) > 1 else ""),
                instructions=instruction,
            ))
        return tasks


def task_preview(task):
    preview = dict(task)
    urls = task.get("affected_urls", [])
    if urls:
        preview["affected_url_count"] = len(urls)
        preview["affected_urls"] = urls[:5]
    return preview


def safe_cell(value):
    if isinstance(value, list):
        return "\n".join(value)
    value = str(value)
    if value.startswith(("'", "\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def worklist_csv(tasks, extra=()):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS + extra)
    writer.writeheader()
    for task in tasks:
        writer.writerow({key: safe_cell(task.get(key, "")) for key in writer.fieldnames})
    return stream.getvalue().encode("utf-8-sig")


def read_worklist(content):
    tasks = []
    seen = set()
    for _, row in csv_rows(content, "Worklist CSV", {field: (field,) for field in FIELDS},
                           {"task_id", "action", "source_url", "destination_url"}):
        task = {key: value[1:] if value.startswith("'") and safe_cell(value[1:]) == value else value
                for key, value in row.items()}
        task_id = task["task_id"]
        if not re.fullmatch(r"ila-[0-9a-f]{20}", task_id) or task_id in seen:
            raise ValueError("Use the saved worklist with its unique task IDs.")
        seen.add(task_id)
        if task["action"] not in ACTION_LABELS:
            raise ValueError("Use the saved worklist with its original action values.")
        for field in ("source_url", "destination_url", "replacement_url"):
            if task.get(field):
                task[field] = normal_url(task[field])
        if not task["destination_url"]:
            raise ValueError("Each worklist task needs a destination URL.")
        urls = task.get("affected_urls", "").splitlines() or ([task["source_url"]] if task["source_url"] else [])
        task["affected_urls"] = list(dict.fromkeys(normal_url(url.strip()) for url in urls if url.strip()))
        tasks.append(task)
    if not tasks:
        raise ValueError("The worklist has no task rows.")
    return tasks


def verify(worklist_text, pages_text, inlinks_text):
    tasks = read_worklist(worklist_text)
    pages = read_pages(pages_text)
    inspect = {(task["source_url"], task["destination_url"]) for task in tasks
               if task["action"] in {"add_link", "review_candidate", "improve_anchor"} and task["source_url"]}
    links = read_inlinks(inlinks_text, inspect_pairs=inspect)
    covered = {source for source, _ in links.edges if source in pages and pages[source].status == 200}
    for task in tasks:
        status, detail = verify_task(task, links, covered)
        task.update(verification_status=status, verification_evidence=detail)
    return tasks


def verify_task(task, links, covered):
    action, target = task["action"], task["destination_url"]
    sources = task["affected_urls"]
    if action in {"repair_broken", "replace_redirect"}:
        remaining = sum((source, target) in links.edges and source in covered for source in sources)
        if remaining:
            return "still present", f"The old destination is still linked from {remaining} affected source page(s)."
        if not sources or any(source not in covered for source in sources):
            return OUTSIDE, "At least one affected source has no usable outgoing-link coverage."
        replacement = task.get("replacement_url")
        if replacement:
            missing = sum((source, replacement) not in links.edges for source in sources)
            if missing:
                return "still present", f"The expected replacement was not observed on {missing} source page(s)."
            return "verified", "Old links absent and replacement observed on all affected source pages."
        return "verified", "Old links absent from all affected source pages with outgoing-link coverage."

    source = task["source_url"]
    if source not in covered:
        return OUTSIDE, "This source has no usable outgoing-link coverage."
    pair = (source, target)
    if pair not in links.edges:
        return "still present", "The requested source–destination link was not observed."
    sightings = links.observations.get(pair, set())
    text_sightings = {(position, anchor) for position, anchor, image in sightings if not image}
    intended = anchor_key(task.get("anchor", ""))
    content_links = {anchor for position, anchor in text_sightings if position == CONTENT}
    position_known = all(position != UNKNOWN for position, _, _ in sightings)
    if action == "improve_anchor":
        if anchor_key(task.get("previous_anchor", "")) in content_links:
            return "still present", "The previous anchor is still present on a content link."
        if intended and intended in content_links and position_known:
            return "verified", "New anchor observed in content and previous anchor not observed there."
        if not position_known or "" in content_links:
            return OUTSIDE, "The new crawl did not establish both content position and anchor wording."
        return "still present", "The requested replacement anchor was not observed in content."
    if content_links:
        if not intended or intended in content_links:
            return "verified", "Source–destination link observed in main content with the expected anchor when specified."
        if "" not in content_links:
            return "still present", "A content link exists, but its anchor differs from the worklist."
        return "verified", "Source–destination link observed in main content. Anchor wording was unavailable."
    if position_known:
        return "still present", "The link was observed outside main content or as an image."
    if task.get("baseline_pair") == "yes":
        return OUTSIDE, "This URL pair already existed. Link Position is needed to verify the new content link."
    unknown = {anchor for position, anchor in text_sightings if position == UNKNOWN}
    if not unknown:
        return OUTSIDE, "The crawl did not establish a text link in the requested placement."
    if intended and "" not in unknown and intended not in unknown:
        return "still present", "The pair was observed, but the expected anchor was not."
    return "verified", "Source–destination link observed. Link position was unavailable."
