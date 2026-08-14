from datetime import date
from uuid import uuid4

import pandas as pd
import streamlit as st

from app.energy_calculator import (
    COMMODITIES,
    CONTRACT_TYPES,
    EnergyCalculationError,
    add_supply_point,
    calculate_quote,
    create_quote,
    derive_supply_start,
    save_price_period,
)


def _money(value):
    return f"{value:,.2f} Kč".replace(",", " ")


def _product_options(conn, commodity=None):
    sql = """
        SELECT p.id,p.supplier || ' ' || p.name || ' · ' || p.commodity,
               p.commodity,p.fixation_months
        FROM energy_products p WHERE p.active
    """
    params = []
    if commodity:
        sql += " AND p.commodity=?"
        params.append(commodity)
    return conn.execute(sql + " ORDER BY p.supplier,p.name,p.commodity", params).fetchall()


def _price_list_options(conn, product_id, signing_date=None):
    rows = conn.execute("""
        SELECT id,name,signing_valid_from,signing_valid_to
        FROM energy_price_lists
        WHERE product_id=? AND active
        ORDER BY signing_valid_from DESC
    """, [product_id]).fetchall()
    if signing_date:
        matching = [r for r in rows if r[2] <= signing_date and (r[3] is None or r[3] >= signing_date)]
        return matching or rows
    return rows


