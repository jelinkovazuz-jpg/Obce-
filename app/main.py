import streamlit as st
import duckdb
import pandas as pd
from io import BytesIO
from geopy.geocoders import Nominatim
from distance import vzdalenost
from auth import login, logout
from pathlib import Path

st.set_page_config(page_title="Města a obce ČR", page_icon="🏛️", layout="wide")
# Přihlášení
if "logged" not in st.session_state:
    st.session_state.logged = False

if not st.session_state.logged:
    login()
    st.stop()
BASE_DIR = Path(__file__).resolve().parent.parent
conn = duckdb.connect(str(BASE_DIR / "data" / "obce.duckdb"))
geolocator = Nominatim(user_agent="obce_app")

st.markdown("""
<style>
.main h1 {
    color:#0F4C81;
}

div[data-testid="stMetric"]{
    border:1px solid #e8e8e8;
    border-radius:12px;
    padding:12px;
    background:#fafafa;
}
</style>
""", unsafe_allow_html=True)

st.markdown("# 🏛️ Města a obce ČR")
st.markdown("### Vyhledávání obcí, kontaktů a obecních úřadů podle vzdálenosti")
st.divider()

with st.sidebar:

    st.success(f"👤 {st.session_state.display_name}")
    st.caption(f"Role: {st.session_state.role}")

    if st.button("🚪 Odhlásit", use_container_width=True):
        logout()

    st.divider()

    st.header("⚙️ Nastavení")

    mesto = st.text_input(
        "Výchozí obec",
        value="Heřmanův Městec"
    )

    polomer = st.slider(
        "Poloměr (km)",
        1,
        100,
        20
    )

    hledat = st.button(
        "🔎 Vyhledat",
        use_container_width=True
    )

if hledat:
    location = geolocator.geocode(f"{mesto}, Česká republika")
    if location is None:
        st.error("Obec nebyla nalezena.")
        st.stop()

    obce = conn.execute('''
        SELECT nazev, latitude, longitude, web, email, telefon, ico
        FROM obce
        WHERE latitude IS NOT NULL
    ''').fetchall()

    vysledky=[]

    for obec in obce:
        km=vzdalenost(location.latitude, location.longitude, obec[1], obec[2])
        if km<=polomer:
            vysledky.append({
                "Obec":obec[0],
                "Vzdálenost (km)":round(km,2),
                "🌐 Web":obec[3] or "",
                "📧 E-mail":obec[4] or "",
                "☎️ Telefon":obec[5] or "",
                "🏢 IČO":obec[6] or ""
            })

    df=pd.DataFrame(vysledky)
    if not df.empty:
        df=df.sort_values("Vzdálenost (km)")

    c1,c2,c3,c4=st.columns(4)
    c1.metric("🏘️ Obcí",len(df))
    c2.metric("🌐 Webů",(df["🌐 Web"]!="").sum())
    c3.metric("📧 E-mailů",(df["📧 E-mail"]!="").sum())
    c4.metric("☎️ Telefonů",(df["☎️ Telefon"]!="").sum())

    st.divider()
    st.subheader("📋 Výsledky")

    st.data_editor(
        df,
        hide_index=True,
        disabled=True,
        use_container_width=True,
        column_config={
            "🌐 Web": st.column_config.LinkColumn("🌐 Web")
        }
    )

    buffer=BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer,index=False)

    st.download_button(
        "📥 Exportovat výsledky do Excelu",
        data=buffer.getvalue(),
        file_name="obce.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )
