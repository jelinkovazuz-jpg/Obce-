from io import BytesIO
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from geopy.geocoders import Nominatim

# Streamlit Cloud may include packages whose names collide with local modules.
# Import everything through the explicit application package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import login, logout
from app.crm import (
    ACTIVITY_TYPES,
    PRIORITIES,
    STATUSES,
    add_activity,
    add_task,
    connect,
    init_crm,
    save_record,
    set_contacted_on,
    set_task_completed,
    update_inline_crm,
    update_municipality,
    init_crm_documents,
)
from app.distance import vzdalenost
from app.email_sync import EmailSyncError, init_email_sync, sync_mailbox
from app.email_sender import (
    MAX_BATCH_SIZE,
    EmailSendError,
    init_email_sender,
    send_individual_messages,
)
from app.innogy_import import InnogyImportError, import_contracts, init_innogy
from app.energy_calculator import init_energy_calculator
from app.energy_price_import import ensure_bundled_price_lists
from app.energy_ui import render_energy_calculator


load_dotenv()
st.set_page_config(page_title="CRM obcí ČR", page_icon="🏛️", layout="wide")

RESPONSE_DEADLINE_DAYS = 7
RESPONSE_STATES = ["Neosloveno", "Čekáme", "Bez odpovědi", "Odpověděli"]


def response_status(last_sent, last_received, now=None):
    if last_sent is None:
        return "Neosloveno", 0
    if last_received is not None and last_received > last_sent:
        return "Odpověděli", 0
    now = now or pd.Timestamp.now().to_pydatetime()
    waiting_days = max((now - last_sent).days, 0)
    state = "Bez odpovědi" if waiting_days >= RESPONSE_DEADLINE_DAYS else "Čekáme"
    return state, waiting_days


def configured_value(name, default=""):
    """Read local .env values or Streamlit Cloud secrets without exposing them."""
    try:
        secret = st.secrets.get(name)
    except (FileNotFoundError, KeyError):
        secret = None
    return str(secret) if secret else os.getenv(name, default)


@st.fragment(run_every="10m")
def automatic_email_sync():
    now = datetime.now()
    last_attempt = st.session_state.get("email_last_auto_sync_at")
    if last_attempt and now - last_attempt < timedelta(minutes=10):
        return

    address = configured_value("SEZNAM_EMAIL_ADDRESS", "program.obce@email.cz")
    password = configured_value("SEZNAM_EMAIL_PASSWORD") or st.session_state.get(
        "email_password_input", ""
    )
    if not password:
        st.info(
            "Automatická synchronizace čeká na heslo. Zadejte ho níže nebo ho "
            "uložte do Streamlit Secrets."
        )
        return

    # A normal Streamlit rerun also executes fragments. Remember the attempt
    # before connecting so every widget click cannot reopen the IMAP mailbox.
    st.session_state.email_last_auto_sync_at = now
    sync_conn = connect()
    try:
        init_crm(sync_conn)
        init_email_sync(sync_conn)
        stats = sync_mailbox(
            sync_conn, address, password, st.session_state.username, limit=100
        )
    except EmailSyncError as exc:
        st.warning(f"Automatická synchronizace se nezdařila: {exc}")
    finally:
        sync_conn.close()

    if "stats" in locals():
        checked_at = now.strftime("%d.%m.%Y %H:%M:%S")
        st.session_state.email_last_auto_sync = checked_at
        st.success(
            f"Automatická synchronizace aktivní · poslední kontrola {checked_at} · "
            f"nové zprávy: {stats['new']}"
        )

if "logged" not in st.session_state:
    st.session_state.logged = False
if not st.session_state.logged:
    login()
    st.stop()

conn = connect()
SCHEMA_VERSION = 3
if st.session_state.get("schema_version") != SCHEMA_VERSION:
    init_crm(conn)
    init_email_sync(conn)
    init_email_sender(conn)
    init_innogy(conn)
    init_energy_calculator(conn)
    ensure_bundled_price_lists(conn)
    st.session_state.schema_version = SCHEMA_VERSION

