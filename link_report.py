"""Generate a portable Excel action plan from the audit findings."""

import io
import json
import re
from collections import Counter
from pathlib import Path
from textwrap import wrap
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from link_worklist import ACTION_LABELS, FIELDS, OUTSIDE, worklist_csv

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCUMENT_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

def action_rows(report: dict) -> list[tuple]:
    """One row per proposed link or target that needs investigation."""
    rows = []
    priority = 0
    for key in ("priority_targets_with_observed_low_counts", "coverage_checks_for_unreported_targets"):
        for target in report[key]:
            priority += 1
            common = (
                ", ".join(target["matched_focus_keywords"]), target["search_impressions"],
                target["search_clicks"], target["gsc_reported_internal_links"],
            )
            observed_count = target["gsc_reported_internal_links"] is not None
            for source in target["candidate_sources"]:
                crawl_observed = source["existing_source_to_target_link"] != "unknown"
                if not observed_count:
                    action = "Verify coverage first"
                    start = "Check the target’s actual existing inlinks first. "
                elif not crawl_observed:
                    action = "Verify source first"
                    start = "Check that the source discusses the topic and does not already link to the target. "
                else:
                    action = "Review and add link"
                    start = ""
                evidence = (
                    "Titles, headings, and crawl inlinks" if crawl_observed else "URL wording only"
                )
                instructions = (
                    start + source["placement_hint"] + " Confirm the destination is indexable and the link "
                    "is absent. Add one contextual link to the target with a natural anchor. "
                    "Publish and click the link to verify it."
                )
                rows.append((
                    priority, action, target["target_url"], source["source_url"], source["suggested_anchor"],
                    *common, evidence, instructions, "To review",
                ))
            if not target["candidate_sources"]:
                action = (
                    "Check destination first" if target["eligible_in_crawl"] is False else "Find source pages"
                )
                instructions = target["recommended_action"]
                if target["eligible_in_crawl"] is not False:
                    instructions += " Export a crawl with page titles and inlinks, or identify a related guide or category manually."
                rows.append((
                    priority, action, target["target_url"], None, None, *common,
                    "Reported low count" if observed_count else "Count unknown in sampled report",
                    instructions, "To investigate",
                ))
    return rows


def audit_totals(report: dict, rows: list[tuple]) -> dict:
    return {
        "targets_in_plan": len({row[2] for row in rows}),
        "proposed_source_target_pairs": sum(row[3] is not None for row in rows),
        "targets_to_investigate": sum(row[3] is None for row in rows),
        "redirect_or_error_links_in_plan": len(report["crawl_source_link_fixes"]),
        "anchor_improvements_in_plan": len(report.get("crawl_anchor_improvements", [])),
    }


def existing_link_actions(report: dict) -> tuple[list[dict], list[dict]]:
    repairs = [{
        "action": "Replace redirect link" if item["issue"] == "link_to_redirect" else "Repair broken link",
        "source_url": item["source_url"],
        "target_url": item["target_url"],
        "http_status": item["target_http_status"],
        "evidence": "HTTP status observed in the supplied crawl.",
        "what_to_do": item["suggested_fix"],
        "status": "To review",
    } for item in report["crawl_source_link_fixes"]]
    anchor_edits = [{
        "action": "Improve existing anchor",
        "source_url": item["source_url"],
        "target_url": item["target_url"],
        "current_anchor": item["current_anchor"],
        "suggested_anchor": item["suggested_anchor"],
        "evidence": item["evidence"],
        "what_to_do": item["suggested_fix"],
        "status": "To review",
    } for item in report.get("crawl_anchor_improvements", [])]
    return repairs, anchor_edits


HEADERS = ("Priority", "Finding and owner", "Page to edit", "Link destination",
           "How to fix it", "What the audit found", "Status", "Task data")
WIDTHS = (11, 23, 25, 26, 54, 39, 14, 12)
WORKSHEET = "Audit and fixes"
TABLE_HEADER_ROW = 10
LOGO_PATH = Path(__file__).resolve().parent / "plugin" / "assets" / "logo.png"
DEVELOPER_EMAIL = "ahmadmohee@gmail.com"
EXPERT_AUDIT_NOTE = (
    "Developed by Ahmad Mohee. Need your audit performed by an expert? "
    f"Contact Ahmad Mohee at {DEVELOPER_EMAIL}."
)
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
SHEET_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
MAX_WORKSHEET_BYTES = 48 * 1024 * 1024
TITLE, HEADER, BODY, LINK, LABEL, NOTE, HIGH, STATUS, SHADED, SHADED_LINK = range(1, 11)
STATUS_LABELS = {"verified": "Verified", "still present": "Still open", OUTSIDE: "Check manually"}


