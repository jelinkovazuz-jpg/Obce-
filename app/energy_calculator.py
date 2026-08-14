"""Databáze a výpočtové jádro cenových nabídek energií.

Výpočty jsou záměrně oddělené od Streamlit UI, aby šly testovat a aby bylo
možné později přidat další produkty nebo dodavatele bez změny kalkulačky.
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4


COMMODITIES = ["Elektřina", "Plyn"]
CONTRACT_TYPES = ["Doba neurčitá", "Doba určitá"]


class EnergyCalculationError(RuntimeError):
    pass


def init_energy_calculator(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_products (
            id VARCHAR PRIMARY KEY, supplier VARCHAR NOT NULL,
            name VARCHAR NOT NULL, commodity VARCHAR NOT NULL,
            fixation_months INTEGER NOT NULL DEFAULT 36,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE (supplier, name, commodity)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_price_lists (
            id VARCHAR PRIMARY KEY, product_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL, signing_valid_from DATE NOT NULL,
            signing_valid_to DATE, active BOOLEAN NOT NULL DEFAULT TRUE,
            note VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_price_periods (
            id VARCHAR PRIMARY KEY, price_list_id VARCHAR NOT NULL,
            rate_band VARCHAR NOT NULL DEFAULT 'Všechny',
            component VARCHAR NOT NULL DEFAULT 'Jednotná',
            valid_from DATE NOT NULL, valid_to DATE,
            unit_price DOUBLE NOT NULL, monthly_fee DOUBLE NOT NULL DEFAULT 0,
            UNIQUE (price_list_id, rate_band, component, valid_from)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_quotes (
            id VARCHAR PRIMARY KEY, kod_obce INTEGER, customer_name VARCHAR NOT NULL,
            product_id VARCHAR NOT NULL, price_list_id VARCHAR NOT NULL,
            signing_date DATE NOT NULL, title VARCHAR, created_by VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_supply_points (
            id VARCHAR PRIMARY KEY, quote_id VARCHAR NOT NULL,
            address VARCHAR NOT NULL, ean_eic VARCHAR NOT NULL,
            commodity VARCHAR NOT NULL, rate_band VARCHAR NOT NULL,
            annual_consumption DOUBLE NOT NULL,
            vt_share DOUBLE NOT NULL DEFAULT 100,
            current_supplier VARCHAR NOT NULL,
            current_price_vt DOUBLE NOT NULL,
            current_price_nt DOUBLE,
            current_monthly_fee DOUBLE NOT NULL DEFAULT 0,
            contract_type VARCHAR NOT NULL,
            contract_end_date DATE, notice_months INTEGER,
            notice_submitted_date DATE, supply_start_date DATE NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (quote_id, ean_eic)
        )
    """)
    conn.execute("ALTER TABLE energy_supply_points ADD COLUMN IF NOT EXISTS product_id VARCHAR")
    conn.execute("ALTER TABLE energy_supply_points ADD COLUMN IF NOT EXISTS price_list_id VARCHAR")
    _seed_optimal_36(conn)


def _seed_optimal_36(conn):
    """Idempotent sample based on the values supplied by the user."""
    products = [
        ("innogy-optimal36-ele", "innogy", "Optimal 36", "Elektřina"),
        ("innogy-optimal36-gas", "innogy", "Optimal 36", "Plyn"),
    ]
    for row in products:
        conn.execute("""
            INSERT INTO energy_products (id,supplier,name,commodity,fixation_months)
            VALUES (?,?,?,?,36) ON CONFLICT DO NOTHING
        """, row)
    lists = [
        ("optimal36-ele-example", products[0][0], "Optimal 36 – vzor 2026", date(2026, 1, 1), date(2026, 12, 31)),
        ("optimal36-gas-example", products[1][0], "Optimal 36 – vzor 2026", date(2026, 1, 1), date(2026, 12, 31)),
    ]
    for row in lists:
        conn.execute("""
            INSERT INTO energy_price_lists
                (id,product_id,name,signing_valid_from,signing_valid_to,note)
            VALUES (?,?,?,?,?,'Vzorové ceny zadané při vytvoření kalkulačky; před použitím ověřte.')
            ON CONFLICT DO NOTHING
        """, row)
    electricity = [
        (date(2026, 1, 1), date(2026, 12, 31), 2355.00),
        (date(2027, 1, 1), date(2027, 6, 30), 2280.00),
        (date(2027, 7, 1), date(2027, 12, 31), 2523.20),
        (date(2028, 1, 1), None, 2440.20),
    ]
    for component in ("VT", "NT", "Jednotná"):
        for start, end, price in electricity:
            _seed_period(conn, "optimal36-ele-example", "Všechny", component, start, end, price, 127)
    gas = [
        (date(2026, 1, 1), date(2027, 6, 30), 825.50),
        (date(2027, 7, 1), date(2027, 12, 31), 952.50),
        (date(2028, 1, 1), date(2028, 12, 31), 832.50),
        (date(2029, 1, 1), None, 780.00),
    ]
    for start, end, price in gas:
        _seed_period(conn, "optimal36-gas-example", "nad 7,56 do 63 MWh", "Jednotná", start, end, price, 130)


