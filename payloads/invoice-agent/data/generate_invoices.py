"""
Generate 5 synthetic Japanese 請求書 (invoice) PDFs using reportlab.
Fonts are embedded so pdfplumber can extract text without OCR.
Two invoices (004, 005) contain deliberate arithmetic errors for validation testing.

Usage: python3 generate_invoices.py
Output: INV-2024-00{1..5}.pdf and ground_truth/INV-2024-00{1..5}.json
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)

# ── Font registration ─────────────────────────────────────────────────────────
FONT_PATH = "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf"
pdfmetrics.registerFont(TTFont("IPAGothic", FONT_PATH))

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class LineItem:
    description: str
    quantity: int
    unit_price: int
    amount: int  # may be intentionally wrong in test cases


@dataclass
class Invoice:
    invoice_number: str
    issue_date: str
    due_date: str
    issuer_name: str
    issuer_address: str
    issuer_registration_number: str
    recipient_name: str
    recipient_address: str
    line_items: List[LineItem]
    subtotal: int
    consumption_tax: int   # may be intentionally wrong
    total: int             # may be intentionally wrong
    # ground-truth metadata (not rendered in PDF)
    is_valid: bool = True
    validation_issues: List[str] = field(default_factory=list)


# ── 5 synthetic invoices ──────────────────────────────────────────────────────
INVOICES = [
    Invoice(
        invoice_number="INV-2024-001",
        issue_date="2024-10-01",
        due_date="2024-10-31",
        issuer_name="株式会社テックソリューションズ",
        issuer_address="東京都渋谷区恵比寿一丁目1番1号",
        issuer_registration_number="T1234567890123",
        recipient_name="合同会社サンプル商事",
        recipient_address="東京都新宿区西新宿二丁目2番2号",
        line_items=[
            LineItem("コンサルティングサービス", 2, 150000, 300000),
            LineItem("資料作成費", 1, 50000, 50000),
        ],
        subtotal=350000,
        consumption_tax=35000,
        total=385000,
        is_valid=True,
    ),
    Invoice(
        invoice_number="INV-2024-002",
        issue_date="2024-10-05",
        due_date="2024-11-04",
        issuer_name="株式会社テックソリューションズ",
        issuer_address="東京都渋谷区恵比寿一丁目1番1号",
        issuer_registration_number="T1234567890123",
        recipient_name="株式会社フロンティア",
        recipient_address="大阪府大阪市北区梅田三丁目3番3号",
        line_items=[
            LineItem("ソフトウェア開発費", 3, 200000, 600000),
            LineItem("サーバー構築費", 1, 120000, 120000),
            LineItem("保守サポート", 1, 30000, 30000),
        ],
        subtotal=750000,
        consumption_tax=75000,
        total=825000,
        is_valid=True,
    ),
    Invoice(
        invoice_number="INV-2024-003",
        issue_date="2024-10-10",
        due_date="2024-11-09",
        issuer_name="株式会社テックソリューションズ",
        issuer_address="東京都渋谷区恵比寿一丁目1番1号",
        issuer_registration_number="T1234567890123",
        recipient_name="有限会社デザインワークス",
        recipient_address="愛知県名古屋市中区栄四丁目4番4号",
        line_items=[
            LineItem("デザイン制作費", 1, 80000, 80000),
            LineItem("印刷費", 500, 300, 150000),
        ],
        subtotal=230000,
        consumption_tax=23000,
        total=253000,
        is_valid=True,
    ),
    Invoice(
        # INVALID: 消費税 stated as 30,000 but should be 25,000 (10% of 250,000)
        invoice_number="INV-2024-004",
        issue_date="2024-10-15",
        due_date="2024-11-14",
        issuer_name="株式会社テックソリューションズ",
        issuer_address="東京都渋谷区恵比寿一丁目1番1号",
        issuer_registration_number="T1234567890123",
        recipient_name="株式会社データアナリティクス",
        recipient_address="福岡県福岡市博多区博多駅前五丁目5番5号",
        line_items=[
            LineItem("データ分析サービス", 5, 40000, 200000),
            LineItem("レポート作成費", 2, 25000, 50000),
        ],
        subtotal=250000,
        consumption_tax=30000,  # WRONG: should be 25000
        total=280000,
        is_valid=False,
        validation_issues=["消費税の金額が不正: 記載値30000円、正しい値25000円 (小計250000円の10%)"],
    ),
    Invoice(
        # INVALID: 合計 stated as 650,000 but should be 660,000 (600,000 + 60,000)
        invoice_number="INV-2024-005",
        issue_date="2024-10-20",
        due_date="2024-11-19",
        issuer_name="株式会社テックソリューションズ",
        issuer_address="東京都渋谷区恵比寿一丁目1番1号",
        issuer_registration_number="T1234567890123",
        recipient_name="合同会社AIラボ",
        recipient_address="東京都港区六本木六丁目6番6号",
        line_items=[
            LineItem("AIシステム導入支援", 1, 500000, 500000),
            LineItem("トレーニングサービス", 2, 50000, 100000),
        ],
        subtotal=600000,
        consumption_tax=60000,  # correct
        total=650000,           # WRONG: should be 660000
        is_valid=False,
        validation_issues=["合計金額が不正: 記載値650000円、正しい値660000円 (小計600000円 + 消費税60000円)"],
    ),
]


# ── PDF builder ───────────────────────────────────────────────────────────────
def fmt_yen(amount: int) -> str:
    return f"¥{amount:,}"


def build_pdf(inv: Invoice, output_path: str) -> None:
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    def style(size=10, bold=False, align="LEFT", color=colors.black):
        return ParagraphStyle(
            name=f"s{size}",
            fontName="IPAGothic",
            fontSize=size,
            leading=size * 1.5,
            textColor=color,
            alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}[align],
        )

    title_style = style(18, align="CENTER")
    h2_style = style(11)
    normal_style = style(10)
    small_style = style(9)
    right_style = style(10, align="RIGHT")

    elems = []

    # ── Title ────────────────────────────────────────────────────────────────
    elems.append(Paragraph("請　求　書", title_style))
    elems.append(Spacer(1, 6 * mm))

    # ── Header: invoice number / dates ────────────────────────────────────────
    header_data = [
        ["請求書番号", inv.invoice_number],
        ["発行日", inv.issue_date],
        ["支払期限", inv.due_date],
    ]
    header_table = Table(header_data, colWidths=[40 * mm, 60 * mm])
    header_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "IPAGothic"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("ALIGN", (1, 0), (1, -1), "LEFT"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    # ── Issuer / Recipient side by side ───────────────────────────────────────
    issuer_lines = [
        Paragraph("【請求元】", h2_style),
        Paragraph(inv.issuer_name, normal_style),
        Paragraph(inv.issuer_address, small_style),
        Paragraph(f"登録番号: {inv.issuer_registration_number}", small_style),
    ]
    recipient_lines = [
        Paragraph("【請求先】", h2_style),
        Paragraph(inv.recipient_name, normal_style),
        Paragraph(inv.recipient_address, small_style),
    ]

    party_data = [[issuer_lines, recipient_lines]]
    party_table = Table(party_data, colWidths=[85 * mm, 85 * mm])
    party_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    top_block = Table(
        [[header_table, party_table]],
        colWidths=[115 * mm, 55 * mm],
    )
    top_block.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elems.append(top_block)
    elems.append(Spacer(1, 8 * mm))

    # ── Line items table ──────────────────────────────────────────────────────
    item_header = ["品名", "数量", "単価", "金額"]
    rows = [item_header]
    for li in inv.line_items:
        rows.append([
            li.description,
            str(li.quantity),
            fmt_yen(li.unit_price),
            fmt_yen(li.amount),
        ])

    col_widths = [90 * mm, 20 * mm, 30 * mm, 30 * mm]
    item_table = Table(rows, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "IPAGothic"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2FF")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elems.append(item_table)
    elems.append(Spacer(1, 6 * mm))

    # ── Totals ────────────────────────────────────────────────────────────────
    totals_data = [
        ["小計", fmt_yen(inv.subtotal)],
        ["消費税 (10%)", fmt_yen(inv.consumption_tax)],
        ["合計金額", fmt_yen(inv.total)],
    ]
    totals_table = Table(totals_data, colWidths=[40 * mm, 40 * mm])
    totals_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "IPAGothic"),
        ("FONTSIZE", (0, 0), (-1, 1), 10),
        ("FONTSIZE", (0, 2), (-1, 2), 12),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#4472C4")),
        ("TEXTCOLOR", (0, 2), (-1, 2), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    # Right-align the totals block
    right_wrapper = Table([[None, totals_table]], colWidths=[90 * mm, 80 * mm])
    right_wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elems.append(right_wrapper)

    doc.build(elems)
    print(f"  Written: {output_path}")


# ── Ground truth JSON writer ──────────────────────────────────────────────────
def write_ground_truth(inv: Invoice, gt_dir: str) -> None:
    gt = {
        "invoice_number": inv.invoice_number,
        "issue_date": inv.issue_date,
        "due_date": inv.due_date,
        "issuer_name": inv.issuer_name,
        "issuer_address": inv.issuer_address,
        "issuer_registration_number": inv.issuer_registration_number,
        "recipient_name": inv.recipient_name,
        "recipient_address": inv.recipient_address,
        "line_items": [asdict(li) for li in inv.line_items],
        "subtotal": inv.subtotal,
        "consumption_tax": inv.consumption_tax,
        "total": inv.total,
        "_meta": {
            "is_valid": inv.is_valid,
            "validation_issues": inv.validation_issues,
            "correct_consumption_tax": int(inv.subtotal * 0.1),
            "correct_total": inv.subtotal + int(inv.subtotal * 0.1),
        },
    }
    path = os.path.join(gt_dir, f"{inv.invoice_number}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=2)
    print(f"  Written: {path}")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    gt_dir = os.path.join(script_dir, "ground_truth")
    os.makedirs(gt_dir, exist_ok=True)

    for inv in INVOICES:
        print(f"\n{inv.invoice_number} ({'VALID' if inv.is_valid else 'INVALID'}):")
        pdf_path = os.path.join(script_dir, f"{inv.invoice_number}.pdf")
        build_pdf(inv, pdf_path)
        write_ground_truth(inv, gt_dir)

    print("\nDone. Verifying text extraction with pdfplumber...")
    import pdfplumber
    for inv in INVOICES:
        pdf_path = os.path.join(script_dir, f"{inv.invoice_number}.pdf")
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        has_number = inv.invoice_number in text
        has_total = str(inv.total) in text.replace(",", "").replace("¥", "")
        print(f"  {inv.invoice_number}: invoice_number_found={has_number}  total_found={has_total}")
