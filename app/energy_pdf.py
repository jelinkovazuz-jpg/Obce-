"""Branded PDF export for municipal energy offers."""

from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


PINK = colors.HexColor("#EB0061")
NAVY = colors.HexColor("#111F55")
GREEN = colors.HexColor("#008C3F")
DARK = colors.HexColor("#202733")
MUTED = colors.HexColor("#6F7A89")
LIGHT = colors.HexColor("#F1F4F7")
LINE = colors.HexColor("#D9DFE6")


def _register_fonts():
    if "Offer" in pdfmetrics.getRegisteredFontNames():
        return
    import font_roboto
    font_dir = Path(font_roboto.__file__).parent / "files"
    pdfmetrics.registerFont(TTFont("Offer", str(font_dir / "Roboto-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Offer-Bold", str(font_dir / "Roboto-Bold.ttf")))


def _money(value):
    return f"{value:,.0f} Kč".replace(",", " ")


def _price(value):
    return f"{value:,.2f} Kč/MWh".replace(",", " ")


def _date(value):
    return f"{value.day}. {value.month}. {value.year}"


def _period_label(period, is_last=False):
    if is_last:
        return f"od {_date(period['valid_from'])}"
    return f"{_date(period['valid_from'])} - {_date(period['valid_to'])}"


def _display_periods(periods):
    """Collapse identical VT/NT rows so one price interval is shown once."""
    result = []
    seen = set()
    for period in periods:
        key = (
            period["valid_from"], period["valid_to"], period["unit_price"],
            period["monthly_fee"],
        )
        if key not in seen:
            seen.add(key)
            result.append(period)
    return result


def _fit(c, text, x, y, max_width, size=9, font="Offer", min_size=6):
    text = str(text or "")
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= .5
    if c.stringWidth(text, font, size) > max_width:
        while text and c.stringWidth(text + "…", font, size) > max_width:
            text = text[:-1]
        text += "…"
    c.setFont(font, size)
    c.drawString(x, y, text)


def _header(c, width, offer_date, label):
    c.setFillColor(PINK)
    c.rect(0, 0, width, 7, stroke=0, fill=1)
    c.setFont("Offer-Bold", 28)
    c.drawString(36, c._pagesize[1] - 48, "innogy")
    c.setFillColor(NAVY)
    c.setFont("Offer-Bold", 10)
    c.drawRightString(width - 36, c._pagesize[1] - 36, label.upper())
    c.setFillColor(DARK)
    c.setFont("Offer", 9)
    c.drawRightString(width - 36, c._pagesize[1] - 51, _date(offer_date))


def _footer(c, width):
    c.setFillColor(MUTED)
    c.setFont("Offer", 6.8)
    c.drawString(36, 18, "Ceny jsou uvedeny bez DPH. Výpočet vychází z uvedené roční spotřeby a předpokládá její zachování.")
    c.setFillColor(PINK)
    c.setFont("Offer-Bold", 8)
    c.drawRightString(width - 36, 18, "innogy")


def _summary_page(c, quote, result, details):
    width, height = A4
    c.setPageSize(A4)
    _header(c, width, quote[2], "Souhrnná nabídka energií")
    c.setFillColor(LIGHT)
    c.roundRect(30, height - 150, width - 60, 72, 12, stroke=0, fill=1)
    c.setFillColor(PINK)
    c.setFont("Offer-Bold", 10)
    c.drawString(44, height - 100, "●  OPTIMAL 36")
    c.setFillColor(DARK)
    c.setFont("Offer-Bold", 22)
    c.drawString(44, height - 128, f"Souhrnná nabídka pro {quote[0]}")
    c.setFillColor(NAVY)
    c.setFont("Offer", 10)
    c.drawString(44, height - 144, "Přehled všech odběrných míst a předpokládané úspory")

    columns = [
        ("Odběrné místo", 150), ("EAN / EIC", 112), ("Komodita", 52),
        ("MWh/rok", 62), ("Úspora 12 m.", 70), ("Úspora 36 m.", 73),
    ]
    x0, table_top, row_h = 35, height - 180, 19
    c.setFillColor(NAVY)
    c.roundRect(x0, table_top - 24, sum(w for _, w in columns), 24, 6, stroke=0, fill=1)
    x = x0
    c.setFillColor(colors.white)
    for title, col_w in columns:
        c.setFont("Offer-Bold", 7.5)
        c.drawString(x + 5, table_top - 16, title)
        x += col_w
    y = table_top - 24
    max_rows = min(len(result["points"]), 20)
    for index, item in enumerate(result["points"][:max_rows], start=1):
        full, year = item["full"], item["first_year"]
        c.setFillColor(colors.white if index % 2 else colors.HexColor("#F8FAFB"))
        c.rect(x0, y - row_h, sum(w for _, w in columns), row_h, stroke=0, fill=1)
        values = [
            f"{index}. {full['address']}", full["ean_eic"], full["commodity"],
            f"{full['annual_consumption']:.3f}", _money(year["saving"]), _money(full["saving"]),
        ]
        x = x0
        for col_index, ((_, col_w), value) in enumerate(zip(columns, values)):
            c.setFillColor(GREEN if col_index >= 4 and float(year["saving"] if col_index == 4 else full["saving"]) >= 0 else (PINK if col_index >= 4 else DARK))
            _fit(c, value, x + 5, y - 13, col_w - 10, 7.5, "Offer-Bold" if col_index >= 4 else "Offer")
            x += col_w
        c.setStrokeColor(LINE)
        c.line(x0, y - row_h, x0 + sum(w for _, w in columns), y - row_h)
        y -= row_h
    if len(result["points"]) > max_rows:
        c.setFillColor(MUTED)
        c.setFont("Offer", 7)
        c.drawString(x0, y - 10, f"Dalších {len(result['points']) - max_rows} odběrných míst je uvedeno na samostatných stranách.")

    box_y = 58
    half = (width - 75) / 2
    c.setFillColor(colors.HexColor("#EAF6EE"))
    c.roundRect(35, box_y, half, 67, 10, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.setFont("Offer-Bold", 9)
    c.drawString(50, box_y + 45, "CELKOVÁ ÚSPORA ZA PRVNÍCH 12 MĚSÍCŮ")
    c.setFont("Offer-Bold", 22)
    c.drawString(50, box_y + 17, _money(result["saving_12"]))
    c.setFillColor(GREEN)
    c.roundRect(45 + half, box_y, half, 67, 10, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Offer-Bold", 9)
    c.drawString(60 + half, box_y + 45, "PŘEDPOKLÁDANÁ ÚSPORA ZA CELÝCH 36 MĚSÍCŮ")
    c.setFont("Offer-Bold", 22)
    c.drawString(60 + half, box_y + 17, _money(result["saving_36"]))
    _footer(c, width)
    c.showPage()


def _detail_page(c, quote, item, meta):
    width, height = A4
    c.setPageSize(A4)
    full, year = item["full"], item["first_year"]
    _header(c, width, quote[2], f"Nabídka - {full['commodity']}")
    c.setFillColor(LIGHT)
    c.roundRect(34, height - 175, width - 68, 90, 12, stroke=0, fill=1)
    c.setFillColor(PINK)
    c.setFont("Offer-Bold", 10)
    c.drawString(50, height - 112, "●  OPTIMAL 36")
    c.setFillColor(DARK)
    c.setFont("Offer-Bold", 20)
    c.drawString(50, height - 142, f"Výhodnější {full['commodity'].lower()} pro {quote[0]}")
    c.setFillColor(MUTED)
    _fit(c, full["address"], 50, height - 162, width - 100, 10)

    labels = ["EAN / EIC", "SAZBA / PÁSMO", "ROČNÍ SPOTŘEBA", "ZAHÁJENÍ DODÁVKY"]
    values = [full["ean_eic"], meta["rate_band"], f"{full['annual_consumption']:.3f} MWh", _date(full["start"])]
    for index, (label, value) in enumerate(zip(labels, values)):
        x = 40 + index * 137
        c.setFillColor(MUTED); c.setFont("Offer", 7); c.drawString(x, height - 210, label)
        c.setFillColor(DARK); _fit(c, value, x, height - 226, 125, 9, "Offer-Bold")

    c.setFillColor(DARK); c.setFont("Offer-Bold", 14)
    c.drawString(40, height - 265, "Přehled cen innogy v čase")
    periods = _display_periods(full["periods"])[:6]
    y = height - 292
    c.setStrokeColor(PINK); c.setLineWidth(3); c.line(50, y, width - 50, y)
    for index, period in enumerate(periods):
        x = 50 + index * (width - 100) / max(len(periods), 1)
        c.setFillColor(colors.white); c.circle(x, y, 6, stroke=1, fill=1)
        c.setFillColor(MUTED); c.setFont("Offer", 6.5)
        timeline_label = (
            f"od {_date(period['valid_from'])}"
            if index == len(periods) - 1 else _date(period["valid_from"])
        )
        c.drawString(x, y + 14, timeline_label)
        c.setFillColor(DARK); c.setFont("Offer-Bold", 8)
        c.drawString(x, y - 20, _price(period["unit_price"]).replace("/MWh", ""))

    table_y = height - 355
    c.setFillColor(DARK); c.setFont("Offer-Bold", 14); c.drawString(40, table_y, "Srovnání obchodních cen")
    table_y -= 28
    c.setFillColor(LIGHT); c.roundRect(35, table_y - 25, width - 70, 25, 6, stroke=0, fill=1)
    c.setFillColor(MUTED); c.setFont("Offer-Bold", 7)
    c.drawString(48, table_y - 16, "OBDOBÍ")
    c.drawString(255, table_y - 16, "SOUČASNÝ DODAVATEL")
    c.setFillColor(PINK); c.drawRightString(width - 48, table_y - 16, "INNOGY OPTIMAL 36")
    y = table_y - 35
    visible_periods = periods[:5]
    for index, period in enumerate(visible_periods):
        c.setFillColor(DARK); c.setFont("Offer", 7.5)
        c.drawString(48, y, _period_label(period, index == len(periods) - 1))
        c.drawString(255, y, _price(full["current_price_vt"]))
        c.setFillColor(PINK); c.setFont("Offer-Bold", 8)
        c.drawRightString(width - 48, y, _price(period["unit_price"]))
        c.setStrokeColor(LINE); c.line(45, y - 9, width - 45, y - 9)
        y -= 27

    supplier_y = max(y - 10, 245)
    c.setFillColor(MUTED); c.setFont("Offer", 7); c.drawString(45, supplier_y, "SOUČASNÝ DODAVATEL")
    c.setFillColor(DARK); c.setFont("Offer-Bold", 9); c.drawString(45, supplier_y - 15, full["current_supplier"])

    c.setFillColor(LIGHT); c.roundRect(35, 132, width - 70, 67, 10, stroke=0, fill=1)
    c.setFillColor(MUTED); c.setFont("Offer-Bold", 8); c.drawString(50, 177, "PLÁNOVANÁ ÚSPORA ZA PRVNÍCH 12 MĚSÍCŮ")
    c.setFillColor(DARK); c.setFont("Offer-Bold", 22); c.drawString(50, 145, _money(year["saving"]))
    c.setFillColor(GREEN if full["saving"] >= 0 else PINK)
    c.roundRect(35, 50, width - 70, 67, 10, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Offer-Bold", 8); c.drawString(50, 95, "PŘEDPOKLÁDANÁ ÚSPORA ZA CELÝCH 36 MĚSÍCŮ")
    c.setFont("Offer-Bold", 22); c.drawString(50, 64, _money(full["saving"]))
    c.setFont("Offer-Bold", 12); c.drawRightString(width - 50, 70, "Optimal 36")
    _footer(c, width)
    c.showPage()


def build_energy_offer_pdf(conn, quote_id, result):
    _register_fonts()
    quote = conn.execute("""
        SELECT customer_name,coalesce(title,''),signing_date
        FROM energy_quotes WHERE id=?
    """, [quote_id]).fetchone()
    if not quote:
        raise ValueError("Nabídka nebyla nalezena.")
    point_rows = conn.execute("""
        SELECT id,rate_band FROM energy_supply_points
        WHERE quote_id=? ORDER BY created_at
    """, [quote_id]).fetchall()
    details = {point_id: {"rate_band": rate} for point_id, rate in point_rows}
    output = BytesIO()
    c = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    _summary_page(c, quote, result, details)
    for item in result["points"]:
        _detail_page(c, quote, item, details[item["full"]["point_id"]])
    c.save()
    return output.getvalue()