def workbook_style_xml():
    root = ET.Element("styleSheet", xmlns=SPREADSHEET_NS)
    fonts = ET.SubElement(root, "fonts", count="5")
    for size, color, bold, underline in (
        (11, "1A3040", False, False), (22, "163D52", True, False),
        (11, "FFFFFF", True, False), (11, "12608B", False, True), (11, "163D52", True, False),
    ):
        font = ET.SubElement(fonts, "font")
        if bold:
            ET.SubElement(font, "b")
        if underline:
            ET.SubElement(font, "u")
        ET.SubElement(font, "sz", val=str(size))
        ET.SubElement(font, "color", rgb="FF" + color)
        ET.SubElement(font, "name", val="Calibri")
    fills = ET.SubElement(root, "fills", count="6")
    for pattern, color in (
        ("none", ""), ("gray125", ""), ("solid", "163D52"), ("solid", "EEF5F8"),
        ("solid", "FDEBD5"), ("solid", "EAF4F1"),
    ):
        fill = ET.SubElement(ET.SubElement(fills, "fill"), "patternFill", patternType=pattern)
        if color:
            ET.SubElement(fill, "fgColor", rgb="FF" + color)
            ET.SubElement(fill, "bgColor", indexed="64")
    border = ET.SubElement(ET.SubElement(root, "borders", count="1"), "border")
    for side in ("left", "right", "top", "bottom", "diagonal"):
        ET.SubElement(border, side)
    default = dict(numFmtId="0", fontId="0", fillId="0", borderId="0")
    ET.SubElement(ET.SubElement(root, "cellStyleXfs", count="1"), "xf", **default)
    styles = ET.SubElement(root, "cellXfs", count="11")
    ET.SubElement(styles, "xf", **default, xfId="0")
    for font, fill in ((1, 0), (2, 2), (0, 0), (3, 0), (4, 3),
                       (0, 3), (4, 4), (4, 5), (0, 3), (3, 3)):
        style = ET.SubElement(styles, "xf", numFmtId="0", fontId=str(font), fillId=str(fill), borderId="0",
                              xfId="0", applyFont="1", applyFill="1", applyAlignment="1")
        ET.SubElement(style, "alignment", vertical="center", wrapText="1", indent="1")
    ET.SubElement(ET.SubElement(root, "cellStyles", count="1"), "cellStyle", name="Normal", xfId="0", builtinId="0")
    return ET.tostring(root, encoding="utf-8")


