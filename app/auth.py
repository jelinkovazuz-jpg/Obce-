import streamlit as st
import duckdb
import bcrypt

DB = "data/obce.duckdb"


def login():

    conn = duckdb.connect(DB)

    st.markdown("# 🔐 Přihlášení")
    st.markdown("## 🏛️ Města a obce ČR")

    username = st.text_input("Uživatelské jméno")
    password = st.text_input("Heslo", type="password")

    if st.button("Přihlásit", use_container_width=True):

        user = conn.execute("""
            SELECT
                username,
                display_name,
                password_hash,
                role
            FROM users
            WHERE username = ?
            AND active = TRUE
        """, [username]).fetchone()

        if user is None:
            st.error("Neplatné uživatelské jméno nebo heslo.")
            return

        if bcrypt.checkpw(
            password.encode("utf-8"),
            user[2].encode("utf-8")
        ):

            st.session_state.logged = True
            st.session_state.username = user[0]
            st.session_state.display_name = user[1]
            st.session_state.role = user[3]

            st.rerun()

        st.error("Neplatné uživatelské jméno nebo heslo.")


def logout():

    st.session_state.clear()

    st.rerun()