def _seed_period(conn, price_list_id, rate, component, start, end, price, fee):
    key = f"{price_list_id}|{rate}|{component}|{start.isoformat()}"
    conn.execute("""
        INSERT INTO energy_price_periods
            (id,price_list_id,rate_band,component,valid_from,valid_to,unit_price,monthly_fee)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
    """, [key, price_list_id, rate, component, start, end, price, fee])


def first_of_next_month(value):
    return add_months(value.replace(day=1), 1)


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def derive_supply_start(contract_type, signing_date, contract_end_date=None,
                        notice_months=None, notice_submitted_date=None):
    """Derive start on the first day after the contract/notice period.

    For an indefinite contract, full calendar months are counted. A notice
    submitted in August with three months therefore starts supply on 1 Dec.
    """
    if contract_type == "Doba určitá":
        if not contract_end_date:
            raise EnergyCalculationError("U smlouvy na dobu určitou vyplňte datum konce.")
        return first_of_next_month(contract_end_date)
    submitted = notice_submitted_date or signing_date
    if not submitted:
        raise EnergyCalculationError("Vyplňte datum podání výpovědi nebo podpisu nabídky.")
    months = int(notice_months or 0)
    if months < 0:
        raise EnergyCalculationError("Výpovědní doba nesmí být záporná.")
    return add_months(submitted.replace(day=1), months + 1)


def _as_decimal(value):
    return Decimal(str(value or 0))


def _periods(conn, price_list_id, rate_band, component, start, end):
    rows = conn.execute("""
        SELECT valid_from,valid_to,unit_price,monthly_fee,rate_band,component
        FROM energy_price_periods
        WHERE price_list_id=?
          AND rate_band IN (?, 'Všechny')
          AND component IN (?, 'Jednotná')
          AND valid_from < ?
          AND coalesce(valid_to, DATE '9999-12-31') >= ?
        ORDER BY CASE WHEN rate_band=? THEN 0 ELSE 1 END,
                 CASE WHEN component=? THEN 0 ELSE 1 END, valid_from
    """, [price_list_id, rate_band, component, end, start, rate_band, component]).fetchall()
    # Exact rows win over generic rows for the same date range.
    result = []
    for row in rows:
        if not any(r[0] == row[0] and r[1] == row[1] for r in result):
            result.append(row)
    return sorted(result, key=lambda row: row[0])


def calculate_supply_point(conn, point_id, months=36):
    row = conn.execute("""
        SELECT sp.id,sp.address,sp.ean_eic,sp.commodity,sp.rate_band,
               sp.annual_consumption,sp.vt_share,sp.current_supplier,
               sp.current_price_vt,sp.current_price_nt,sp.current_monthly_fee,
               sp.supply_start_date,coalesce(sp.price_list_id,q.price_list_id),
               p.fixation_months
        FROM energy_supply_points sp
        JOIN energy_quotes q ON q.id=sp.quote_id
        JOIN energy_products p ON p.id=coalesce(sp.product_id,q.product_id)
        WHERE sp.id=?
    """, [point_id]).fetchone()
    if not row:
        raise EnergyCalculationError("Odběrné místo nebylo nalezeno.")
    (_, address, ean, commodity, rate, annual, vt_share, current_supplier,
     current_vt, current_nt, current_fee, start, price_list_id, fixation) = row
    months = min(int(months), int(fixation))
    if annual < 0 or not 0 <= vt_share <= 100:
        raise EnergyCalculationError("Spotřeba ani podíl VT/NT nejsou platné.")
    components = [("Jednotná", Decimal("1"), _as_decimal(current_vt))]
    if commodity == "Elektřina" and current_nt is not None and vt_share < 100:
        share = _as_decimal(vt_share) / Decimal("100")
        components = [("VT", share, _as_decimal(current_vt)),
                      ("NT", Decimal("1") - share, _as_decimal(current_nt))]

    lines = []
    innogy_energy = current_energy = Decimal("0")
    innogy_fixed = current_fixed = Decimal("0")
    period_totals = {}
    annual_dec = _as_decimal(annual)
    for month_number in range(months):
        month_start = add_months(start, month_number)
        month_end = add_months(start, month_number + 1)
        month_days = Decimal((month_end - month_start).days)
        current_fixed += _as_decimal(current_fee)
        fee_for_month = None
        for component, share, current_price in components:
            monthly_consumption = annual_dec * share / Decimal("12")
            current_energy += monthly_consumption * current_price
            periods = _periods(conn, price_list_id, rate, component, month_start, month_end)
            covered_days = 0
            for valid_from, valid_to, unit_price, monthly_fee, used_rate, used_component in periods:
                seg_start = max(month_start, valid_from)
                seg_end = min(month_end, (valid_to + timedelta(days=1)) if valid_to else month_end)
                if seg_start >= seg_end:
                    continue
                days = (seg_end - seg_start).days
                covered_days += days
                consumption = monthly_consumption * Decimal(days) / month_days
                cost = consumption * _as_decimal(unit_price)
                innogy_energy += cost
                fee_for_month = _as_decimal(monthly_fee) if fee_for_month is None else fee_for_month
                period_key = (valid_from, valid_to, component, float(unit_price), float(monthly_fee))
                period_totals[period_key] = period_totals.get(period_key, Decimal("0")) + cost
                lines.append({
                    "month": month_number + 1, "from": seg_start, "to": seg_end - timedelta(days=1),
                    "component": component, "consumption_mwh": float(consumption),
                    "unit_price": float(unit_price), "energy_cost": float(cost),
                })
            if covered_days != int(month_days):
                raise EnergyCalculationError(
                    f"Ceník nepokrývá celé období {month_start:%d.%m.%Y}–{month_end - timedelta(days=1):%d.%m.%Y} "
                    f"pro {rate}, {component}."
                )
        innogy_fixed += fee_for_month or Decimal("0")

    def money(value):
        return round(float(value), 2)

    period_summary = [
        {"valid_from": key[0], "valid_to": key[1], "component": key[2],
         "unit_price": key[3], "monthly_fee": key[4], "energy_cost": money(cost)}
        for key, cost in sorted(period_totals.items(), key=lambda item: item[0][0])
    ]
    return {
        "point_id": point_id, "address": address, "ean_eic": ean,
        "commodity": commodity, "annual_consumption": annual,
        "current_supplier": current_supplier,
        "current_price_vt": current_vt, "current_price_nt": current_nt,
        "current_monthly_fee": current_fee,
        "months": months, "start": start, "end": add_months(start, months),
        "current_energy": money(current_energy), "current_fixed": money(current_fixed),
        "current_total": money(current_energy + current_fixed),
        "innogy_energy": money(innogy_energy), "innogy_fixed": money(innogy_fixed),
        "innogy_total": money(innogy_energy + innogy_fixed),
        "energy_saving": money(current_energy - innogy_energy),
        "fixed_saving": money(current_fixed - innogy_fixed),
        "saving": money(current_energy + current_fixed - innogy_energy - innogy_fixed),
        "periods": period_summary, "lines": lines,
    }


