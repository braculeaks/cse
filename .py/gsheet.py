"""
Export every tab of a Google Sheet's htmlview page to PDF and/or CSV.

Setup (once):
    pip install playwright pypdf
    playwright install chromium

Examples:
    python gsheet.py                         # PDFs of all tabs (+ ALL_TABS.pdf)
    python gsheet.py --csv                   # CSVs of all tabs
    python gsheet.py --pdf --csv             # both
    python gsheet.py --csv --combined        # also one ALL_TABS.csv with a "tab" column
    python gsheet.py --csv --only AAF ACH Tabular
    python gsheet.py --pdf --a3              # bigger paper for wide tabs
    python gsheet.py "<other htmlview url>" --csv
"""
import argparse
import asyncio
import csv
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from playwright.async_api import async_playwright

# Remember to paste the htmlview link here
DEFAULT_URL = "https://docs.google.com/spreadsheets/d/1PlgZ5Z6PpRd75Hqp5vbx4UvEXCK_Vhw8TdQLvAYXmjw/htmlview"


# ---------- helpers ----------

def js_unescape(s: str) -> str:
    s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\\'", "'")
    return json.loads(f'"{s}"')


def field(block: str, key: str):
    m = re.search(key + r'\s*:\s*"((?:[^"\\]|\\.)*)"', block)
    return js_unescape(m.group(1)) if m else None


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "sheet"