def short_url(url):
    parsed = urlsplit(url)
    path = unquote(parsed.path) or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def describe_task(task, targets):
    action = task["action"]
    source = task.get("source_url") or ""
    destination = task["destination_url"]
    affected = task.get("affected_urls", [])
    replacement = task.get("replacement_url") or ""
    anchor = task.get("anchor") or ""
    section = task.get("section") or ""
    sentence = task.get("sentence") or ""
    target = targets.get(destination, {})
    issue = ACTION_LABELS[action]
    target_note = ""
    evidence = task.get("evidence") or ""
    target_eligibility = target.get("eligible_in_crawl")
    default_status = "Check first" if action == "review_candidate" else "To do"
    if action == "add_link":
        issue = "Add a link in the article"
        location = f'Find this sentence in “{section}”.' if section else "Find the sentence below."
        if task.get("anchor_edit") == "already in sentence":
            edit = f'Select the words “{anchor}” and link them to the destination shown.'
        else:
            edit = f'Work “{anchor}” naturally into the sentence, then link those words to the destination shown.'
        instructions = f'{location}\n“{sentence}”\n{edit} Publish and click the link to check it.'
    elif action == "review_candidate":
        issue = "Check where a link fits"
        if source:
            instructions = "Open the suggested page. Look for a paragraph on the same topic and check for an existing link. Add a link only if it helps the reader."
            if sentence:
                instructions += f' Review the passage “{sentence}”.'
            if anchor:
                instructions += f' Consider linking the words “{anchor}”.'
        elif target_eligibility is False or "canonical" in task.get("instructions", ""):
            instructions = "Open the destination and check that it loads and can appear in Google. Check that the preferred URL is correct before looking for pages that could link to it."
        else:
            instructions = "Find a related article, guide or service page. Check whether it already links to the destination. Add a link in a relevant paragraph if it helps the reader."
    elif action == "repair_broken":
        issue = "Broken link"
        instructions = ("Find the link to the URL shown. Replace it with a working page that serves the same purpose, "
                        "or remove the link if there’s no suitable replacement. Publish and click the updated link.")
        target_note = "The linked page returned an error in the crawl. Readers may reach a dead end."
    elif action == "replace_redirect":
        issue = "Link goes through a redirect"
        instructions = ("Confirm the proposed destination is the page readers should land on. "
                        f"Replace links pointing to {short_url(destination)} with the direct URL shown."
                        if replacement else
                        "Open the old URL and identify the correct final page. Update the link to point directly to that page.")
        instructions += " Publish and click the updated link."
        if section:
            instructions += f" The crawl points to a shared {task.get('link_position') or 'template'}. Check it and the affected pages below."
        target_note = "The crawl found an extra redirect. A direct link takes readers straight to the intended page."
    else:
        issue = "Unclear link text"
        instructions = (f'Find “{task.get("previous_anchor", "")}” in the page body. Change the linked words to “{anchor}”. '
                        "Publish and click the link to check it.")
        target_note = "The existing link text gives readers little information about the destination."

    if action in {"add_link", "review_candidate"} and target:
        body_links, menu_links = target.get("crawl_content_linking_urls"), target.get("crawl_navigation_linking_urls")
        reported = target.get("gsc_reported_internal_links")
        impressions = target.get("search_impressions")
        facts = []
        if body_links is not None:
            facts.append(f"{body_links:,} pages link from their main content in the crawl")
        if menu_links is not None and (menu_links or not body_links):
            facts.append(f"{menu_links:,} pages link from their menus")
        if impressions is not None:
            facts.append(f"{impressions:,} Google Search impressions")
        if reported is not None:
            facts.append(f"{reported:,} links in Google's sampled report")
        if target.get("client_priority"):
            facts.insert(0, "You marked this page as a priority")
        target_note = ". ".join(facts) + "." if facts else ""
    if action == "add_link":
        target_note += (" " if target_note else "") + "A link in the article gives readers a relevant next step."
    elif action == "review_candidate":
        target_note += (" " if target_note else "") + "Check the suggested placement before publishing."
    if action in {"repair_broken", "replace_redirect"} and evidence:
        target_note += f" {evidence}"
    verification = task.get("verification_status")
    if verification:
        target_note += f' Latest crawl. {task.get("verification_evidence") or ""}'
    shown_destination = replacement or destination
    if len(affected) > 1:
        shown_source = f"{len(affected):,} pages. See affected pages below."
    elif source:
        shown_source = short_url(source)
    else:
        shown_source = "Choose a suitable page"
    values = (
        task.get("priority", ""), f"{issue}\n{task.get('suggested_owner') or ''}".rstrip(),
        shown_source, short_url(shown_destination), instructions, target_note or evidence,
        STATUS_LABELS.get(verification, default_status),
        json.dumps({key: task[key] for key in FIELDS if key in task and key != "affected_urls"}, ensure_ascii=False),
    )
    links = {"D": shown_destination}
    if source and len(affected) <= 1:
        links["C"] = source
    return values, links