st.markdown(
    """
    <style>
    .main h1 { color: #0F4C81; }
    div[data-testid="stMetric"] {
        border: 1px solid #e8e8e8; border-radius: 12px;
        padding: 12px; background: #fafafa;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def render_municipality_documents(document_conn, kod_obce, key_prefix):
    # Streamlit Cloud can hot-reload this view while retaining an older DB
    # connection. Run the small idempotent migration at the point of use.
    init_crm_documents(document_conn)
    st.markdown("#### Uložené nabídky a dokumenty")
    documents = document_conn.execute("""
        SELECT id,document_type,file_name,mime_type,file_data,updated_at,created_by
        FROM crm_documents WHERE kod_obce=? ORDER BY updated_at DESC
    """, [kod_obce]).fetchall()
    if not documents:
        st.caption("V kartě zatím nejsou uložené žádné dokumenty.")
        return
    for document_id, document_type, file_name, mime_type, file_data, saved_at, created_by in documents:
        left, right = st.columns([3, 1])
        left.write(f"**{document_type}**  \n{file_name}")
        left.caption(
            f"Uloženo {saved_at:%d.%m.%Y %H:%M}"
            + (f" · {created_by}" if created_by else "")
        )
        right.download_button(
            "📄 Stáhnout", data=bytes(file_data), file_name=file_name,
            mime=mime_type, key=f"{key_prefix}_{document_id}", width="stretch",
        )


@st.dialog("Karta obce", width="large")
def quick_municipality_card(kod_obce):
    # A dialog reruns independently from the main page. It therefore needs
    # its own connection instead of the main connection, which may be closed.
    card_conn = connect()
    init_crm(card_conn)
    init_email_sync(card_conn)
    init_innogy(card_conn)
    obec = card_conn.execute("""
        SELECT o.nazev,o.okres,o.kraj,o.web,o.email,o.telefon,o.ico,
               coalesce(c.status,'Nová'),coalesce(c.priority,'Střední'),
               coalesce(c.owner_username,''),c.next_contact,coalesce(c.note,''),
               c.contacted_on
        FROM obce o LEFT JOIN crm_records c USING (kod_obce)
        WHERE o.kod_obce=?
    """, [kod_obce]).fetchone()
    if not obec:
        st.error("Obec už v databázi neexistuje.")
        card_conn.close()
        return

    st.subheader(f"🏛️ {obec[0]}")
    st.caption(f"{obec[1] or 'Bez okresu'} · {obec[2] or 'Bez kraje'} · kód {kod_obce}")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**E-mail**  \n{obec[4] or '—'}")
    c2.markdown(f"**Telefon**  \n{obec[5] or '—'}")
    c3.markdown(f"**IČO**  \n{obec[6] or '—'}")
    if obec[3]:
        st.link_button("🌐 Otevřít web obce", obec[3])

    users = card_conn.execute(
        "SELECT username,display_name FROM users WHERE active ORDER BY display_name"
    ).fetchall()
    user_labels = {"— Nepřiřazeno —": ""} | {
        f"{name} ({username})": username for username, name in users
    }
    owner_keys = list(user_labels)
    current_owner = next(
        (label for label, username in user_labels.items() if username == obec[9]),
        owner_keys[0],
    )
    with st.form(f"quick_card_{kod_obce}"):
        a, b, c = st.columns(3)
        status = a.selectbox("Stav", STATUSES, index=STATUSES.index(obec[7]))
        priority = b.selectbox("Priorita", PRIORITIES, index=PRIORITIES.index(obec[8]))
        owner = c.selectbox("Obchodník", owner_keys, index=owner_keys.index(current_owner))
        has_next = st.checkbox("Naplánovat další kontakt", value=obec[10] is not None)
        next_contact = st.date_input(
            "Další kontakt", value=obec[10] or "today", disabled=not has_next
        )
        note = st.text_area("Poznámka", value=obec[11], height=100)
        if st.form_submit_button("💾 Uložit kartu", type="primary"):
            save_record(
                card_conn, kod_obce, status, priority, user_labels[owner],
                next_contact if has_next else None, note,
            )
            st.toast("Karta obce byla uložena.", icon="✅")
            card_conn.close()
            st.rerun(scope="app")

    st.markdown("#### Poslední e-maily")
    messages = card_conn.execute("""
        SELECT direction,subject,sent_at FROM crm_emails
        WHERE kod_obce=? ORDER BY sent_at DESC LIMIT 5
    """, [kod_obce]).fetchall()
    if messages:
        for direction, subject, sent_at in messages:
            icon = "📤" if direction == "Odesláno" else "📥"
            when = sent_at.strftime("%d.%m.%Y %H:%M") if sent_at else "bez data"
            st.write(f"{icon} **{subject}** · {when}")
    else:
        st.caption("Zatím bez synchronizované komunikace.")
    response_info = card_conn.execute("""
        SELECT
            max(sent_at) FILTER (WHERE direction='Odesláno'),
            max(sent_at) FILTER (
                WHERE direction='Přijato'
                  AND lower(coalesce(sender,'')) NOT LIKE '%mailer-daemon%'
                  AND lower(coalesce(sender,'')) NOT LIKE '%no-reply%'
                  AND lower(coalesce(sender,'')) NOT LIKE '%noreply%'
                  AND lower(coalesce(subject,'')) NOT LIKE '%automatická odpověď%'
                  AND lower(coalesce(subject,'')) NOT LIKE '%automatic reply%'
            )
        FROM crm_emails WHERE kod_obce=?
    """, [kod_obce]).fetchone()
    last_sent, last_received = response_info
    response_label, _ = response_status(last_sent, last_received)
    st.caption(
        f"Poslední oslovení: {last_sent.strftime('%d.%m.%Y') if last_sent else '—'} · "
        f"Stav odpovědi: {response_label}"
    )
    innogy_summary = card_conn.execute("""
        SELECT count(*),count(distinct ean_eic),
               string_agg(distinct commodity, ', ' ORDER BY commodity)
        FROM innogy_contracts WHERE kod_obce=?
    """, [kod_obce]).fetchone()
    st.markdown("#### Innogy smlouvy")
    if innogy_summary[0]:
        i1, i2, i3 = st.columns(3)
        i1.metric("Smluv", innogy_summary[0])
        i2.metric("Odběrných míst", innogy_summary[1])
        i3.metric("Komodity", innogy_summary[2] or "—")
        innogy_rows = card_conn.execute("""
            SELECT commodity AS "Komodita",ean_eic AS "EAN/EIC",
                   product AS "Produkt",opportunity_status AS "Stav",
                   signed_at AS "Podepsáno",verification_status AS "Ověření",
                   consumption_gas AS "Spotřeba ZP",consumption_high AS "Spotřeba VT",
                   consumption_low AS "Spotřeba NT"
            FROM innogy_contracts WHERE kod_obce=? ORDER BY signed_at DESC NULLS LAST
        """, [kod_obce]).fetchdf()
        st.dataframe(innogy_rows, hide_index=True, width="stretch")
    else:
        st.caption("K této obci zatím nejsou spárované smlouvy Innogy.")
    render_municipality_documents(card_conn, kod_obce, "quick_document")
    if st.button("Zavřít kartu", key=f"close_quick_{kod_obce}"):
        card_conn.close()
        st.rerun(scope="app")
    card_conn.close()

with st.sidebar:
    st.success(f"👤 {st.session_state.display_name}")
    st.caption(f"Role: {st.session_state.role}")
    if st.button("🚪 Odhlásit", width="stretch"):
        logout()
    st.divider()
    st.caption("CRM obcí ČR")
    active_page = st.radio(
        "Navigace",
        ["🔎 Vyhledávání", "📊 Pipeline", "🏢 Detail obce", "✅ Úkoly",
         "📧 E-mail", "⚡ Innogy", "💡 Kalkulace"],
        label_visibility="collapsed",
    )

st.title("🏛️ CRM obcí ČR")
st.caption("Vyhledávání obcí, obchodní pipeline, aktivity a úkoly")
automatic_email_sync()

if active_page == "🔎 Vyhledávání":
    left, middle, right = st.columns([2, 1, 1])
    with left:
        mesto = st.text_input("Výchozí obec", value="Heřmanův Městec")
    with middle:
        polomer = st.slider("Poloměr (km)", 1, 100, 20)
    with right:
        st.write("")
        st.write("")
        hledat = st.button("🔎 Vyhledat", type="primary", width="stretch")

    if hledat:
        local = conn.execute(
            """
            SELECT latitude, longitude FROM obce
            WHERE lower(nazev) = lower(?) AND latitude IS NOT NULL
            ORDER BY kod_obce LIMIT 1
            """,
            [mesto.strip()],
        ).fetchone()

        if local:
            latitude, longitude = local
        else:
            try:
                location = Nominatim(user_agent="obce_crm_app", timeout=5).geocode(
                    f"{mesto}, Česká republika"
                )
            except Exception:
                location = None
            if location is None:
                st.error("Obec nebyla nalezena. Zkontrolujte její název.")
                st.stop()
            latitude, longitude = location.latitude, location.longitude

        st.session_state.search_context = (latitude, longitude, polomer)

    if "search_context" in st.session_state:
        latitude, longitude, active_radius = st.session_state.search_context
        rows = conn.execute(
            """
            SELECT o.kod_obce, o.nazev, o.okres, o.kraj, o.latitude, o.longitude,
                   o.web, o.email, o.telefon, o.ico,
                   coalesce(c.status, 'Nová') AS status, c.contacted_on,
                   CASE WHEN u.username IS NOT NULL
                        THEN u.display_name || ' (' || u.username || ')'
                        ELSE coalesce(c.owner_username, '—') END AS owner,
                   em.last_sent, em.last_received
            FROM obce o
            LEFT JOIN crm_records c USING (kod_obce)
            LEFT JOIN users u ON u.username = c.owner_username
            LEFT JOIN (
                SELECT kod_obce,
                       max(sent_at) FILTER (WHERE direction='Odesláno') AS last_sent,
                       max(sent_at) FILTER (
                           WHERE direction='Přijato'
                             AND lower(coalesce(sender,'')) NOT LIKE '%mailer-daemon%'
                             AND lower(coalesce(sender,'')) NOT LIKE '%no-reply%'
                             AND lower(coalesce(sender,'')) NOT LIKE '%noreply%'
                             AND lower(coalesce(subject,'')) NOT LIKE '%automatická odpověď%'
                             AND lower(coalesce(subject,'')) NOT LIKE '%automatic reply%'
                       ) AS last_received
                FROM crm_emails GROUP BY kod_obce
            ) em USING (kod_obce)
            WHERE o.latitude IS NOT NULL AND o.longitude IS NOT NULL
            """
        ).fetchall()

        results = []
        for row in rows:
            km = vzdalenost(latitude, longitude, row[4], row[5])
            if km <= active_radius:
                last_sent, last_received = row[13], row[14]
                response_state, waiting_days = response_status(last_sent, last_received)
                results.append(
                    {
                        "Karta": False, "Vybrat": False, "Kód": row[0],
                        "Obec": row[1],
                        "_Název obce": row[1],
                        "Okres": row[2] or "",
                        "Kraj": row[3] or "", "Vzdálenost (km)": round(km, 2),
                        "Stav CRM": row[10], "Osloveno": row[11], "Obchodník": row[12],
                        "Odpověď": response_state, "Čekáme dní": waiting_days,
                        "Poslední odpověď": last_received,
                        "Web": row[6] or "", "E-mail": row[7] or "",
                        "Telefon": row[8] or "", "IČO": row[9] or "",
                    }
                )

        columns = ["Karta", "Vybrat", "Kód", "Obec", "_Název obce", "Okres", "Kraj", "Vzdálenost (km)",
                   "Stav CRM", "Osloveno", "Odpověď", "Čekáme dní", "Poslední odpověď",
                   "Obchodník", "Web", "E-mail", "Telefon", "IČO"]
        df = pd.DataFrame(results, columns=columns).sort_values("Vzdálenost (km)")
        response_filter = st.multiselect(
            "Filtrovat podle odpovědi", RESPONSE_STATES, default=RESPONSE_STATES
        )
        df = df[df["Odpověď"].isin(response_filter)]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🏘️ Obcí", len(df))
        c2.metric("🌐 Webů", int(df["Web"].ne("").sum()))
        c3.metric("📧 E-mailů", int(df["E-mail"].ne("").sum()))
        c4.metric("☎️ Telefonů", int(df["Telefon"].ne("").sum()))

        active_users = conn.execute(
            "SELECT username, display_name FROM users WHERE active ORDER BY display_name"
        ).fetchall()
        inline_user_labels = {"—": ""} | {
            f"{display_name} ({username})": username for username, display_name in active_users
        }

        edited_df = st.data_editor(
            df, hide_index=True, width="stretch", key="search_results_editor",
            disabled=["Kód", "Obec", "Vzdálenost (km)", "Odpověď", "Čekáme dní", "Poslední odpověď"],
            column_config={
                "_Název obce": None,
                "Web": st.column_config.LinkColumn("Web"),
                "Obec": st.column_config.TextColumn(
                    "Obec", pinned=True, disabled=True,
                ),
                "Karta": st.column_config.CheckboxColumn(
                    "Otevřít kartu", pinned=True,
                    help="Zaškrtnutím otevřete kartu v tomto okně bez nového přihlášení."
                ),
                "Vybrat": st.column_config.CheckboxColumn(
                    "Vybrat", help="Zařadit obec do rozesílky"
                ),
                "Stav CRM": st.column_config.SelectboxColumn(
                    "Stav CRM", options=STATUSES, required=True
                ),
                "Osloveno": st.column_config.DateColumn(
                    "Osloveno", help="Datum, kdy jste obci poslali e-mail", format="DD.MM.YYYY"
                ),
                "Odpověď": st.column_config.TextColumn("Odpověď", pinned=True),
                "Čekáme dní": st.column_config.NumberColumn("Čekáme dní", format="%d"),
                "Poslední odpověď": st.column_config.DatetimeColumn(
                    "Poslední odpověď", format="DD.MM.YYYY HH:mm"
                ),
                "Obchodník": st.column_config.SelectboxColumn(
                    "Obchodník", options=list(inline_user_labels), required=True
                ),
            },
        )
        changes = 0
        original_rows = df.set_index("Kód").to_dict("index")
        for _, edited_row in edited_df.iterrows():
            code = int(edited_row["Kód"])
            original = original_rows[code]
            value = edited_row["Osloveno"]
            new_date = None if pd.isna(value) else pd.Timestamp(value).date()
            old_value = original["Osloveno"]
            old_date = None if pd.isna(old_value) else pd.Timestamp(old_value).date()
            if new_date != old_date:
                changes += int(
                    set_contacted_on(conn, code, new_date, st.session_state.username)
                )
            municipality_columns = ["Okres", "Kraj", "Web", "E-mail", "Telefon", "IČO"]
            if any(str(edited_row[col] or "") != str(original[col] or "") for col in municipality_columns):
                update_municipality(
                    conn, code, str(edited_row["_Název obce"] or ""),
                    *(str(edited_row[col] or "") for col in municipality_columns)
                )
                changes += 1
            if (
                edited_row["Stav CRM"] != original["Stav CRM"]
                or edited_row["Obchodník"] != original["Obchodník"]
            ):
                update_inline_crm(
                    conn, code, edited_row["Stav CRM"],
                    inline_user_labels.get(edited_row["Obchodník"], ""),
                )
                changes += 1
        if changes:
            st.toast(f"Změny byly automaticky uloženy ({changes}×).", icon="✅")
            st.rerun()
        # A rising checkbox edge opens the dialog in the current Streamlit
        # session. Keeping the checked value does not reopen it on later reruns.
        current_card_codes = {
            int(value) for value in edited_df.loc[edited_df["Karta"], "Kód"].tolist()
        }
        previous_card_codes = set(st.session_state.get("open_card_checkboxes", []))
        newly_opened = current_card_codes - previous_card_codes
        st.session_state.open_card_checkboxes = sorted(current_card_codes)
        if newly_opened:
            quick_municipality_card(sorted(newly_opened)[0])
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        st.download_button(
            "📥 Exportovat výsledky do Excelu", buffer.getvalue(), "obce_crm.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        selected = edited_df[edited_df["Vybrat"] & edited_df["E-mail"].ne("")]
        st.markdown("### ✉️ Odeslat nabídku vybraným obcím")
        st.caption(
            f"Vybráno {len(selected)} obcí s e-mailem. Každá zpráva bude odeslána "
            f"samostatně; maximum je {MAX_BATCH_SIZE} zpráv v jedné dávce."
        )
        skip_contacted = st.checkbox("Přeskočit již oslovené obce", value=True)
        if skip_contacted:
            selected = selected[selected["Osloveno"].isna()]
            st.caption(f"Po odebrání již oslovených zbývá {len(selected)} příjemců.")

        with st.form("bulk_email_form"):
            subject_template = st.text_input(
                "Předmět", placeholder="Nabídka pro obec {{obec}}"
            )
            body_template = st.text_area(
                "Text zprávy",
                placeholder="Dobrý den,\n\nobracím se na obec {{obec}}…",
                height=220,
                help="Lze použít {{obec}}, {{okres}}, {{kraj}} a {{email}}.",
            )
            sender_address = st.text_input(
                "Odesílatel", value=os.getenv("SEZNAM_EMAIL_ADDRESS", "program.obce@email.cz")
            )
            sender_password = st.text_input(
                "Heslo k e-mailu", value=os.getenv("SEZNAM_EMAIL_PASSWORD", ""), type="password"
            )
            confirm_send = st.checkbox(
                "Potvrzuji odeslání samostatné zprávy každému vybranému příjemci"
            )
            send_clicked = st.form_submit_button(
                f"📨 Odeslat {len(selected)} samostatných e-mailů", type="primary"
            )

        if send_clicked:
            if not confirm_send:
                st.error("Před odesláním potvrďte rozesílku.")
            elif not sender_password:
                st.error("Zadejte heslo k e-mailu nebo ho nastavte v .env.")
            elif len(selected) > MAX_BATCH_SIZE:
                st.error(f"Vyberte nejvýše {MAX_BATCH_SIZE} obcí.")
            else:
                recipient_rows = [
                    {
                        "code": int(row["Kód"]), "name": str(row["_Název obce"]),
                        "district": str(row["Okres"]), "region": str(row["Kraj"]),
                        "email": str(row["E-mail"]),
                    }
                    for _, row in selected.iterrows()
                ]
                with st.spinner("Odesílám každé obci samostatnou zprávu…"):
                    try:
                        send_result = send_individual_messages(
                            conn, sender_address.strip(), sender_password, recipient_rows,
                            subject_template, body_template, st.session_state.username,
                        )
                    except EmailSendError as exc:
                        st.error(str(exc))
                    else:
                        st.success(
                            f"Odesláno: {send_result['sent']}, chyby: {send_result['failed']}."
                        )
                        st.dataframe(
                            pd.DataFrame(send_result["details"], columns=["Obec", "E-mail", "Výsledek"]),
                            hide_index=True, width="stretch",
                        )


if active_page == "📊 Pipeline":
    summary = conn.execute(
        """
        SELECT s.status, count(c.kod_obce) AS pocet
        FROM (SELECT unnest(?) AS status) s
        LEFT JOIN crm_records c ON c.status = s.status
        GROUP BY s.status
        """,
        [STATUSES],
    ).fetchdf()
    metric_cols = st.columns(len(STATUSES))
    for col, status in zip(metric_cols, STATUSES):
        count = int(summary.loc[summary["status"] == status, "pocet"].iloc[0])
        col.metric(status, count)

    status_filter = st.multiselect("Zobrazit stavy", STATUSES, default=STATUSES)
    if status_filter:
        placeholders = ", ".join("?" for _ in status_filter)
        pipeline = conn.execute(
            f"""
            SELECT o.kod_obce AS "Kód", o.nazev AS "Obec", o.okres AS "Okres",
                   c.status AS "Stav", c.priority AS "Priorita",
                   coalesce(u.display_name, c.owner_username, '') AS "Obchodník",
                   c.next_contact AS "Další kontakt", o.email AS "E-mail",
                   o.telefon AS "Telefon", c.updated_at AS "Aktualizováno"
            FROM crm_records c
            JOIN obce o USING (kod_obce)
            LEFT JOIN users u ON u.username = c.owner_username
            WHERE c.status IN ({placeholders})
            ORDER BY c.next_contact NULLS LAST, c.updated_at DESC
            """,
            status_filter,
        ).fetchdf()
        st.dataframe(pipeline, hide_index=True, width="stretch")
    else:
        st.info("Vyberte alespoň jeden stav.")


if active_page == "🏢 Detail obce":
    municipalities = conn.execute(
        """
        SELECT kod_obce, nazev, coalesce(okres, ''), coalesce(kraj, '')
        FROM obce ORDER BY nazev, okres, kod_obce
        """
    ).fetchall()
    municipality_map = {
        f"{name} — {district or region or 'bez okresu'} [{code}]": code
        for code, name, district, region in municipalities
    }
    selected_label = st.selectbox("Vyberte obec", municipality_map.keys())
    kod_obce = municipality_map[selected_label]

    obec = conn.execute(
        """
        SELECT o.nazev, o.okres, o.kraj, o.web, o.email, o.telefon, o.ico,
               coalesce(c.status, 'Nová'), coalesce(c.priority, 'Střední'),
               coalesce(c.owner_username, ''), c.next_contact, coalesce(c.note, '')
        FROM obce o LEFT JOIN crm_records c USING (kod_obce)
        WHERE o.kod_obce = ?
        """,
        [kod_obce],
    ).fetchone()
    users = conn.execute(
        "SELECT username, display_name FROM users WHERE active ORDER BY display_name"
    ).fetchall()
    user_labels = {"— Nepřiřazeno —": ""} | {f"{name} ({username})": username for username, name in users}

    st.subheader(obec[0])
    info1, info2, info3, info4 = st.columns(4)
    info1.write(f"**Okres:** {obec[1] or '—'}")
    info2.write(f"**Kraj:** {obec[2] or '—'}")
    info3.write(f"**E-mail:** {obec[4] or '—'}")
    info4.write(f"**Telefon:** {obec[5] or '—'}")
    if obec[3]:
        st.link_button("🌐 Otevřít web obce", obec[3])

    with st.form("crm_record_form"):
        a, b, c = st.columns(3)
        status = a.selectbox("Stav", STATUSES, index=STATUSES.index(obec[7]))
        priority = b.selectbox("Priorita", PRIORITIES, index=PRIORITIES.index(obec[8]))
        owner_keys = list(user_labels)
        current_owner = next((label for label, value in user_labels.items() if value == obec[9]), owner_keys[0])
        owner_label = c.selectbox("Obchodník", owner_keys, index=owner_keys.index(current_owner))
        has_next_contact = st.checkbox("Naplánovat další kontakt", value=obec[10] is not None)
        next_contact = st.date_input("Datum dalšího kontaktu", value=obec[10] or "today", disabled=not has_next_contact)
        note = st.text_area("Souhrnná poznámka", value=obec[11], height=100)
        if st.form_submit_button("💾 Uložit CRM údaje", type="primary"):
            save_record(conn, kod_obce, status, priority, user_labels[owner_label],
                        next_contact if has_next_contact else None, note)
            st.success("CRM údaje byly uloženy.")
            st.rerun()

    activity_col, task_col = st.columns(2)
    with activity_col:
        st.markdown("#### Přidat aktivitu")
        with st.form("activity_form", clear_on_submit=True):
            activity_type = st.selectbox("Typ aktivity", ACTIVITY_TYPES)
            subject = st.text_input("Předmět")
            detail = st.text_area("Detail", height=90)
            if st.form_submit_button("➕ Přidat aktivitu"):
                if not subject.strip():
                    st.error("Vyplňte předmět aktivity.")
                else:
                    add_activity(conn, kod_obce, activity_type, subject.strip(), detail,
                                 st.session_state.username)
                    st.success("Aktivita byla přidána.")
                    st.rerun()
    with task_col:
        st.markdown("#### Přidat úkol")
        with st.form("task_form", clear_on_submit=True):
            task_title = st.text_input("Název úkolu")
            due_date = st.date_input("Termín", value="today")
            assigned_label = st.selectbox("Přiřadit", list(user_labels), key="task_owner")
            if st.form_submit_button("➕ Přidat úkol"):
                if not task_title.strip():
                    st.error("Vyplňte název úkolu.")
                else:
                    add_task(conn, kod_obce, task_title.strip(), due_date,
                             user_labels[assigned_label], st.session_state.username)
                    st.success("Úkol byl přidán.")
                    st.rerun()

    st.markdown("#### Historie aktivit")
    activities = conn.execute(
        """
        SELECT activity_type AS "Typ", subject AS "Předmět", detail AS "Detail",
               created_by AS "Vytvořil", created_at AS "Datum"
        FROM crm_activities WHERE kod_obce = ? ORDER BY created_at DESC
        """,
        [kod_obce],
    ).fetchdf()
    if activities.empty:
        st.caption("Zatím bez aktivit.")
    else:
        st.dataframe(activities, hide_index=True, width="stretch")

    st.markdown("#### E-mailová komunikace")
    messages = conn.execute("""
        SELECT direction,sender,recipients,subject,body_text,attachments,sent_at
        FROM crm_emails WHERE kod_obce=? ORDER BY sent_at DESC
    """, [kod_obce]).fetchall()
    if not messages:
        st.caption("Zatím nebyla synchronizována žádná komunikace s touto obcí.")
    else:
        for direction, sender, recipients, subject, body, attachments, sent_at in messages:
            icon = "📤" if direction == "Odesláno" else "📥"
            timestamp = sent_at.strftime("%d.%m.%Y %H:%M") if sent_at else "bez data"
            with st.expander(f"{icon} {timestamp} · {subject}"):
                st.caption(f"Od: {sender or '—'}  |  Komu: {recipients or '—'}")
                st.text(body) if body else st.caption("Zpráva nemá textový obsah.")
                attachment_names = json.loads(attachments or "[]")
                if attachment_names:
                    st.caption("Přílohy: " + ", ".join(attachment_names))

    render_municipality_documents(conn, kod_obce, "detail_document")


if active_page == "✅ Úkoly":
    mine_only = st.checkbox("Pouze moje úkoly", value=True)
    show_completed = st.checkbox("Zobrazit dokončené", value=False)
    clauses, params = ["1 = 1"], []
    if mine_only:
        clauses.append("t.assigned_to = ?")
        params.append(st.session_state.username)
    if not show_completed:
        clauses.append("t.completed = FALSE")
    tasks = conn.execute(
        f"""
        SELECT t.id, o.nazev, t.title, t.due_date, t.assigned_to, t.completed,
               CASE WHEN NOT t.completed AND t.due_date < CURRENT_DATE THEN 'Po termínu' ELSE '' END
        FROM crm_tasks t JOIN obce o USING (kod_obce)
        WHERE {' AND '.join(clauses)}
        ORDER BY t.completed, t.due_date NULLS LAST, t.created_at DESC
        """,
        params,
    ).fetchall()
    if not tasks:
        st.info("Žádné úkoly pro zvolený filtr.")
    else:
        for task_id, name, title, due, assigned, completed, warning in tasks:
            cols = st.columns([1, 3, 2, 2, 1])
            cols[0].write("🔴" if warning else ("✅" if completed else "🟡"))
            cols[1].write(f"**{title}**\n\n{name}")
            cols[2].write(f"Termín: {due or '—'}")
            cols[3].write(f"Přiřazeno: {assigned or '—'}")
            label = "Obnovit" if completed else "Dokončit"
            if cols[4].button(label, key=f"task_{task_id}"):
                set_task_completed(conn, task_id, not completed)
                st.rerun()
            st.divider()


if active_page == "📧 E-mail":
    st.subheader("Synchronizace e-mailové komunikace")
    st.caption(
        "CRM načte přijaté i odeslané zprávy a porovná jejich adresy s e-maily obcí. "
        "Ukládá text komunikace a názvy příloh; samotné soubory příloh nestahuje."
    )
    configured_address = configured_value(
        "SEZNAM_EMAIL_ADDRESS", "program.obce@email.cz"
    )
    configured_password = configured_value("SEZNAM_EMAIL_PASSWORD")
    email_address = st.text_input(
        "E-mailová adresa", value=configured_address, key="email_address_input"
    )
    email_password = st.text_input(
        "Heslo pro poštovní aplikaci",
        value=configured_password,
        type="password",
        key="email_password_input",
        help="Při dvoufázovém ověření použijte samostatné heslo pro poštovní program.",
    )
    if st.session_state.get("email_last_auto_sync"):
        st.caption(
            "Poslední automatická kontrola: "
            + st.session_state.email_last_auto_sync
        )
    if st.button("🔄 Synchronizovat e-mailovou komunikaci", type="primary"):
        if not email_password:
            st.error("Zadejte heslo nebo ho nastavte v souboru .env.")
        else:
            with st.spinner("Načítám přijaté a odeslané zprávy ze Seznamu…"):
                try:
                    sync_stats = sync_mailbox(
                        conn, email_address.strip(), email_password,
                        st.session_state.username,
                    )
                except EmailSyncError as exc:
                    st.error(str(exc))
                else:
                    st.success(
                        f"Hotovo: zkontrolováno {sync_stats['checked']} zpráv, "
                        f"nalezeno {sync_stats['matched']} shod a přidáno "
                        f"{sync_stats['new']} zpráv "
                        f"({sync_stats['sent']} odeslaných, {sync_stats['received']} přijatých)."
                    )

    st.markdown("#### Nastavení automatické synchronizace")
    st.code(
        'SEZNAM_EMAIL_ADDRESS = "program.obce@email.cz"\n'
        'SEZNAM_EMAIL_PASSWORD = "sem_zadejte_skutecne_heslo"',
        language="toml",
    )
    st.caption(
        "Tyto dva řádky vložte do souboru .env v hlavní složce projektu. "
        "Na Streamlit Cloud vložte stejné hodnoty do Settings → Secrets. "
        "Kontrola probíhá každých 10 minut, dokud je relace aplikace aktivní."
    )


if active_page == "⚡ Innogy":
    st.subheader("Import smluv Innogy iSales")
    st.caption(
        "Nahrajte Excel export ContractListExport.xlsx. Obce se párují primárně "
        "podle IČO; opakovaný import stejné smlouvy ji aktualizuje."
    )
    innogy_file = st.file_uploader("Excel export z iSales", type=["xlsx"], key="innogy_upload")
    if innogy_file is not None and st.button("⚡ Importovat smlouvy", type="primary"):
        try:
            result = import_contracts(
                conn, innogy_file, innogy_file.name, st.session_state.username
            )
        except InnogyImportError as exc:
            st.error(str(exc))
        else:
            st.success(
                f"Importováno {result['total']} řádků: {result['matched']} spárováno "
                f"s obcemi, {result['unmatched']} nespárováno."
            )

    import_history = conn.execute("""
        SELECT file_name AS "Soubor",imported_at AS "Importováno",
               total_rows AS "Řádků",matched_rows AS "Spárováno",
               unmatched_rows AS "Nespárováno",imported_by AS "Uživatel"
        FROM innogy_imports ORDER BY imported_at DESC LIMIT 20
    """).fetchdf()
    if not import_history.empty:
        st.markdown("#### Historie importů")
        st.dataframe(import_history, hide_index=True, width="stretch")

    matched_contracts = conn.execute("""
        SELECT o.nazev AS "Obec",c.ico AS "IČO",c.commodity AS "Komodita",
               c.ean_eic AS "EAN/EIC",c.product AS "Produkt",
               c.opportunity_status AS "Stav",c.signed_at AS "Podepsáno",
               c.verification_status AS "Ověření",c.seller_name AS "Prodejce"
        FROM innogy_contracts c JOIN obce o USING (kod_obce)
        ORDER BY c.updated_at DESC
    """).fetchdf()
    st.markdown("#### Spárované obecní smlouvy")
    if matched_contracts.empty:
        st.caption("Zatím nebyly importovány žádné spárované obecní smlouvy.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Smluv", len(matched_contracts))
        m2.metric("Obcí", matched_contracts["Obec"].nunique())
        m3.metric("Odběrných míst", matched_contracts["EAN/EIC"].nunique())
        st.dataframe(matched_contracts, hide_index=True, width="stretch")

    unmatched_contracts = conn.execute("""
        SELECT customer_name AS "Zákazník",ico AS "IČO",ean_eic AS "EAN/EIC",
               commodity AS "Komodita",product AS "Produkt",seller_name AS "Prodejce",
               updated_at AS "Poslední import"
        FROM innogy_contracts WHERE kod_obce IS NULL
        ORDER BY updated_at DESC
    """).fetchdf()
    st.markdown("#### Nespárované záznamy")
    if unmatched_contracts.empty:
        st.caption("Všechny importované obecní smlouvy jsou spárované.")
    else:
        st.dataframe(unmatched_contracts, hide_index=True, width="stretch")


if active_page == "💡 Kalkulace":
    render_energy_calculator(
        conn, st.session_state.username, st.session_state.role
    )

conn.close()
