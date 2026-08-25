from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.energy_calculator import (
    COMMODITIES,
    CONTRACT_TYPES,
    CUSTOMER_TYPES,
    PROLONGATION_OUTCOMES,
    EnergyCalculationError,
    add_supply_point,
    calculate_quote,
    create_quote,
    derive_supply_start,
    add_months,
    resolve_prolongation_rule,
    save_prolongation_rule,
)
from app.energy_price_import import (
    EnergyPriceImportError,
    import_parsed_pdf,
    parse_innogy_price_pdf,
)
from app.invoice_import import InvoiceImportError, parse_supplier_invoice_pdf
from app.energy_pdf import build_energy_offer_pdf
from app.crm import save_offer_document


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
        return matching
    return rows


def _as_date(value):
    """Convert Streamlit/pandas date editor values to plain dates."""
    if value is None or pd.isna(value):
        return None
    return value.date() if hasattr(value, "date") else value


def _default_product_price_list(conn, commodity, signing_date):
    products = _product_options(conn, commodity)
    products.sort(key=lambda row: ("optimal 36" not in row[1].lower(), row[1]))
    for product_id, _label, _commodity, _months in products:
        price_lists = _price_list_options(conn, product_id, signing_date)
        if price_lists:
            return product_id, price_lists[0][0]
    raise EnergyCalculationError(
        f"Pro komoditu {commodity} a datum nabídky není dostupný akční ceník."
    )


def _invoice_row(conn, extracted, signing_date):
    stated_contract_end = extracted["contract_end_date"]
    fixed = (
        extracted["contract_type"] == "Doba určitá"
        and stated_contract_end
        and stated_contract_end >= signing_date
    )
    warnings = list(extracted.get("warnings", []))
    rule = None
    if stated_contract_end and stated_contract_end < signing_date:
        rule = resolve_prolongation_rule(
            conn, extracted["supplier"], extracted.get("current_product", ""),
            extracted["commodity"], extracted["billing_to"],
        )
    if fixed:
        contract_type, contract_end = "Doba určitá", stated_contract_end
        notice_months, notice_date = 0, None
        supply_start = contract_end + timedelta(days=1)
    elif rule and rule["outcome"] == "Znovu na dobu určitou":
        contract_type, contract_end = "Doba určitá", stated_contract_end
        while contract_end < signing_date:
            contract_end = add_months(contract_end, int(rule["renewal_months"]))
        notice_months, notice_date = 0, None
        supply_start = contract_end + timedelta(days=1)
        warnings.append(
            f"Použito pravidlo prolongace: prodloužení o {rule['renewal_months']} měsíců."
        )
    else:
        contract_type, contract_end = "Doba neurčitá", None
        notice_months = int(rule["notice_months"] or 0) if rule else 3
        notice_date = signing_date
        supply_start = derive_supply_start(
            contract_type, signing_date, None, notice_months, notice_date
        )
        if rule:
            warnings.append(
                f"Použito pravidlo prolongace: přechod na dobu neurčitou, "
                f"výpovědní doba {notice_months} měsíců."
            )
        elif stated_contract_end and stated_contract_end < signing_date:
            warnings.append(
                "Pro tento produkt není založené pravidlo prolongace. Použit je pouze "
                "pracovní odhad tří měsíců; termín je nutné ověřit."
            )
    return {
        "Soubor": extracted["file_name"],
        "Dodavatel": extracted["supplier"],
        "Komodita": extracted["commodity"],
        "Adresa odběrného místa": extracted["address"],
        "EAN / EIC": extracted["ean_eic"],
        "Sazba / pásmo": extracted["rate_band"],
        "Roční spotřeba MWh": float(extracted["annual_consumption_mwh"]),
        "Podíl VT %": float(extracted["vt_share"]),
        "Cena VT / MWh": float(extracted["current_price_vt"]),
        "Cena NT / MWh": extracted["current_price_nt"],
        "Stálý plat / měsíc": float(extracted["current_monthly_fee"]),
        "Typ smlouvy": contract_type,
        "Konec smlouvy": contract_end,
        "Výpovědní doba": notice_months,
        "Datum výpovědi": notice_date,
        "Začátek innogy": supply_start,
        "Faktura od": extracted["billing_from"],
        "Faktura do": extracted["billing_to"],
        "Zdroj spotřeby": extracted["consumption_source"],
        "Upozornění": " ".join(warnings),
    }