class WaffleTable(HTMLParser):
    """Pulls the cell grid out of Google's htmlview <table class="waffle">.
    Skips the A/B/C header row, row-number cells and the frozen-row divider,
    and expands merged cells (value in the top-left cell, blanks elsewhere)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # >0 while inside the first waffle table
        self.done = False
        self.in_thead = False
        self.row = None
        self.cell = None        # [text_parts, rowspan, colspan]
        self.skip_cell = False
        self.raw_rows = []      # list of rows, each a list of (text, rowspan, colspan)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif not self.done and "waffle" in (a.get("class") or ""):
                self.depth = 1
            return
        if not self.depth:
            return
        if tag == "thead":
            self.in_thead = True
        elif tag == "tr" and not self.in_thead:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            cls = a.get("class") or ""
            if tag == "th":          # row numbers
                self.skip_cell = True
                return
            # frozen-row/column divider cells are kept as markers and removed later
            self.cell = [[], int(a.get("rowspan", 1) or 1), int(a.get("colspan", 1) or 1),
                         "freezebar-cell" in cls]
        elif tag == "br" and self.cell is not None:
            self.cell[0].append("\n")

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "table":
            self.depth -= 1
            if not self.depth:
                self.done = True
        elif tag == "thead":
            self.in_thead = False
        elif tag == "th":
            self.skip_cell = False
        elif tag == "td" and self.cell is not None:
            text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.cell[0]))
            text = "\n".join(line.strip() for line in text.split("\n")).strip()
            self.row.append((text, self.cell[1], self.cell[2], self.cell[3]))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            # skip the horizontal frozen-row divider (a row made only of divider cells)
            if self.row and not all(cell[3] for cell in self.row):
                self.raw_rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None and not self.skip_cell:
            self.cell[0].append(data)

    def grid(self):
        out, pending = [], {}   # pending[(r, c)] = True for cells covered by a rowspan
        divider_cols = set()    # column positions of the frozen-column divider
        for r, raw in enumerate(self.raw_rows):
            row, c = [], 0
            for text, rs, cs, is_divider in raw:
                while pending.pop((r, c), False):
                    row.append(""); c += 1
                if is_divider:
                    divider_cols.add(c)
                row.append(text)
                row.extend([""] * (cs - 1))
                for dr in range(1, rs):
                    for dc in range(cs):
                        pending[(r + dr, c + dc)] = True
                c += cs
            while pending.pop((r, c), False):
                row.append(""); c += 1
            out.append(row)
        if divider_cols:
            out = [[v for i, v in enumerate(row) if i not in divider_cols] for row in out]
        # trim trailing empty rows and columns
        while out and not any(out[-1]):
            out.pop()
        width = max((max((i + 1 for i, v in enumerate(r) if v), default=0) for r in out), default=0)
        return [r[:width] + [""] * (width - len(r[:width])) for r in out]


def html_to_rows(html: str):
    p = WaffleTable()
    p.feed(html)
    return p.grid() if p.raw_rows else None


# ---------- per-tab export ----------

async def export_tab(ctx, sem, i, total, tab, args):
    async with sem:
        page = await ctx.new_page()
        result = {"tab": tab, "pdf": None, "rows": None}
        try:
            await page.goto(tab["url"], wait_until="networkidle", timeout=90_000)
            if not await page.locator("table").count():
                print(f"[{i}/{total}] {tab['name']}: no table found, skipped")
                return result
            stem = f"{i:03d}_{safe_name(tab['name'])}"

            if args.csv:
                page_html = await page.content()
                rows = html_to_rows(page_html)
                if not rows:
                    dbg = args.out / "csv" / f"DEBUG_{stem}.html"
                    dbg.write_text(page_html, encoding="utf-8")
                    print(f"   ! {tab['name']}: couldn't read a table for CSV, saved {dbg.name}")
                else:
                    path = args.out / "csv" / f"{stem}.csv"
                    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                        csv.writer(fh).writerows(rows)
                    result["rows"] = rows

            if args.pdf:
                await page.emulate_media(media="screen")
                await page.add_style_tag(content="body{margin:0!important}")
                width = await page.evaluate(
                    "Math.max(document.body.scrollWidth,"
                    " ...[...document.querySelectorAll('table')].map(t => t.scrollWidth))")
                printable = 1540 if args.a3 else 1060
                scale = max(0.1, min(1.0, printable / max(width, 1)))
                path = args.out / "pdf" / f"{stem}.pdf"
                await page.pdf(path=str(path), scale=scale, print_background=True,
                               format="A3" if args.a3 else "A4", landscape=True,
                               margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"})
                result["pdf"] = path

            print(f"[{i}/{total}] {tab['name']}")
        except Exception as e:
            print(f"[{i}/{total}] {tab['name']}: FAILED ({e})")
        finally:
            await page.close()
        return result


# ---------- main ----------

async def main():
    ap = argparse.ArgumentParser(description="Export Google Sheet htmlview tabs to PDF/CSV")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="htmlview URL")
    ap.add_argument("--pdf", action="store_true", help="export PDFs (default if neither --pdf nor --csv)")
    ap.add_argument("--csv", action="store_true", help="export one CSV per tab")
    ap.add_argument("--combined", action="store_true", help="with --csv: also write ALL_TABS.csv")
    ap.add_argument("--only", nargs="+", metavar="TAB", help="only these tab names, e.g. --only AAF ACH")
    ap.add_argument("--a3", action="store_true", help="A3 landscape PDFs instead of A4")
    ap.add_argument("--headers", action="store_true", help="keep A/B/C letters and row numbers in PDFs")
    ap.add_argument("--concurrency", type=int, default=4, help="tabs processed in parallel (default 4)")
    ap.add_argument("--out", type=Path, default=Path("sheet_export"), help="output folder")
    args = ap.parse_args()
    if not (args.pdf or args.csv):
        args.pdf = True
    for sub, on in (("pdf", args.pdf), ("csv", args.csv)):
        if on:
            (args.out / sub).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await ctx.new_page()
        await page.goto(args.url, wait_until="networkidle", timeout=90_000)
        html = await page.content()

        # htmlview lists its tabs in a script: items.push({name: "...", pageUrl: "...", gid: "..."})
        tabs = []
        for block in re.findall(r"items\.push\((\{.*?\})\)", html, re.S):
            name, url, gid = field(block, "name"), field(block, "pageUrl"), field(block, "gid")
            if not url:
                continue
            if not args.headers:
                url = url.replace("headers=true", "headers=false")
            tabs.append({"name": name or gid, "url": url, "gid": gid})

        if not tabs:
            Path("debug_htmlview.html").write_text(html, encoding="utf-8")
            print("No tabs found. Saved debug_htmlview.html, send it for a fix.")
            await browser.close()
            return
        await page.close()

        if args.only:
            wanted = {n.lower() for n in args.only}
            tabs = [t for t in tabs if t["name"].lower() in wanted]
            missing = wanted - {t["name"].lower() for t in tabs}
            if missing:
                print("Not found:", ", ".join(sorted(missing)))

        print(f"Exporting {len(tabs)} tab(s) as {' + '.join(k for k in ('pdf', 'csv') if getattr(args, k))}")
        sem = asyncio.Semaphore(max(1, args.concurrency))
        results = await asyncio.gather(*[
            export_tab(ctx, sem, i, len(tabs), t, args) for i, t in enumerate(tabs, 1)])
        await browser.close()

    if args.pdf:
        pdfs = [r["pdf"] for r in results if r["pdf"]]
        if pdfs:
            from pypdf import PdfWriter
            writer = PdfWriter()
            for f in pdfs:
                writer.append(str(f))
            with open(args.out / "ALL_TABS.pdf", "wb") as fh:
                writer.write(fh)
        print(f"PDF: {len(pdfs)}/{len(tabs)} tabs")

    if args.csv:
        done = [r for r in results if r["rows"] is not None]
        if args.combined and done:
            with open(args.out / "ALL_TABS.csv", "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                for r in done:
                    for row in r["rows"]:
                        w.writerow([r["tab"]["name"], *row])
        print(f"CSV: {len(done)}/{len(tabs)} tabs")

    print(f"Output folder: {args.out.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())