def calculate_quote(conn, quote_id):
    points = conn.execute(
        "SELECT id FROM energy_supply_points WHERE quote_id=? ORDER BY created_at", [quote_id]
    ).fetchall()
    results = []
    for (point_id,) in points:
        first_year = calculate_supply_point(conn, point_id, 12)
        full = calculate_supply_point(conn, point_id, 36)
        results.append({"first_year": first_year, "full": full})
    return {
        "points": results,
        "saving_12": round(sum(item["first_year"]["saving"] for item in results), 2),
        "saving_36": round(sum(item["full"]["saving"] for item in results), 2),
    }


def create_quote(conn, kod_obce, customer_name, product_id, price_list_id,
                 signing_date, title, username):
    quote_id = str(uuid4())
    conn.execute("""
        INSERT INTO energy_quotes
            (id,kod_obce,customer_name,product_id,price_list_id,signing_date,title,created_by)
        VALUES (?,?,?,?,?,?,?,?)
    """, [quote_id, kod_obce, customer_name.strip(), product_id, price_list_id,
           signing_date, title.strip() or None, username])
    return quote_id


def add_supply_point(conn, quote_id, address, ean_eic, commodity, rate_band,
                     annual_consumption, vt_share, current_supplier,
                     current_price_vt, current_price_nt, current_monthly_fee,
                     contract_type, contract_end_date, notice_months,
                     notice_submitted_date, supply_start_date, product_id=None,
                     price_list_id=None):
    point_id = str(uuid4())
    conn.execute("""
        INSERT INTO energy_supply_points (
            id,quote_id,address,ean_eic,commodity,rate_band,annual_consumption,
            vt_share,current_supplier,current_price_vt,current_price_nt,
            current_monthly_fee,contract_type,contract_end_date,notice_months,
            notice_submitted_date,supply_start_date,product_id,price_list_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [point_id, quote_id, address.strip(), ean_eic.strip(), commodity, rate_band,
           annual_consumption, vt_share, current_supplier.strip(), current_price_vt,
           current_price_nt, current_monthly_fee, contract_type, contract_end_date,
           notice_months, notice_submitted_date, supply_start_date, product_id,
           price_list_id])
    return point_id


def save_price_period(conn, price_list_id, rate_band, component, valid_from,
                      valid_to, unit_price, monthly_fee, period_id=None):
    period_id = period_id or str(uuid4())
    if valid_to and valid_to < valid_from:
        raise EnergyCalculationError("Konec cenového období nesmí být před začátkem.")
    conn.execute("""
        INSERT INTO energy_price_periods
            (id,price_list_id,rate_band,component,valid_from,valid_to,unit_price,monthly_fee)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET rate_band=excluded.rate_band,
            component=excluded.component,valid_from=excluded.valid_from,
            valid_to=excluded.valid_to,unit_price=excluded.unit_price,
            monthly_fee=excluded.monthly_fee
    """, [period_id, price_list_id, rate_band.strip(), component, valid_from,
           valid_to, unit_price, monthly_fee])
    return period_id