def render_energy_calculator(conn, username, role):
    st.subheader("Kalkulačka cenových nabídek energií")
    st.caption("Obchodní část ceny bez DPH. Regulovaná část se do porovnání nezahrnuje.")
    active_section = st.radio(
        "Část kalkulačky",
        ["Nová nabídka", "Odběrná místa a výpočet", "Administrace ceníků"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if active_section == "Nová nabídka":
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
            customer_type = st.radio("Typ zákazníka", CUSTOMER_TYPES, horizontal=True)
            if customer_type == "Obec":
                municipality_label = st.selectbox("Zákazník / obec", municipality_labels)
                household_name = ""
            else:
                municipality_label = None
                household_name = st.text_input(
                    "Jméno zákazníka / domácnosti",
                    placeholder="Např. Jan Novák",
                )
            title = st.text_input("Název nabídky", placeholder="Např. Nabídka energií 2026")
            signing_date = st.date_input("Datum vypracování nabídky", value=date.today())
            product_label = st.selectbox("Výchozí produkt", product_labels)
            lists = _price_list_options(conn, product_labels[product_label], signing_date)
            list_labels = {
                f"{name} · platnost akce {start:%d.%m.%Y}–{end.strftime('%d.%m.%Y') if end else 'bez konce'}": list_id
                for list_id, name, start, end in lists
            }
            selected_list = st.selectbox("Akční ceník", list_labels) if lists else None
            submitted = st.form_submit_button("Vytvořit nabídku", type="primary")
        if submitted:
            if not selected_list:
                st.error("Pro produkt není založen žádný ceník.")
            else:
                if customer_type == "Obec":
                    code, name = municipality_labels[municipality_label]
                else:
                    code, name = None, household_name.strip()
                try:
                    quote_id = create_quote(
                        conn, code, name, product_labels[product_label],
                        list_labels[selected_list], signing_date, title, username,
                        customer_type,
                    )
                except EnergyCalculationError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.energy_quote_id = quote_id
                    st.success("Nabídka byla vytvořena. Pokračujte přidáním odběrných míst.")

    if active_section == "Odběrná místa a výpočet":
        quotes = conn.execute("""
            SELECT q.id,q.customer_name,coalesce(q.title,''),q.signing_date,
                   count(sp.id),coalesce(q.customer_type,'Obec')
            FROM energy_quotes q LEFT JOIN energy_supply_points sp ON sp.quote_id=q.id
            GROUP BY q.id,q.customer_name,q.title,q.signing_date,q.customer_type,q.created_at
            ORDER BY q.created_at DESC
        """).fetchall()
        if not quotes:
            st.info("Nejprve vytvořte nabídku.")
        else:
            quote_labels = {
                f"{'🏛️' if customer_type == 'Obec' else '🏠'} {customer} · "
                f"{title or 'nabídka'} · {signed:%d.%m.%Y} · {count} OM": quote_id
                for quote_id, customer, title, signed, count, customer_type in quotes
            }
            default_id = st.session_state.get("energy_quote_id")
            labels = list(quote_labels)
            default_index = next((i for i, label in enumerate(labels) if quote_labels[label] == default_id), 0)
            quote_label = st.selectbox("Nabídka zákazníka", labels, index=default_index)
            quote_id = quote_labels[quote_label]
            signing_date = conn.execute(
                "SELECT signing_date FROM energy_quotes WHERE id=?", [quote_id]
            ).fetchone()[0]

            st.markdown("### Hromadné nahrání faktur")
            st.info(
                "Vyberte najednou všechny faktury obce (například 20 PDF). "
                "Každá faktura se načte jako samostatné odběrné místo a před uložením "
                "můžete všechny údaje upravit v tabulce."
            )
            nonce_key = f"energy_invoice_nonce_{quote_id}"
            upload_nonce = int(st.session_state.get(nonce_key, 0))
            invoice_files = st.file_uploader(
                "Přetáhněte sem faktury současných dodavatelů",
                type=["pdf"], accept_multiple_files=True,
                key=f"energy_invoices_{quote_id}_{upload_nonce}",
                help="Lze označit více PDF současně. Jedno PDF představuje jedno odběrné místo.",
            )
            if invoice_files:
                parsed_invoices = []
                failed_invoices = []
                for invoice in invoice_files:
                    try:
                        extracted = parse_supplier_invoice_pdf(
                            invoice.getvalue(), invoice.name
                        )
                    except InvoiceImportError as exc:
                        failed_invoices.append(f"{invoice.name}: {exc}")
                    else:
                        parsed_invoices.append(extracted)

                if failed_invoices:
                    st.error("Některé soubory se nepodařilo načíst:\n\n- " + "\n- ".join(failed_invoices))
                if parsed_invoices:
                    st.success(
                        f"Načteno {len(parsed_invoices)} z {len(invoice_files)} faktur. "
                        "Zkontrolujte hlavně barevně označené nebo prázdné údaje."
                    )
                    invoice_frame = pd.DataFrame(
                        [_invoice_row(conn, item, signing_date) for item in parsed_invoices]
                    )
                    edited_invoices = st.data_editor(
                        invoice_frame,
                        use_container_width=True,
                        hide_index=True,
                        key=f"energy_invoice_table_{quote_id}_{upload_nonce}",
                        disabled=["Soubor", "Faktura od", "Faktura do", "Zdroj spotřeby", "Upozornění"],
                        column_config={
                            "Komodita": st.column_config.SelectboxColumn(options=COMMODITIES, required=True),
                            "Typ smlouvy": st.column_config.SelectboxColumn(options=CONTRACT_TYPES, required=True),
                            "Roční spotřeba MWh": st.column_config.NumberColumn(min_value=0.0, format="%.6f"),
                            "Podíl VT %": st.column_config.NumberColumn(min_value=0.0, max_value=100.0),
                            "Cena VT / MWh": st.column_config.NumberColumn(min_value=0.0, format="%.2f Kč"),
                            "Cena NT / MWh": st.column_config.NumberColumn(min_value=0.0, format="%.2f Kč"),
                            "Stálý plat / měsíc": st.column_config.NumberColumn(min_value=0.0, format="%.2f Kč"),
                            "Konec smlouvy": st.column_config.DateColumn(format="DD.MM.YYYY"),
                            "Datum výpovědi": st.column_config.DateColumn(format="DD.MM.YYYY"),
                            "Začátek innogy": st.column_config.DateColumn(format="DD.MM.YYYY", required=True),
                        },
                    )
                    st.caption(
                        "U smlouvy na dobu určitou vyplňte konec smlouvy. U smlouvy na dobu "
                        "neurčitou zkontrolujte výpovědní dobu, datum výpovědi a začátek dodávky."
                    )
                    normalized_eans = edited_invoices["EAN / EIC"].astype(str).str.strip().str.upper()
                    duplicate_eans = sorted(set(normalized_eans[normalized_eans.duplicated(False)]))
                    if duplicate_eans:
                        st.warning(
                            f"Nalezeno {len(duplicate_eans)} opakovaných EAN/EIC. "
                            "Pro každé odběrné místo bude použita faktura s nejnovějším "
                            "koncem fakturačního období."
                        )
                    confirmed = st.checkbox(
                        f"Zkontrolovala jsem údaje všech {len(edited_invoices)} odběrných míst",
                        key=f"confirm_invoice_batch_{quote_id}_{upload_nonce}",
                    )
                    if st.button(
                        f"Uložit všech {len(edited_invoices)} odběrných míst a spočítat úsporu",
                        type="primary", key=f"save_invoice_batch_{quote_id}_{upload_nonce}",
                    ):
                        try:
                            if not confirmed:
                                raise EnergyCalculationError("Nejprve potvrďte kontrolu všech údajů.")
                            if edited_invoices.empty:
                                raise EnergyCalculationError("Není vybrané žádné odběrné místo.")
                            conn.execute("BEGIN TRANSACTION")
                            rows_to_save = edited_invoices.sort_values(
                                "Faktura do", na_position="first"
                            )
                            for _, row in rows_to_save.iterrows():
                                if not str(row["Adresa odběrného místa"]).strip() or not str(row["EAN / EIC"]).strip():
                                    raise EnergyCalculationError(
                                        f"U souboru {row['Soubor']} chybí adresa nebo EAN/EIC."
                                    )
                                current_price = row["Cena VT / MWh"]
                                if pd.isna(current_price) or float(current_price) <= 0:
                                    raise EnergyCalculationError(
                                        f"U souboru {row['Soubor']} doplňte obchodní cenu za MWh."
                                    )
                                contract_type = row["Typ smlouvy"]
                                contract_end = _as_date(row["Konec smlouvy"])
                                notice_date = _as_date(row["Datum výpovědi"])
                                notice_months = int(row["Výpovědní doba"] or 0)
                                if contract_type == "Doba určitá" and not contract_end:
                                    raise EnergyCalculationError(
                                        f"U souboru {row['Soubor']} doplňte konec smlouvy."
                                    )
                                product_id, price_list_id = _default_product_price_list(
                                    conn, row["Komodita"], signing_date
                                )
                                price_nt = row["Cena NT / MWh"]
                                add_supply_point(
                                    conn, quote_id, row["Adresa odběrného místa"], row["EAN / EIC"],
                                    row["Komodita"], row["Sazba / pásmo"], float(row["Roční spotřeba MWh"]),
                                    float(row["Podíl VT %"]), row["Dodavatel"], float(row["Cena VT / MWh"]),
                                    None if pd.isna(price_nt) else float(price_nt), float(row["Stálý plat / měsíc"]),
                                    contract_type, contract_end if contract_type == "Doba určitá" else None,
                                    None if contract_type == "Doba určitá" else notice_months,
                                    None if contract_type == "Doba určitá" else notice_date,
                                    _as_date(row["Začátek innogy"]), product_id, price_list_id,
                                    row["Soubor"], _as_date(row["Faktura od"]), _as_date(row["Faktura do"]),
                                    row["Zdroj spotřeby"],
                                )
                            conn.execute("COMMIT")
                        except Exception as exc:
                            try:
                                conn.execute("ROLLBACK")
                            except Exception:
                                pass
                            st.error(str(exc))
                        else:
                            unique_count = normalized_eans[normalized_eans.ne("")].nunique()
                            st.success(
                                f"Uloženo {unique_count} unikátních odběrných míst"
                                + (f"; {len(edited_invoices) - unique_count} duplicitních faktur aktualizovalo stejné OM."
                                   if unique_count < len(edited_invoices) else ".")
                            )
                            # Never mutate the state of widgets already rendered
                            # in this run. A fresh key safely clears the batch.
                            st.session_state[nonce_key] = upload_nonce + 1
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
                            "Úspora za 12 měsíců (Kč)": y1["saving"],
                            "Úspora za celé období 36 měsíců (Kč)": full["saving"],
                        })
                    st.dataframe(
                        pd.DataFrame(summary), hide_index=True, width="stretch",
                        column_config={
                            "Roční spotřeba MWh": st.column_config.NumberColumn(format="%.3f MWh"),
                            "Úspora za 12 měsíců (Kč)": st.column_config.NumberColumn(format="%.2f Kč"),
                            "Úspora za celé období 36 měsíců (Kč)": st.column_config.NumberColumn(format="%.2f Kč"),
                        },
                    )
                    pdf_data = build_energy_offer_pdf(conn, quote_id, result)
                    safe_customer = "".join(
                        char if char.isalnum() or char in "-_" else "_"
                        for char in quote_label.split("·", 1)[0].strip()
                    )
                    pdf_file_name = f"nabidka_energii_{safe_customer}.pdf"
                    quote_owner = conn.execute("""
                        SELECT kod_obce,coalesce(customer_type,'Obec')
                        FROM energy_quotes WHERE id=?
                    """, [quote_id]).fetchone()
                    can_save_to_municipality = bool(
                        quote_owner and quote_owner[1] == "Obec" and quote_owner[0] is not None
                    )
                    if can_save_to_municipality:
                        download_col, save_col = st.columns(2)
                    else:
                        download_col, save_col = st.container(), None
                    with download_col:
                        st.download_button(
                            "📄 Stáhnout grafickou nabídku v PDF",
                            data=pdf_data,
                            file_name=pdf_file_name,
                            mime="application/pdf",
                            type="primary",
                            key=f"energy_pdf_{quote_id}",
                            width="stretch",
                        )
                    if save_col is not None:
                        with save_col:
                            if st.button(
                                "💾 Uložit do karty obce", key=f"save_energy_pdf_{quote_id}",
                                width="stretch",
                            ):
                                _, replaced = save_offer_document(
                                    conn, quote_owner[0], quote_id, pdf_file_name,
                                    pdf_data, username,
                                )
                                st.success(
                                    "Aktualizovaná nabídka byla uložena do karty obce."
                                    if replaced else "Nabídka byla uložena do karty obce."
                                )
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
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Současný náklad / 12 měs.", _money(y1["current_total"]))
                            c2.metric("innogy / 12 měs.", _money(y1["innogy_total"]))
                            c3.metric("Úspora / 12 měs.", _money(y1["saving"]))
                            c4.metric("Úspora / celých 36 měs.", _money(full["saving"]))
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

    if active_section == "Administrace ceníků":
        if role != "admin":
            st.info("Ceníky může upravovat pouze administrátor.")
        else:
            st.markdown("#### Pravidla prolongací")
            st.caption(
                "Pravidlo se páruje podle části názvu dodavatele a produktu přečtených "
                "z faktury. Přesnější název produktu má přednost."
            )
            rules = conn.execute("""
                SELECT supplier_pattern AS "Dodavatel obsahuje",
                       product_pattern AS "Produkt obsahuje",commodity AS "Komodita",
                       valid_from AS "Platí od",valid_to AS "Platí do",
                       outcome AS "Po skončení",renewal_months AS "Prodloužení měs.",
                       notice_months AS "Výpověď měs.",source_url AS "Zdroj",
                       note AS "Poznámka"
                FROM energy_prolongation_rules WHERE active
                ORDER BY commodity,supplier_pattern,product_pattern,valid_from DESC
            """).fetchdf()
            if not rules.empty:
                st.dataframe(rules, hide_index=True, width="stretch")
            with st.expander("Přidat pravidlo prolongace"):
                with st.form("energy_prolongation_rule", clear_on_submit=True):
                    a, b = st.columns(2)
                    rule_supplier = a.text_input("Dodavatel obsahuje", placeholder="např. ČEZ")
                    rule_product = b.text_input("Produkt obsahuje", placeholder="např. Bez starostí na 1 rok")
                    rule_commodity = a.selectbox("Komodita", COMMODITIES)
                    rule_outcome = b.selectbox("Po skončení smlouvy", PROLONGATION_OUTCOMES)
                    rule_from = a.date_input("Pravidlo platí od", value=date(2020, 1, 1))
                    use_rule_to = b.checkbox("Má datum konce platnosti")
                    rule_to = b.date_input("Pravidlo platí do") if use_rule_to else None
                    if rule_outcome == "Znovu na dobu určitou":
                        renewal_months = a.number_input("Prodloužení (měsíce)", min_value=1, value=12, step=1)
                        notice_rule_months = None
                    else:
                        renewal_months = None
                        notice_rule_months = a.number_input("Výpovědní doba (měsíce)", min_value=0, value=3, step=1)
                    rule_source = st.text_input("Odkaz na oficiální podmínky")
                    rule_note = st.text_area("Poznámka")
                    save_rule = st.form_submit_button("Uložit pravidlo", type="primary")
                if save_rule:
                    try:
                        if not rule_supplier.strip() or not rule_product.strip():
                            raise EnergyCalculationError("Vyplňte dodavatele a produkt.")
                        save_prolongation_rule(
                            conn, rule_supplier, rule_product, rule_commodity,
                            rule_from, rule_to, rule_outcome, renewal_months,
                            notice_rule_months, rule_source, rule_note,
                        )
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.success("Pravidlo prolongace bylo uloženo.")
                        st.rerun()

            st.markdown("#### Aktuální měsíční akční ceník")
            current = conn.execute("""
                SELECT pl.name,pl.signing_valid_from,pl.signing_valid_to,
                       count(pp.id),pl.note
                FROM energy_price_lists pl
                LEFT JOIN energy_price_periods pp ON pp.price_list_id=pl.id
                WHERE pl.active
                GROUP BY pl.id,pl.name,pl.signing_valid_from,pl.signing_valid_to,pl.note
                ORDER BY pl.signing_valid_from DESC,pl.name
            """).fetchdf()
            if current.empty:
                st.info("Zatím není nahraný žádný aktuální ceník.")
            else:
                current.columns = ["Ceník", "Podpis od", "Podpis do", "Cenových řádků", "Zdroj"]
                st.dataframe(current, hide_index=True, width="stretch")

            st.markdown("#### Nahrát nový ceník")
            st.caption(
                "Nahrajte původní PDF Innogy pro elektřinu, plyn nebo obě komodity. "
                "Platnost akce i všechny ceny aplikace přečte přímo z dokumentu."
            )
            uploaded_prices = st.file_uploader(
                "Akční ceník PDF", type=["pdf"], accept_multiple_files=True,
                key="energy_price_upload",
            )
            parsed_files = []
            if uploaded_prices:
                try:
                    parsed_files = [
                        parse_innogy_price_pdf(upload.getvalue(), upload.name)
                        for upload in uploaded_prices
                    ]
                    unique_keys = {
                        (item["commodity"], item["signing_from"], item["signing_to"])
                        for item in parsed_files
                    }
                    if len(unique_keys) != len(parsed_files):
                        raise EnergyPriceImportError(
                            "Pro jednu komoditu a měsíc nahrajte pouze jeden PDF ceník."
                        )
                except EnergyPriceImportError as exc:
                    st.error(str(exc))
                else:
                    st.markdown("##### Kontrola rozpoznaných cen")
                    for parsed in parsed_files:
                        st.write(
                            f"**{parsed['commodity']} · {parsed['product']}** — podpis "
                            f"{parsed['signing_from']:%d.%m.%Y} až {parsed['signing_to']:%d.%m.%Y}"
                        )
                        preview = pd.DataFrame(parsed["rows"]).rename(columns={
                            "rate": "Sazba / pásmo", "component": "Složka",
                            "valid_from": "Cena od", "valid_to": "Cena do",
                            "unit_price": "Kč/MWh bez DPH",
                            "monthly_fee": "Kč/měsíc bez DPH",
                        })
                        st.dataframe(
                            preview[["Sazba / pásmo", "Složka", "Cena od", "Cena do",
                                     "Kč/MWh bez DPH", "Kč/měsíc bez DPH"]],
                            hide_index=True, width="stretch",
                        )
            if parsed_files and st.button("Potvrdit a aktivovat ceník", type="primary"):
                try:
                    results = [import_parsed_pdf(conn, parsed, username) for parsed in parsed_files]
                except Exception as exc:
                    st.error(f"Ceník se nepodařilo uložit: {exc}")
                else:
                    st.success(
                        f"Aktivováno {len(results)} PDF a "
                        f"{sum(result['rows'] for result in results)} cenových řádků."
                    )
                    st.rerun()

            history = conn.execute("""
                SELECT action_month AS "Měsíc",file_name AS "Soubor",
                       row_count AS "Řádků",list_count AS "Ceníků",
                       imported_at AS "Nahráno",imported_by AS "Uživatel"
                FROM energy_price_imports ORDER BY imported_at DESC LIMIT 24
            """).fetchdf()
            if not history.empty:
                st.markdown("#### Historie nahrání")
                st.dataframe(history, hide_index=True, width="stretch")