def render_energy_calculator(conn, username, role):
    st.subheader("Kalkulačka cenových nabídek energií")
    st.caption("Obchodní část ceny bez DPH. Regulovaná část se do porovnání nezahrnuje.")
    new_tab, calculation_tab, admin_tab = st.tabs(
        ["Nová nabídka", "Odběrná místa a výpočet", "Administrace ceníků"]
    )

    with new_tab:
        municipalities = conn.execute(
            "SELECT kod_obce,nazev,coalesce(okres,'') FROM obce ORDER BY nazev,okres"
        ).fetchall()
        municipality_labels = {
            f"{name} — {district or 'bez okresu'} [{code}]": (code, name)
            for code, name, district in municipalities
        }
        products = _product_options(conn)
        product_labels = {label: product_id for product_id, label, _, _ in products}
        with st.form("energy_new_quote"):
            municipality_label = st.selectbox("Zákazník / obec", municipality_labels)
            title = st.text_input("Název nabídky", placeholder="Např. Nabídka energií 2026")
            signing_date = st.date_input("Datum podpisu nabídky", value=date.today())
            product_label = st.selectbox("Výchozí produkt", product_labels)
            lists = _price_list_options(conn, product_labels[product_label], signing_date)
            list_labels = {
                f"{name} · podpis {start:%d.%m.%Y}–{end.strftime('%d.%m.%Y') if end else 'bez konce'}": list_id
                for list_id, name, start, end in lists
            }
            selected_list = st.selectbox("Akční ceník", list_labels) if lists else None
            submitted = st.form_submit_button("Vytvořit nabídku", type="primary")
        if submitted:
            if not selected_list:
                st.error("Pro produkt není založen žádný ceník.")
            else:
                code, name = municipality_labels[municipality_label]
                quote_id = create_quote(
                    conn, code, name, product_labels[product_label],
                    list_labels[selected_list], signing_date, title, username,
                )
                st.session_state.energy_quote_id = quote_id
                st.success("Nabídka byla vytvořena. Pokračujte přidáním odběrných míst.")

    with calculation_tab:
        quotes = conn.execute("""
            SELECT q.id,q.customer_name,coalesce(q.title,''),q.signing_date,
                   count(sp.id)
            FROM energy_quotes q LEFT JOIN energy_supply_points sp ON sp.quote_id=q.id
            GROUP BY q.id,q.customer_name,q.title,q.signing_date,q.created_at
            ORDER BY q.created_at DESC
        """).fetchall()
        if not quotes:
            st.info("Nejprve vytvořte nabídku.")
        else:
            quote_labels = {
                f"{customer} · {title or 'nabídka'} · {signed:%d.%m.%Y} · {count} OM": quote_id
                for quote_id, customer, title, signed, count in quotes
            }
            default_id = st.session_state.get("energy_quote_id")
            labels = list(quote_labels)
            default_index = next((i for i, label in enumerate(labels) if quote_labels[label] == default_id), 0)
            quote_label = st.selectbox("Nabídka zákazníka", labels, index=default_index)
            quote_id = quote_labels[quote_label]
            signing_date = conn.execute(
                "SELECT signing_date FROM energy_quotes WHERE id=?", [quote_id]
            ).fetchone()[0]

            with st.expander("Přidat odběrné místo", expanded=True):
                commodity = st.radio("Komodita", COMMODITIES, horizontal=True)
                products = _product_options(conn, commodity)
                product_labels = {label: product_id for product_id, label, _, _ in products}
                product_label = st.selectbox("Produkt", product_labels, key="point_product")
                price_lists = _price_list_options(conn, product_labels[product_label], signing_date)
                price_labels = {name: price_id for price_id, name, _, _ in price_lists}
                selected_price_label = st.selectbox("Ceník", price_labels, key="point_price_list") if price_lists else None
                known_rates = []
                if selected_price_label:
                    known_rates = [r[0] for r in conn.execute(
                        "SELECT DISTINCT rate_band FROM energy_price_periods WHERE price_list_id=? ORDER BY 1",
                        [price_labels[selected_price_label]],
                    ).fetchall()]
                with st.form("energy_supply_point", clear_on_submit=True):
                    a, b = st.columns(2)
                    address = a.text_input("Adresa odběrného místa")
                    ean = b.text_input("EAN / EIC")
                    rate = a.selectbox("Distribuční sazba / pásmo", known_rates or ["Všechny"])
                    annual = b.number_input("Roční spotřeba (MWh)", min_value=0.0, step=0.1, format="%.3f")
                    vt_share = 100.0
                    current_nt = None
                    current_vt = a.number_input(
                        "Současná cena za MWh bez DPH" + (" – VT" if commodity == "Elektřina" else ""),
                        min_value=0.0, step=10.0,
                    )
                    if commodity == "Elektřina":
                        vt_share = b.number_input("Podíl VT (%)", min_value=0.0, max_value=100.0, value=100.0)
                        if vt_share < 100:
                            current_nt = b.number_input("Současná cena NT za MWh bez DPH", min_value=0.0, step=10.0)
                    supplier = a.text_input("Současný dodavatel")
                    current_fee = b.number_input("Současný stálý měsíční plat bez DPH", min_value=0.0, step=1.0)
                    contract_type = st.selectbox("Typ smlouvy", CONTRACT_TYPES)
                    if contract_type == "Doba určitá":
                        contract_end = st.date_input("Datum konce smlouvy")
                        notice_months, notice_date = None, None
                    else:
                        notice_months = st.number_input("Výpovědní doba (měsíce)", min_value=0, value=3, step=1)
                        notice_date = st.date_input("Datum podání výpovědi", value=signing_date)
                        contract_end = None
                    manual_start = st.checkbox("Zadat zahájení dodávky ručně")
                    explicit_start = st.date_input("Zahájení dodávky innogy") if manual_start else None
                    add_clicked = st.form_submit_button("Přidat a spočítat", type="primary")
                if add_clicked:
                    try:
                        supply_start = explicit_start or derive_supply_start(
                            contract_type, signing_date, contract_end,
                            notice_months, notice_date,
                        )
                        if not all([address.strip(), ean.strip(), supplier.strip(), selected_price_label]):
                            raise EnergyCalculationError("Vyplňte adresu, EAN/EIC, dodavatele a ceník.")
                        add_supply_point(
                            conn, quote_id, address, ean, commodity, rate, annual,
                            vt_share, supplier, current_vt, current_nt, current_fee,
                            contract_type, contract_end, notice_months, notice_date,
                            supply_start, product_labels[product_label],
                            price_labels[selected_price_label],
                        )
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"Odběrné místo přidáno. Zahájení dodávky: {supply_start:%d.%m.%Y}.")
                        st.rerun()

            try:
                result = calculate_quote(conn, quote_id)
            except EnergyCalculationError as exc:
                st.error(str(exc))
            else:
                if result["points"]:
                    m1, m2 = st.columns(2)
                    m1.metric("Celková úspora za 12 měsíců", _money(result["saving_12"]))
                    m2.metric("Celková úspora za 36 měsíců", _money(result["saving_36"]))
                    summary = []
                    for item in result["points"]:
                        y1, full = item["first_year"], item["full"]
                        summary.append({
                            "EAN/EIC": full["ean_eic"], "Adresa": full["address"],
                            "Komodita": full["commodity"], "Roční spotřeba MWh": full["annual_consumption"],
                            "Úspora 12 měsíců": y1["saving"], "Úspora 36 měsíců": full["saving"],
                        })
                    st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch")
                    for item in result["points"]:
                        y1, full = item["first_year"], item["full"]
                        with st.expander(f"{full['commodity']} · {full['ean_eic']} · {full['address']}"):
                            st.write(f"**Současný dodavatel:** {full['current_supplier']}")
                            current_price_text = f"{full['current_price_vt']:,.2f} Kč/MWh"
                            if full["current_price_nt"] is not None:
                                current_price_text += f" VT; {full['current_price_nt']:,.2f} Kč/MWh NT"
                            st.caption(
                                f"Současná cena: {current_price_text} · stálý plat "
                                f"{full['current_monthly_fee']:,.2f} Kč/měsíc · bez DPH"
                            )
                            c1, c2, c3 = st.columns(3)
                            c1.metric("Současný náklad / 12 měs.", _money(y1["current_total"]))
                            c2.metric("innogy / 12 měs.", _money(y1["innogy_total"]))
                            c3.metric("Úspora / 12 měs.", _money(y1["saving"]))
                            st.caption(
                                f"Rozpad 36 měsíců: energie {_money(full['energy_saving'])}, "
                                f"stálý plat {_money(full['fixed_saving'])}; celkem {_money(full['saving'])}."
                            )
                            period_frame = pd.DataFrame(full["periods"])
                            period_frame = period_frame.rename(columns={
                                "valid_from": "Od", "valid_to": "Do",
                                "component": "Složka",
                                "unit_price": "Cena Kč/MWh", "monthly_fee": "Stálý plat Kč/měs.",
                                "energy_cost": "Náklad energie Kč",
                            })
                            st.dataframe(period_frame, hide_index=True, width="stretch")
                else:
                    st.caption("Nabídka zatím nemá odběrná místa.")

    with admin_tab:
        if role != "admin":
            st.info("Ceníky může upravovat pouze administrátor.")
        else:
            st.warning("Změna ceníku se okamžitě projeví v přepočtu uložených nabídek.")
            products = _product_options(conn)
            product_labels = {label: product_id for product_id, label, _, _ in products}
            selected_product_label = st.selectbox("Produkt ceníku", product_labels, key="admin_product")
            selected_product = product_labels[selected_product_label]
            lists = _price_list_options(conn, selected_product)
            list_labels = {name: list_id for list_id, name, _, _ in lists}
            selected_list_label = st.selectbox("Akční ceník", list_labels, key="admin_list") if lists else None
            if selected_list_label:
                list_id = list_labels[selected_list_label]
                periods = conn.execute("""
                    SELECT id AS "ID",rate_band AS "Sazba / pásmo",component AS "VT / NT",
                           valid_from AS "Platí od",valid_to AS "Platí do",
                           unit_price AS "Kč/MWh",monthly_fee AS "Kč/měsíc"
                    FROM energy_price_periods WHERE price_list_id=?
                    ORDER BY rate_band,component,valid_from
                """, [list_id]).fetchdf()
                edited_periods = st.data_editor(
                    periods, hide_index=True, width="stretch", key="energy_period_editor",
                    disabled=["ID"], column_config={"ID": None}, num_rows="fixed",
                )
                if st.button("Uložit změny ceníku", type="primary"):
                    try:
                        for _, row in edited_periods.iterrows():
                            save_price_period(
                                conn, list_id, str(row["Sazba / pásmo"]),
                                str(row["VT / NT"]), pd.Timestamp(row["Platí od"]).date(),
                                None if pd.isna(row["Platí do"]) else pd.Timestamp(row["Platí do"]).date(),
                                float(row["Kč/MWh"]), float(row["Kč/měsíc"]), str(row["ID"]),
                            )
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.success("Změny ceníku byly uloženy.")
                        st.rerun()
                with st.form("energy_add_period", clear_on_submit=True):
                    rate = st.text_input("Sazba nebo pásmo", value="Všechny")
                    component = st.selectbox("Složka", ["Jednotná", "VT", "NT"])
                    valid_from = st.date_input("Cena platí od")
                    has_end = st.checkbox("Cena má datum konce", value=True)
                    valid_to = st.date_input("Cena platí do") if has_end else None
                    unit_price = st.number_input("Cena bez DPH (Kč/MWh)", min_value=0.0, step=0.1)
                    fee = st.number_input("Stálý plat bez DPH (Kč/měsíc)", min_value=0.0, step=1.0)
                    if st.form_submit_button("Přidat cenové období"):
                        try:
                            save_price_period(conn, list_id, rate, component, valid_from, valid_to, unit_price, fee)
                        except Exception as exc:
                            st.error(str(exc))
                        else:
                            st.success("Cenové období bylo přidáno.")
                            st.rerun()

            st.markdown("#### Přidat produkt nebo akční ceník")
            product_col, list_col = st.columns(2)
            with product_col.form("energy_add_product", clear_on_submit=True):
                supplier = st.text_input("Dodavatel", value="innogy")
                name = st.text_input("Název produktu")
                commodity = st.selectbox("Komodita", COMMODITIES, key="admin_new_commodity")
                fixation = st.number_input("Délka fixace (měsíce)", min_value=1, value=36)
                if st.form_submit_button("Přidat produkt"):
                    conn.execute("""
                        INSERT INTO energy_products (id,supplier,name,commodity,fixation_months)
                        VALUES (?,?,?,?,?)
                    """, [str(uuid4()), supplier.strip(), name.strip(), commodity, fixation])
                    st.rerun()
            with list_col.form("energy_add_list", clear_on_submit=True):
                new_list_name = st.text_input("Název akčního ceníku")
                signing_from = st.date_input("Podpis platí od")
                signing_has_end = st.checkbox("Podpis má datum konce", value=True)
                signing_to = st.date_input("Podpis platí do") if signing_has_end else None
                note = st.text_area("Poznámka")
                if st.form_submit_button("Přidat ceník"):
                    conn.execute("""
                        INSERT INTO energy_price_lists
                            (id,product_id,name,signing_valid_from,signing_valid_to,note)
                        VALUES (?,?,?,?,?,?)
                    """, [str(uuid4()), selected_product, new_list_name.strip(), signing_from, signing_to, note])
                    st.rerun()
