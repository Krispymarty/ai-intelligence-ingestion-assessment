import csv
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TABS = ("Startups", "Products", "Research Papers", "Jobs", "News", "Entity Mapping Log")
INTEGER_COLUMNS = {"content.data.employeeCount", "content.github_stars"}


def build_workbook(output: Path):
    book = Workbook(write_only=True)
    for title in TABS:
        sheet = book.create_sheet(title)
        sheet.freeze_panes = "A2"
        with (output / f"{title}.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream)
            headers = next(rows)
            for number, header in enumerate(headers, start=1):
                sheet.column_dimensions[get_column_letter(number)].width = 42 if (
                    header.endswith(("url", "excerpt", "title")) or header in {"evidence", "id"}
                ) else min(36, max(15, len(header) + 2))
            heading = []
            for title_cell in headers:
                cell = WriteOnlyCell(sheet, value=title_cell)
                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = PatternFill("solid", fgColor="0D6666")
                cell.alignment = Alignment(vertical="center")
                heading.append(cell)
            sheet.append(heading)
            row_count = 1
            for row in rows:
                cells = []
                for column, value in zip(headers, row, strict=True):
                    if not value:
                        cells.append(None)
                    elif column in INTEGER_COLUMNS and value.isdecimal():
                        cells.append(int(value))
                    elif column == "content.is_remote" and value in {"true", "false"}:
                        cells.append(value == "true")
                    else:
                        cell = WriteOnlyCell(sheet, value=value)
                        cell.data_type = "s"
                        cells.append(cell)
                sheet.append(cells)
                row_count += 1
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_count}"
    book.save(output / "AI Assessment.xlsx")
