"""Streamlit application entrypoint for the unified Todo Agent."""

from __future__ import annotations

import streamlit as st

from database.schema import initialize_database
from database.m3 import recover_stale_reminders
from ops.observability import configure_logging
from ops.runtime import ConfigurationError, load_config
from ui.runtime import normalize_user_id
from ui.views import render_workbench


configure_logging()
st.set_page_config(page_title="Todo Agent", page_icon="T", layout="wide")


@st.cache_resource(ttl=60, show_spinner=False)
def _prepare_runtime(database_path: str) -> bool:
    """Run startup database work once per process-minute, not on every rerun."""

    initialize_database(database_path)
    recover_stale_reminders()
    return True


try:
    CONFIG = load_config()
    CONFIG.ensure_directories()
    _prepare_runtime(str(CONFIG.database_path))
except ConfigurationError as error:
    st.error(f"Configuration error: {error}")
    st.stop()

if "user_id" not in st.session_state:
    st.markdown(
        """
        <style>
        [data-testid="stAppViewContainer"] { background: #fbfcfd; }
        .block-container { max-width: 620px; padding-top: 8rem; }
        .todo-login-brand { text-align: center; color: #17202a; }
        .todo-login-brand .todo-brand-mark { display: grid; place-items: center; width: 3rem; height: 3rem; margin: 0 auto 1rem; border-radius: 8px; background: #17202a; color: white; font-weight: 800; }
        .todo-login-brand h1 { margin: 0; font-size: 2rem; }
        .todo-login-brand p { color: #75808b; }
        </style>
        <div class="todo-login-brand">
            <div class="todo-brand-mark">T</div>
            <h1>Todo Agent</h1>
            <p>用一句话安排任务、计划和生活。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("login"):
        username = st.text_input("User name")
        submitted = st.form_submit_button("Continue")
    if submitted:
        user_id = normalize_user_id(username)
        if not user_id:
            st.error("请输入有效的用户名。")
        else:
            st.session_state["user_id"] = user_id
            st.session_state["conversation_history"] = []
            st.rerun()
    st.stop()

USER_ID = st.session_state["user_id"]
render_workbench(USER_ID, CONFIG)