def create_workbook(report: dict, tasks: list[dict], *, verification=False) -> bytes:
    """Write one readable worksheet and retain the task data for follow-up crawls."""
    targets = {
        row["target_url"]: row for key in (
            "priority_targets_with_observed_low_counts", "coverage_checks_for_unreported_targets",
            "contextual_link_opportunities",
        ) for row in report.get(key, [])
    }
    source = report.get("website_url") or "Site inferred from the supplied exports"
    counts = Counter(task["action"] for task in tasks)
    if verification:
        status = Counter(task["verification_status"] for task in tasks)
        outcome = (f'{status["verified"]:,} verified. {status["still present"]:,} still open. '
                   f"{status[OUTSIDE]:,} need a manual check because the new crawl has limited coverage.")
        source = "Compared the saved audit with the new crawl."
    else:
        findings = [f"{counts[action]:,} {single if counts[action] == 1 else plural}" for action, single, plural in (
            ("add_link", "link to add", "links to add"),
            ("review_candidate", "item to check", "items to check"),
            ("repair_broken", "broken link to repair", "broken links to repair"),
            ("replace_redirect", "redirect to update", "redirects to update"),
            ("improve_anchor", "link-text edit", "link-text edits"),
        ) if counts[action]]
        outcome = (", ".join(findings) + "." if findings else
                   "The supplied exports produced no recommended edits.")
        for task in tasks:
            target = targets.get(task["destination_url"], {})
            if (task.get("priority") == "High" and task["action"] == "add_link"
                    and target.get("crawl_content_linking_urls") == 0):
                outcome += (f' Start with {short_url(task["destination_url"])}. '
                            "The crawl found no links to this page from main content.")
                break
    title = "Internal linking audit verification" if verification else "Internal linking audit and fixes"
    sheet = ET.Element("worksheet", xmlns=SPREADSHEET_NS, attrib={"xmlns:r": DOCUMENT_RELS_NS})
    views = ET.SubElement(sheet, "sheetViews")
    view = ET.SubElement(views, "sheetView", workbookViewId="0", showGridLines="0", zoomScale="85")
    ET.SubElement(view, "pane", ySplit=str(TABLE_HEADER_ROW), topLeftCell=f"A{TABLE_HEADER_ROW + 1}",
                  activePane="bottomLeft", state="frozen")
    columns = ET.SubElement(sheet, "cols")
    for index, width in enumerate(WIDTHS, 1):
        settings = {"hidden": "1"} if index == len(WIDTHS) else {}
        ET.SubElement(columns, "col", min=str(index), max=str(index), width=str(width), customWidth="1", **settings)
    data = ET.SubElement(sheet, "sheetData")
    merges = ET.Element("mergeCells")
    hyperlinks = ET.Element("hyperlinks")
    links = ET.Element("Relationships", xmlns=PACKAGE_RELS_NS)

    def add_row(number, values, height, styles, urls=None):
        row = ET.SubElement(data, "row", r=str(number), ht=str(height), customHeight="1")
        for index, (value, style) in enumerate(zip(values, styles)):
            if value is None:
                continue
            column = chr(ord("A") + index)
            address = f"{column}{number}"
            cell = ET.SubElement(row, "c", r=address, s=str(style), t="inlineStr")
            string = ET.SubElement(ET.SubElement(cell, "is"), "t")
            string.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            string.text = ILLEGAL_XML.sub("", str(value))[:32_767]
            if urls and column in urls:
                relation = f"hyperlink{len(links) + 1}"
                ET.SubElement(links, "Relationship", Id=relation, Target=urls[column],
                              Type=DOCUMENT_RELS_NS + "/hyperlink", TargetMode="External")
                ET.SubElement(hyperlinks, "hyperlink", ref=address, attrib={"r:id": relation})

    def note(number, label, value, height=23, url=None):
        add_row(number, (label, None, value), height, (LABEL, LABEL, SHADED_LINK if url else NOTE),
                {"C": url} if url else None)
        ET.SubElement(merges, "mergeCell", ref=f"A{number}:B{number}")
        ET.SubElement(merges, "mergeCell", ref=f"C{number}:G{number}")

    add_row(1, (None,), 9, (BODY,))
    add_row(2, (None, title), 58, (BODY, TITLE))
    ET.SubElement(merges, "mergeCell", ref="B2:G2")
    note(3, "Site and scope", str(source)[:350])
    note(4, "What we found", outcome, 38 if len(outcome) > 140 else 25)
    start = ("Filter Status to Still open or Check manually, then follow the instructions beside each finding."
             if verification else "Filter Priority to High. Open Page to edit and follow How to fix it.")
    note(5, "Start here", start if tasks else "Check that the exports cover the site and period you intended to audit.")
    note(6, "Track the work", "After publishing, open the live page and click the updated link. Set Status to Done when it works.")
    note(7, "Check the next crawl", "Upload this workbook with fresh Screaming Frog Internal and All Inlinks CSVs. The next report shows which fixes the crawl confirmed.")
    note(8, "Read the evidence", ("The crawl only covers the pages and links it recorded. Search Console link counts are a sample." if verification or report.get("mode") == "with_crawl" else
         "Search Console link counts are a sample. Add a crawl for link locations, broken links and redirects."))
    note(9, "Developer and expert audits", EXPERT_AUDIT_NOTE, 28, f"mailto:{DEVELOPER_EMAIL}")
    add_row(TABLE_HEADER_ROW, HEADERS, 30, (HEADER,) * len(HEADERS))
    grouped = []
    row_number = TABLE_HEADER_ROW
    for task in tasks:
        row_number += 1
        values, urls = describe_task(task, targets)
        shaded = row_number % 2 == 0
        base = SHADED if shaded else BODY
        link = SHADED_LINK if shaded else LINK
        styles = (HIGH if values[0] == "High" else base, base, link if "C" in urls else base,
                  link, base, base, STATUS, BODY)
        line_counts = [sum(max(len(wrap(line, max(6, int(width * .94)))), 1)
                           for line in value.splitlines()) for value, width in zip(values[:7], WIDTHS)]
        add_row(row_number, values, min(366, max(44, 12 + 15 * max(line_counts))), styles, urls)
        if len(task.get("affected_urls", [])) > 1:
            grouped.append(task)
    if tasks:
        ET.SubElement(sheet, "autoFilter", ref=f"A{TABLE_HEADER_ROW}:H{row_number}")
    if grouped:
        row_number += 2
        add_row(row_number, ("Pages affected by shared fixes",), 26, (LABEL,))
        ET.SubElement(merges, "mergeCell", ref=f"A{row_number}:G{row_number}")
        for task in grouped:
            for url in task["affected_urls"]:
                row_number += 1
                label = ACTION_LABELS[task["action"]]
                metadata = json.dumps({"task": task["task_id"], "page": url})
                add_row(row_number, ("", label, short_url(url), short_url(task["destination_url"]), None, None, None, metadata),
                        37, (BODY, BODY, LINK, LINK, BODY, BODY, BODY, BODY), {"C": url, "D": task["destination_url"]})

    sheet.append(merges)
    if len(hyperlinks):
        sheet.append(hyperlinks)
    ET.SubElement(sheet, "drawing", attrib={"r:id": "logoDrawing"})
    ET.SubElement(links, "Relationship", Id="logoDrawing", Type=DOCUMENT_RELS_NS + "/drawing",
                  Target="../drawings/drawing1.xml")
    return package_workbook(sheet, links)


def workbook_logo_drawing():
    # DrawingML uses 9,525 EMUs per pixel. The square sits inside the title row.
    size = 56 * 9_525
    return f'''<xdr:wsDr xmlns:xdr="{SHEET_DRAWING_NS}" xmlns:a="{DRAWING_NS}" xmlns:r="{DOCUMENT_RELS_NS}">
  <xdr:oneCellAnchor>
    <xdr:from><xdr:col>0</xdr:col><xdr:colOff>76200</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>95250</xdr:rowOff></xdr:from>
    <xdr:ext cx="{size}" cy="{size}"/>
    <xdr:pic>
      <xdr:nvPicPr><xdr:cNvPr id="1" name="Internal Linking Auditor logo" descr="Internal Linking Auditor logo"/>
        <xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>
      <xdr:blipFill><a:blip r:embed="logoImage"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>
      <xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{size}" cy="{size}"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>
    </xdr:pic>
    <xdr:clientData/>
  </xdr:oneCellAnchor>
</xdr:wsDr>'''.encode("utf-8")


def package_workbook(sheet, links):
    workbook = ET.Element("workbook", xmlns=SPREADSHEET_NS, attrib={"xmlns:r": DOCUMENT_RELS_NS})
    views = ET.SubElement(workbook, "bookViews")
    ET.SubElement(views, "workbookView", activeTab="0")
    ET.SubElement(ET.SubElement(workbook, "sheets"), "sheet", name=WORKSHEET, sheetId="1", attrib={"r:id": "sheet1"})
    root = ET.Element("Relationships", xmlns=PACKAGE_RELS_NS)
    ET.SubElement(root, "Relationship", Id="office", Type=DOCUMENT_RELS_NS + "/officeDocument", Target="xl/workbook.xml")
    relationships = ET.Element("Relationships", xmlns=PACKAGE_RELS_NS)
    ET.SubElement(relationships, "Relationship", Id="styles", Type=DOCUMENT_RELS_NS + "/styles", Target="styles.xml")
    ET.SubElement(relationships, "Relationship", Id="sheet1", Type=DOCUMENT_RELS_NS + "/worksheet", Target="worksheets/sheet1.xml")
    types = ET.Element("Types", xmlns=CONTENT_TYPES_NS)
    ET.SubElement(types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, "Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(types, "Default", Extension="png", ContentType="image/png")
    ET.SubElement(types, "Override", PartName="/xl/drawings/drawing1.xml",
                  ContentType="application/vnd.openxmlformats-officedocument.drawing+xml")
    for part, suffix in (
        ("workbook.xml", "sheet.main+xml"), ("styles.xml", "styles+xml"), ("worksheets/sheet1.xml", "worksheet+xml"),
    ):
        ET.SubElement(types, "Override", PartName=f"/xl/{part}",
                      ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{suffix}")

    result = io.BytesIO()
    drawing_rels = ET.Element("Relationships", xmlns=PACKAGE_RELS_NS)
    ET.SubElement(drawing_rels, "Relationship", Id="logoImage", Type=DOCUMENT_RELS_NS + "/image",
                  Target="../media/logo.png")
    with ZipFile(result, "w", ZIP_DEFLATED) as archive:
        for name, element in (
            ("[Content_Types].xml", types), ("_rels/.rels", root), ("xl/workbook.xml", workbook),
            ("xl/_rels/workbook.xml.rels", relationships), ("xl/worksheets/sheet1.xml", sheet),
            ("xl/drawings/_rels/drawing1.xml.rels", drawing_rels),
        ):
            archive.writestr(name, ET.tostring(element, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/styles.xml", workbook_style_xml())
        archive.writestr("xl/drawings/drawing1.xml", workbook_logo_drawing())
        archive.writestr("xl/media/logo.png", LOGO_PATH.read_bytes())
        if len(links):
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", ET.tostring(links, encoding="utf-8"))
    return result.getvalue()


def workbook_worklist(content: bytes) -> str:
    """Read the same-sheet task data after Excel has saved or sorted the workbook."""
    ns = {"s": SPREADSHEET_NS}

    def xml(archive, name):
        entry = archive.getinfo(name)
        if entry.file_size > MAX_WORKSHEET_BYTES:
            raise ValueError("This Excel report is too large to verify.")
        raw = archive.read(entry).decode("utf-8-sig")
        if "<!DOCTYPE" in raw or "<!ENTITY" in raw:
            raise ValueError("The Excel report contains unsupported XML.")
        return ET.fromstring(raw)

    def strings(element):
        return "".join(node.text or "" for node in element.findall(".//s:t", ns))

    tasks = []
    affected = {}
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            sheet = xml(archive, "xl/worksheets/sheet1.xml")
            shared = ([strings(child) for child in xml(archive, "xl/sharedStrings.xml")]
                      if "xl/sharedStrings.xml" in archive.namelist() else [])
        for row in sheet.findall("s:sheetData/s:row", ns):
            number = row.get("r", "")
            cell = next((cell for cell in row if cell.get("r") == f"H{number}"), None)
            if cell is None:
                continue
            if cell.get("t") == "s":
                index = int(cell.findtext("s:v", namespaces=ns))
                if index < 0:
                    raise ValueError("Invalid shared string reference.")
                value = shared[index]
            else:
                value = strings(cell)
            if not value.startswith("{"):
                continue
            item = json.loads(value)
            if not isinstance(item, dict):
                continue
            if "task_id" in item:
                tasks.append(item)
            elif isinstance(item.get("task"), str) and isinstance(item.get("page"), str):
                affected.setdefault(item["task"], []).append(item["page"])
        if not tasks:
            raise ValueError("Use the Excel report that contains the original audit tasks.")
        for task in tasks:
            task["affected_urls"] = affected.get(task["task_id"], [])
        return worklist_csv(tasks).decode("utf-8-sig")
    except (BadZipFile, ET.ParseError, UnicodeError, IndexError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError("Use an intact Excel report exported by Internal Linking Auditor.") from error
