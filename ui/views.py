"""Streamlit view layer; business operations go through ``ui.runtime``."""

from __future__ import annotations

from datetime import date, timedelta

import altair as alt
import streamlit as st

from ops.runtime import RuntimeConfig
from ui.runtime import (
    conversation_options,
    data_or_error,
    dispatch,
    ensure_conversation,
    load_conversation_history,
    request_id,
    run_agent,
)


def inject_workspace_style() -> None:
    """Apply the restrained conversation-workspace visual system."""

    st.markdown(
        """
        <style>
        :root {
            --todo-ink: #17202a;
            --todo-muted: #75808b;
            --todo-line: #e5e9ed;
            --todo-soft: #f5f7f8;
            --todo-accent: #1f6feb;
        }
        [data-testid="stAppViewContainer"] {
            background: #fbfcfd;
        }
        [data-testid="stHeader"] {
            background: transparent;
        }
        .block-container {
            max-width: 980px;
            padding: 1.25rem 1rem 3rem;
        }
        .todo-brand {
            display: flex;
            align-items: center;
            gap: .7rem;
            color: var(--todo-ink);
            font-size: 1.15rem;
            font-weight: 700;
            letter-spacing: 0;
        }
        .todo-brand-mark, .todo-empty-mark {
            display: grid;
            place-items: center;
            width: 2rem;
            height: 2rem;
            border-radius: 8px;
            color: white;
            background: var(--todo-ink);
            font-weight: 800;
        }
        .todo-brand-subtitle {
            color: var(--todo-muted);
            font-size: .8rem;
            font-weight: 400;
        }
        .todo-user-label {
            color: var(--todo-muted);
            font-size: .78rem;
            text-align: right;
        }
        .todo-topline {
            height: 1px;
            margin: .9rem 0 1.25rem;
            background: var(--todo-line);
        }
        .todo-empty-state {
            padding: 4.25rem 1rem 3rem;
            text-align: center;
        }
        .todo-empty-state .todo-empty-mark {
            width: 2.75rem;
            height: 2.75rem;
            margin: 0 auto 1rem;
            font-size: 1.2rem;
        }
        .todo-empty-state h2 {
            margin: 0;
            color: var(--todo-ink);
            font-size: 1.65rem;
            letter-spacing: 0;
        }
        .todo-empty-state p {
            margin: .7rem auto 0;
            max-width: 34rem;
            color: var(--todo-muted);
            font-size: .95rem;
        }
        [data-testid="stChatMessage"] {
            border: 1px solid var(--todo-line);
            border-radius: 12px;
            background: white;
            padding: .8rem 1rem;
            margin: .65rem 0;
            max-width: 82%;
        }
        [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
            margin-left: auto;
            background: #edf5ff;
            border-color: #d6e6fb;
        }
        [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
            margin-right: auto;
            background: #ffffff;
        }
        [data-testid="stChatMessage"] p {
            line-height: 1.65;
        }
        [data-testid="stChatInput"] {
            position: static !important;
            width: 100% !important;
            transform: none !important;
            margin: .75rem 0 0;
            border: 1px solid #c9d9ee;
            border-radius: 16px;
            background: rgba(255, 255, 255, .97);
            box-shadow: 0 8px 26px rgba(24, 39, 58, .08);
        }
        [data-testid="stChatInput"] textarea {
            min-height: 2.8rem;
            border: 0;
            background: transparent;
        }
        .todo-entry-label {
            margin: 1.5rem 0 .45rem;
            color: var(--todo-muted);
            font-size: .76rem;
            font-weight: 600;
        }
        .todo-entry-label + div [data-testid="stHorizontalBlock"] {
            gap: .45rem;
        }
        .todo-entry-label + div button {
            min-height: 2.35rem;
            border: 1px solid var(--todo-line);
            border-radius: 10px;
            background: white;
            color: #3f4a55;
            font-size: .86rem;
        }
        .todo-panel {
            margin-top: 1rem;
            padding: 1rem;
            border: 1px solid var(--todo-line);
            border-radius: 12px;
            background: white;
        }
        @media (max-width: 700px) {
            .block-container {
                padding: .8rem .7rem 2rem;
            }
            .todo-empty-state {
                padding: 2.5rem .5rem 2rem;
            }
            .todo-empty-state h2 {
                font-size: 1.35rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _stream_text(value: str, *, chunk_size: int = 12):
    """Yield short text chunks so completed model output renders progressively."""

    text = str(value or "")
    for index in range(0, len(text), chunk_size):
        yield text[index:index + chunk_size]


def _tool_summary(message) -> str:
    """Return a bounded human-readable summary for a persisted tool message."""

    content = message.content
    if isinstance(content, dict):
        if content.get("ok") is False:
            error = content.get("error") or {}
            return f"执行未完成：{error.get('message', '工具返回错误')}"
        data = content.get("data")
        if isinstance(data, dict):
            for key in ("title", "name", "message", "status", "count"):
                if key in data:
                    return f"已完成：{data[key]}"
        if isinstance(data, list):
            return f"已完成，返回 {len(data)} 条结果"
        return "工具执行已完成"
    return "工具执行已完成"


def _queue_prompt() -> None:
    """Store submitted text so it can render above the composer on rerun."""

    prompt = str(st.session_state.get("agent_chat_input", "")).strip()
    if prompt:
        st.session_state["pending_agent_prompt"] = prompt


def render_assistant(user_id: str, config: RuntimeConfig) -> None:
    """Render the primary conversation surface and native chat composer."""

    if not config.groq_api_key:
        st.info("配置模型密钥后即可使用自然语言交互，底部功能入口仍可直接使用。")

    history = st.session_state.get("conversation_history", [])
    if not history:
        st.markdown(
            """
            <section class="todo-empty-state">
                <div class="todo-empty-mark">T</div>
                <h2>今天想处理什么？</h2>
                <p>告诉 Agent 你的任务、计划、提醒或账务，它会帮你完成对应操作。</p>
            </section>
            """,
            unsafe_allow_html=True,
        )

    for message in history:
        if message.role in {"user", "assistant"} and message.content:
            with st.chat_message(message.role):
                st.markdown(str(message.content))
        elif message.role == "assistant" and message.tool_calls:
            with st.expander("Agent 正在调用工具", expanded=False):
                for call in message.tool_calls:
                    st.caption(f"已请求：{call.name}")
        elif message.role == "tool":
            with st.expander(f"工具结果：{message.name or '已完成'}", expanded=False):
                st.caption(_tool_summary(message))

    pending_prompt = st.session_state.pop("pending_agent_prompt", None)
    if pending_prompt:
        with st.chat_message("user"):
            st.markdown(pending_prompt)
        with st.chat_message("assistant"):
            with st.spinner("Agent 正在思考..."):
                response, error = run_agent(user_id, config, pending_prompt)
            if error is not None:
                st.error(error)
            elif response:
                st.write_stream(_stream_text(response))
        st.markdown(
            """
            <script>
            const frame = window.parent;
            frame.setTimeout(() => frame.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'}), 50);
            </script>
            """,
            unsafe_allow_html=True,
        )

    st.markdown('<div class="todo-chat-end" id="todo-chat-end"></div>', unsafe_allow_html=True)
    with st.container():
        st.chat_input(
            "发消息或按 Enter 说话...",
            key="agent_chat_input",
            on_submit=_queue_prompt,
        )


def _set_active_panel(panel: str | None) -> None:
    st.session_state["active_panel"] = panel


def render_entry_bar() -> str | None:
    """Render navigation entries and return the currently selected page."""

    active = st.session_state.get("active_panel")
    valid_pages = {
        None,
        "tasks",
        "goals",
        "reminders",
        "finance",
        "analytics",
        "operations",
    }
    if active not in valid_pages:
        active = None
        st.session_state.pop("active_panel", None)
    st.markdown('<div class="todo-entry-label">快捷入口</div>', unsafe_allow_html=True)
    entries = (
        ("对话", None),
        ("任务", "tasks"),
        ("目标", "goals"),
        ("提醒", "reminders"),
        ("财务", "finance"),
        ("图表", "analytics"),
        ("运维", "operations"),
    )
    columns = st.columns([1.2, 1, 1, 1, 1, 1, 1])
    for column, (label, panel) in zip(columns, entries):
        with column:
            st.button(
                label,
                key=f"entry_{panel or 'conversation'}",
                type="primary" if active == panel else "secondary",
                use_container_width=True,
                on_click=_set_active_panel,
                args=(panel,),
            )

    return active


def render_active_page(user_id: str, page: str) -> None:
    """Render exactly one feature page selected from the entry bar."""

    page_renderers = {
        "tasks": render_tasks,
        "goals": render_goals,
        "reminders": render_reminders_panel,
        "finance": render_finance,
        "analytics": render_analytics,
        "operations": render_operations,
    }
    renderer = page_renderers.get(page)
    if renderer is None:
        return
    renderer(user_id)


def render_tasks(user_id: str) -> None:
    st.subheader("任务")
    tasks = data_or_error(dispatch(user_id, "todo_list_tasks", {"limit": 500})) or []
    if not tasks:
        st.info("暂时还没有任务。")
    else:
        st.dataframe(tasks, use_container_width=True, hide_index=True)

    create_key = "show_task_create"
    if st.button("创建任务", key="open_task_create", use_container_width=True):
        st.session_state[create_key] = not st.session_state.get(create_key, False)
    if st.session_state.get(create_key, False):
        with st.form("task_create"):
            title = st.text_input("任务名称")
            description = st.text_area("任务描述")
            planned = st.text_input("计划开始时间（ISO-8601，可选）")
            due = st.text_input("截止时间（ISO-8601，可选）")
            submitted = st.form_submit_button("保存任务")
        if submitted:
            result = dispatch(user_id, "task_create", {
                "title": title,
                "description": description or None,
                "planned_start_at": planned or None,
                "due_at": due or None,
                "source": "ui",
            }, prefix="task-create")
            if data_or_error(result) is not None:
                st.session_state[create_key] = False
                st.success("任务已创建。")
                st.rerun()

    if not tasks:
        return
    labels = {item["id"]: f'{item["id"]}: {item["title"]}' for item in tasks}
    selected = st.selectbox("Task", list(labels), format_func=labels.get)
    selected_task = next(item for item in tasks if item["id"] == selected)
    primary_actions = {
        "pending": ("Start", "start"),
        "in_progress": ("Pause", "pause"),
        "completed": ("Start again", "start"),
    }
    if selected_task["status"] in primary_actions:
        label, action = primary_actions[selected_task["status"]]
        if st.button(label, key="task_primary_action"):
            result = dispatch(user_id, "task_transition", {"task_id": selected, "action": action}, prefix="task")
            if data_or_error(result) is not None:
                messages = {"start": "Task started.", "pause": "Task paused."}
                st.success(messages[action])
    action_columns = st.columns(3)
    with action_columns[0]:
        if st.button("Complete", key="task_complete"):
            result = dispatch(user_id, "task_transition", {"task_id": selected, "action": "complete"}, prefix="task")
            if data_or_error(result) is not None:
                st.success("Execution recorded.")
    with action_columns[1]:
        if st.button("Cancel", key="task_cancel"):
            result = dispatch(user_id, "task_transition", {"task_id": selected, "action": "cancel"}, prefix="task")
            if data_or_error(result) is not None:
                st.success("Task cancelled.")
    with action_columns[2]:
        if st.button("Archive", key="task_archive"):
            result = dispatch(user_id, "task_archive", {"task_id": selected}, prefix="task")
            if data_or_error(result) is not None:
                st.success("Task archived.")

    history_period = st.selectbox("History period", ["day", "week", "custom"], key="history_period")
    reference_date = st.date_input("Reference date", value=date.today(), key="history_reference")
    history_status = st.selectbox("History status", ["all", "started", "completed", "cancelled"], key="history_status")
    history_args = {
        "task_id": selected,
        "period": history_period,
        "reference_at": str(reference_date),
        "status": None if history_status == "all" else history_status,
        "limit": 100,
        "offset": 0,
    }
    if history_period == "custom":
        history_start = st.date_input("History start", value=reference_date, key="history_start")
        history_end = st.date_input("History end", value=reference_date + timedelta(days=1), key="history_end")
        history_args.update({"start_at": str(history_start), "end_at": str(history_end + timedelta(days=1))})
    occurrences = data_or_error(dispatch(user_id, "todo_list_occurrences", history_args))
    if occurrences:
        st.caption("Execution history")
        st.dataframe(occurrences, use_container_width=True, hide_index=True)


def task_options(user_id: str) -> tuple[list[dict], dict[int, str]]:
    tasks = data_or_error(dispatch(user_id, "todo_list_tasks", {"limit": 500})) or []
    return tasks, {item["id"]: f'{item["id"]}: {item["title"]}' for item in tasks}


def render_plans(user_id: str) -> None:
    st.subheader("长期目标与计划")
    plans = data_or_error(dispatch(user_id, "plan_list", {"limit": 100})) or []
    if plans:
        st.dataframe(plans, use_container_width=True, hide_index=True)
    else:
        st.info("暂时还没有长期目标或计划。")

    create_key = "show_plan_create"
    if st.button("创建目标计划", key="open_plan_create", use_container_width=True):
        st.session_state[create_key] = not st.session_state.get(create_key, False)
    if st.session_state.get(create_key, False):
        with st.form("plan_draft"):
            intent = st.text_area("目标描述", key="plan_intent")
            title = st.text_input("计划名称", key="plan_title")
            phase_name = st.text_input("首个阶段", value="初始阶段", key="plan_phase")
            task_titles = st.text_area("阶段任务（每行一项）", key="plan_tasks")
            create = st.form_submit_button("生成计划草案")
    else:
        create = False
        intent = title = phase_name = task_titles = ""
    if create:
        tasks = [
            {"key": f"task-{index}", "title": value.strip()}
            for index, value in enumerate(task_titles.splitlines(), start=1)
            if value.strip()
        ]
        if not tasks and intent.strip():
            tasks = [{"key": "task-1", "title": intent.strip()[:200]}]
        result = dispatch(user_id, "plan_draft", {
            "intent": intent,
            "constraints": {
                "title": title or intent[:200],
                "phases": [{"name": phase_name, "tasks": tasks}],
            },
        }, prefix="plan")
        draft = data_or_error(result)
        if draft is not None:
            st.session_state["plan_draft"] = draft
            st.success("Plan draft created. Review it before applying.")
    draft = st.session_state.get("plan_draft")
    if draft:
        st.caption(f"Draft #{draft['draft_id']} expires at {draft['expires_at']}")
        st.json({
            "title": draft["title"],
            "goal": draft["goal"],
            "phases": draft["phases"],
            "dependencies": draft["dependencies"],
            "warnings": draft["warnings"],
        })
        if draft.get("confirmation_token") and st.button("Confirm and apply plan", key="plan_apply"):
            result = dispatch(user_id, "plan_apply", {
                "draft_id": draft["draft_id"],
                "confirmation_token": draft["confirmation_token"],
            }, prefix="plan-apply")
            applied = data_or_error(result)
            if applied is not None:
                st.session_state.pop("plan_draft", None)
                st.success(f"Plan applied: #{applied['plan_id']}")


def render_goals(user_id: str) -> None:
    """Render created/completed goals before exposing the creation form."""

    st.subheader("目标")
    tasks, labels = task_options(user_id)
    goals = data_or_error(dispatch(user_id, "goal_progress_list", {})) or []
    if goals:
        st.dataframe(goals, use_container_width=True, hide_index=True)
    else:
        st.info("暂时还没有目标。")

    create_key = "show_goal_create"
    if st.button("创建目标", key="open_goal_create", use_container_width=True):
        st.session_state[create_key] = not st.session_state.get(create_key, False)
    if st.session_state.get(create_key, False):
        if not tasks:
            st.info("请先创建任务，再为任务设置目标。")
            return
        with st.form("goal_create_panel"):
            task_id = st.selectbox("关联任务", list(labels), format_func=labels.get)
            period_start = st.date_input("开始日期", value=date.today(), key="goal_panel_start")
            period_end = st.date_input("结束日期", value=date.today() + timedelta(days=30), key="goal_panel_end")
            target = st.number_input("目标完成次数", min_value=1, value=1, step=1)
            create = st.form_submit_button("保存目标")
        if create:
            result = dispatch(user_id, "goal_progress_create", {
                "task_id": task_id,
                "period_start": str(period_start),
                "period_end": str(period_end),
                "target_count": target,
            }, prefix="goal-create")
            if data_or_error(result) is not None:
                st.session_state[create_key] = False
                st.success("目标已创建。")
                st.rerun()

    render_plans(user_id)


def render_reminders_goals(user_id: str) -> None:
    st.subheader("Reminders, recurrence, and goals")
    tasks, labels = task_options(user_id)
    reminder_tab, recurrence_tab, goal_tab, plan_tab = st.tabs(["Reminders", "Recurrence", "Goals", "Plans"])
    with reminder_tab:
        with st.form("reminder_create"):
            content = st.text_input("Reminder")
            trigger = st.text_input("Trigger at (ISO-8601)")
            task_choices = [None, *labels]
            task_id = st.selectbox(
                "Related task",
                task_choices,
                format_func=lambda value: "No related task" if value is None else labels[value],
            )
            timezone_name = st.text_input("Timezone", value="UTC")
            create = st.form_submit_button("Create reminder")
        if create:
            result = dispatch(user_id, "reminder_create", {
                "content": content,
                "trigger_at": trigger,
                "task_id": task_id,
                "timezone": timezone_name,
                "source": "ui",
            })
            if data_or_error(result) is not None:
                st.success("Reminder created.")
        page = st.number_input("Reminder page", min_value=0, value=0, step=1, key="reminder_page")
        reminders = data_or_error(dispatch(user_id, "reminder_list", {
            "limit": 50,
            "offset": page * 50,
            "include_source": True,
        })) or []
        display_reminders = [dict(item, task_title=labels.get(item.get("task_id"))) for item in reminders]
        st.dataframe(display_reminders, use_container_width=True, hide_index=True)
        if reminders:
            reminder_id = st.selectbox(
                "Reminder",
                [item["id"] for item in reminders],
                key="reminder_selected",
            )
            selected_reminder = next(item for item in reminders if item["id"] == reminder_id)
            terminal = {"sent", "expired", "cancelled"}
            if selected_reminder["status"] not in terminal:
                with st.form("reminder_edit"):
                    edit_content = st.text_input("Edited content", value=selected_reminder["content"])
                    edit_trigger = st.text_input("New trigger (ISO-8601, optional)")
                    edit_timezone = st.text_input("Edited timezone", value=selected_reminder["timezone"])
                    save = st.form_submit_button("Update reminder")
                if save:
                    result = dispatch(user_id, "reminder_update", {
                        "reminder_id": reminder_id,
                        "content": edit_content,
                        "trigger_at": edit_trigger or None,
                        "timezone": edit_timezone,
                    })
                    if data_or_error(result) is not None:
                        st.success("Reminder updated.")
            if selected_reminder["status"] not in terminal and st.button("Cancel reminder", key="reminder_cancel"):
                result = dispatch(user_id, "reminder_cancel", {"reminder_id": reminder_id})
                if data_or_error(result) is not None:
                    st.success("Reminder cancelled.")
    with recurrence_tab:
        if not tasks:
            st.info("Create a task before adding recurrence rules.")
        else:
            with st.form("recurrence_create"):
                task_id = st.selectbox("Task", list(labels), format_func=labels.get, key="recurrence_task")
                frequency = st.selectbox("Frequency", ["daily", "weekly", "monthly", "custom"])
                start_date = st.date_input("Start date", value=date.today())
                interval = st.number_input("Interval", min_value=1, value=1, step=1)
                end_date = st.date_input("End date", value=date.today() + timedelta(days=30))
                create = st.form_submit_button("Create recurrence")
            if create:
                timezone_name = (data_or_error(dispatch(user_id, "user_timezone_get", {})) or {}).get("timezone")
                result = dispatch(user_id, "recurrence_create", {"task_id": task_id, "frequency": frequency, "start_date": str(start_date), "interval_value": interval, "end_date": str(end_date), "timezone": timezone_name})
                if data_or_error(result) is not None:
                    st.success("Recurrence created.")
    with goal_tab:
        if not tasks:
            st.info("Create a task before adding goals.")
        else:
            with st.form("goal_create"):
                task_id = st.selectbox("Task", list(labels), format_func=labels.get, key="goal_task")
                period_start = st.date_input("Period start", value=date.today(), key="goal_start")
                period_end = st.date_input("Period end", value=date.today() + timedelta(days=30), key="goal_end")
                target = st.number_input("Target executions", min_value=1, value=1, step=1)
                create = st.form_submit_button("Create goal")
            if create:
                result = dispatch(user_id, "goal_progress_create", {"task_id": task_id, "period_start": str(period_start), "period_end": str(period_end), "target_count": target})
                if data_or_error(result) is not None:
                    st.success("Goal created.")
            goals = data_or_error(dispatch(user_id, "goal_progress_list", {})) or []
            st.dataframe(goals, use_container_width=True, hide_index=True)
    with plan_tab:
        render_plans(user_id)


def render_reminders_panel(user_id: str) -> None:
    """Render reminders first, with creation and recurrence behind buttons."""

    st.subheader("提醒")
    tasks, labels = task_options(user_id)
    reminders = data_or_error(dispatch(user_id, "reminder_list", {
        "limit": 100,
        "offset": 0,
        "include_source": True,
    })) or []
    display_reminders = [dict(item, task_title=labels.get(item.get("task_id"))) for item in reminders]
    if display_reminders:
        st.dataframe(display_reminders, use_container_width=True, hide_index=True)
    else:
        st.info("暂时还没有提醒。")

    create_key = "show_reminder_create"
    if st.button("创建提醒", key="open_reminder_create", use_container_width=True):
        st.session_state[create_key] = not st.session_state.get(create_key, False)
    if st.session_state.get(create_key, False):
        with st.form("reminder_create_panel"):
            content = st.text_input("提醒内容")
            trigger = st.text_input("触发时间（ISO-8601）")
            task_choices = [None, *labels]
            task_id = st.selectbox(
                "关联任务",
                task_choices,
                format_func=lambda value: "不关联任务" if value is None else labels[value],
            )
            timezone_name = st.text_input("时区", value="UTC")
            create = st.form_submit_button("保存提醒")
        if create:
            result = dispatch(user_id, "reminder_create", {
                "content": content,
                "trigger_at": trigger,
                "task_id": task_id,
                "timezone": timezone_name,
                "source": "ui",
            }, prefix="reminder-create")
            if data_or_error(result) is not None:
                st.session_state[create_key] = False
                st.success("提醒已创建。")
                st.rerun()

    if reminders:
        reminder_id = st.selectbox(
            "选择提醒",
            [item["id"] for item in reminders],
            key="reminder_panel_selected",
        )
        selected_reminder = next(item for item in reminders if item["id"] == reminder_id)
        terminal = {"sent", "expired", "cancelled"}
        if selected_reminder["status"] not in terminal:
            with st.form("reminder_edit_panel"):
                edit_content = st.text_input("修改内容", value=selected_reminder["content"])
                edit_trigger = st.text_input("修改触发时间（可选）")
                edit_timezone = st.text_input("修改时区", value=selected_reminder["timezone"])
                save = st.form_submit_button("保存修改")
            if save:
                result = dispatch(user_id, "reminder_update", {
                    "reminder_id": reminder_id,
                    "content": edit_content,
                    "trigger_at": edit_trigger or None,
                    "timezone": edit_timezone,
                }, prefix="reminder-update")
                if data_or_error(result) is not None:
                    st.success("提醒已更新。")
            if st.button("取消提醒", key="cancel_reminder_panel"):
                result = dispatch(user_id, "reminder_cancel", {"reminder_id": reminder_id}, prefix="reminder-cancel")
                if data_or_error(result) is not None:
                    st.success("提醒已取消。")

    with st.expander("添加重复规则", expanded=False):
        if not tasks:
            st.info("请先创建任务，再添加重复规则。")
        else:
            with st.form("recurrence_create_panel"):
                task_id = st.selectbox("任务", list(labels), format_func=labels.get, key="recurrence_panel_task")
                frequency = st.selectbox("频率", ["daily", "weekly", "monthly", "custom"], key="recurrence_panel_frequency")
                start_date = st.date_input("开始日期", value=date.today(), key="recurrence_panel_start")
                interval = st.number_input("间隔", min_value=1, value=1, step=1, key="recurrence_panel_interval")
                end_date = st.date_input("结束日期", value=date.today() + timedelta(days=30), key="recurrence_panel_end")
                create = st.form_submit_button("保存重复规则")
            if create:
                timezone_name = (data_or_error(dispatch(user_id, "user_timezone_get", {})) or {}).get("timezone")
                result = dispatch(user_id, "recurrence_create", {
                    "task_id": task_id,
                    "frequency": frequency,
                    "start_date": str(start_date),
                    "interval_value": interval,
                    "end_date": str(end_date),
                    "timezone": timezone_name,
                }, prefix="recurrence-create")
                if data_or_error(result) is not None:
                    st.success("重复规则已创建。")


def render_finance(user_id: str) -> None:
    st.subheader("财务")
    tasks, task_labels = task_options(user_id)
    transactions = data_or_error(dispatch(user_id, "transaction_list", {"limit": 500})) or []
    if transactions:
        st.dataframe(transactions, use_container_width=True, hide_index=True)
    else:
        st.info("暂时还没有财务记录。")

    create_key = "show_transaction_create"
    if st.button("记一笔账", key="open_transaction_create", use_container_width=True):
        st.session_state[create_key] = not st.session_state.get(create_key, False)
    if st.session_state.get(create_key, False):
        with st.form("transaction_create_panel"):
            transaction_type = st.selectbox("类型", ["expense", "income"])
            amount = st.number_input("金额（最小货币单位）", min_value=1, step=1)
            currency = st.text_input("币种", value="CNY")
            category = st.text_input("分类")
            note = st.text_input("备注")
            create = st.form_submit_button("保存账目")
        if create:
            result = dispatch(user_id, "transaction_create", {
                "type": transaction_type,
                "amount_minor": amount,
                "currency": currency,
                "category": category,
                "note": note or None,
            }, prefix="transaction-create")
            if data_or_error(result) is not None:
                st.session_state[create_key] = False
                st.success("账目已创建。")
                st.rerun()

    if transactions:
        transaction_id = st.selectbox("选择账目", [item["id"] for item in transactions], key="finance_selected")
        selected_transaction = next(item for item in transactions if item["id"] == transaction_id)
        with st.form("transaction_edit"):
            edit_type = st.selectbox("Edited type", ["expense", "income"], index=["expense", "income"].index(selected_transaction["type"]))
            edit_amount = st.number_input("Edited amount in minor units", min_value=1, value=int(selected_transaction["amount_minor"]), step=1)
            edit_category = st.text_input("Edited category", value=selected_transaction["category"])
            edit_note = st.text_input("Edited note", value=selected_transaction.get("note") or "")
            save_transaction = st.form_submit_button("Update transaction")
        if save_transaction:
            result = dispatch(user_id, "transaction_update", {
                "transaction_id": transaction_id,
                "type": edit_type,
                "amount_minor": edit_amount,
                "category": edit_category,
                "note": edit_note or None,
            }, prefix="transaction-update")
            if data_or_error(result) is not None:
                st.success("Transaction updated.")
        if st.button("Void transaction"):
            result = dispatch(user_id, "transaction_void", {"transaction_id": transaction_id})
            if data_or_error(result) is not None:
                st.success("Transaction voided.")
        if tasks:
            link_task = st.selectbox("Link transaction to task", list(task_labels), format_func=task_labels.get)
            if st.button("Confirm task and transaction link"):
                result = dispatch(user_id, "task_transaction_link", {
                    "task_id": link_task,
                    "transaction_id": transaction_id,
                    "basis": "user_confirmed",
                }, prefix="transaction-link")
                if data_or_error(result) is not None:
                    st.success("Task and transaction linked.")
        links = data_or_error(dispatch(user_id, "task_transaction_link_list", {"transaction_id": transaction_id})) or []
        if links:
            st.dataframe(links, use_container_width=True, hide_index=True)


def render_analytics(user_id: str) -> None:
    st.subheader("图表")
    period = st.selectbox("时间段", ["day", "week", "month", "year", "custom"], key="chart_period")
    range_start = range_end = None
    if period == "custom":
        range_start = st.date_input("开始日期", value=date.today() - timedelta(days=6), key="chart_range_start")
        range_end = st.date_input("结束日期（不含）", value=date.today() + timedelta(days=1), key="chart_range_end")
    chart_type = st.selectbox("图表类型", ["bar", "pie", "line"], key="chart_type")
    dataset_label = st.selectbox(
        "数据内容",
        ["Task by day", "Task by task", "Finance by category", "Goal progress"],
        key="chart_dataset",
    )
    task_id = st.number_input("任务筛选（可选）", min_value=0, value=0, step=1, key="chart_task_filter")
    status = st.selectbox(
        "任务状态",
        ["Any", "pending", "in_progress", "completed", "cancelled", "archived"],
        key="chart_status",
    )
    category = st.text_input("财务分类筛选", key="chart_category")
    currency = st.text_input("币种筛选", max_chars=3, key="chart_currency")
    if st.button("刷新图表", key="refresh_chart", type="primary", use_container_width=True):
        query = {
            "period": period,
            "task_id": int(task_id) if task_id else None,
            "status": None if status == "Any" else status,
            "category": category or None,
            "currency": currency or None,
        }
        if period == "custom":
            query.update({"range_start": str(range_start), "range_end": str(range_end)})
        statistics = data_or_error(dispatch(user_id, "statistics_query", query, prefix="statistics"))
        st.session_state.pop("analytics_chart", None)
        if statistics is not None:
            dataset = {
                "Task by day": (["period"], "count"),
                "Task by task": (["task_id"], "count"),
                "Finance by category": (["category"], "total_minor"),
                "Goal progress": (["period_start"], "completed_count"),
            }[dataset_label]
            chart = data_or_error(dispatch(user_id, "chart_build", {
                "chart_type": chart_type,
                "dataset_ref": statistics["dataset_ref"],
                "query_digest": statistics["query_digest"],
                "dimensions": dataset[0],
                "metric": dataset[1],
            }, prefix="chart"))
            if chart is not None:
                st.session_state["analytics_chart"] = chart["spec"]
                st.session_state["analytics_statistics"] = statistics

    spec = st.session_state.get("analytics_chart")
    if not spec:
        st.info("选择时间段和图表类型后，点击刷新图表。")
        return
    st.caption(
        f"{spec['range_start']} to {spec['range_end']} ({spec['timezone']})"
    )
    data = spec.get("data", [])
    if spec["chart_type"] == "table":
        st.dataframe(data, use_container_width=True, hide_index=True)
        return
    if not data:
        st.info("No data in the selected range.")
        return
    dimension = spec["dimensions"][0]
    metric = spec["metric"]
    numeric_values = [
        value
        for row in data
        for value in [row.get(metric)]
        if isinstance(value, (int, float))
    ]
    if not numeric_values or max(numeric_values, default=0) <= 0:
        st.info(
            "当前时间范围内没有可绘制的有效数据。请先记录任务执行、财务记录或目标进度，"
            "再刷新图表。"
        )
        st.dataframe(data, use_container_width=True, hide_index=True)
        return
    tooltip = [
        alt.Tooltip(field=field, type="quantitative" if field == metric else "nominal")
        for field in (*spec["dimensions"], metric)
    ]
    chart = alt.Chart({"values": data})
    if spec["chart_type"] == "line":
        chart = chart.mark_line(point=True).encode(
            x=alt.X(field=dimension, type="nominal"),
            y=alt.Y(field=metric, type="quantitative"),
            tooltip=tooltip,
        )
    elif spec["chart_type"] == "bar":
        chart = chart.mark_bar().encode(
            x=alt.X(field=dimension, type="nominal"),
            y=alt.Y(field=metric, type="quantitative"),
            tooltip=tooltip,
        )
    else:
        chart = chart.mark_arc().encode(
            theta=alt.Theta(field=metric, type="quantitative"),
            color=alt.Color(field=dimension, type="nominal"),
            tooltip=tooltip,
        )
    try:
        st.altair_chart(chart, use_container_width=True)
    except (TypeError, ValueError) as error:
        st.error(f"Unable to render chart data: {error}")


def render_operations(user_id: str) -> None:
    st.subheader("Operations")
    timezone_data = data_or_error(dispatch(user_id, "user_timezone_get", {})) or {"timezone": "UTC"}
    timezone_name = st.text_input("User timezone", value=timezone_data["timezone"])
    if st.button("Save timezone"):
        saved = data_or_error(dispatch(user_id, "user_timezone_set", {"timezone": timezone_name}))
        if saved is not None:
            st.success(f"Timezone saved: {saved['timezone']}")
    if st.button("Run health check"):
        health = data_or_error(dispatch(user_id, "runtime_health", {}))
        if health is not None:
            st.json(health)
    audits = data_or_error(dispatch(user_id, "tool_audit_list", {"limit": 100})) or []
    st.caption("Recent tool audit metadata")
    st.dataframe(audits, use_container_width=True, hide_index=True)
    st.subheader("Data exports")
    export_scope = st.selectbox("Export scope", ["all", "tasks", "plans", "finance", "conversations"])
    if st.button("Create JSON export"):
        exported = data_or_error(dispatch(user_id, "backup_export", {
            "scope": export_scope,
            "format": "json",
        }, prefix="backup-export"))
        if exported is not None:
            st.success(f"Export created: {exported['export_id']}")
    exports = data_or_error(dispatch(user_id, "backup_export_list", {"limit": 20})) or []
    if exports:
        st.dataframe(exports, use_container_width=True, hide_index=True)


def render_workbench(user_id: str, config: RuntimeConfig) -> None:
    """Render either the conversation workspace or one standalone feature page."""

    inject_workspace_style()
    active = st.session_state.get("active_panel")
    valid_pages = {"tasks", "goals", "reminders", "finance", "analytics", "operations"}
    if active not in valid_pages:
        active = None
        st.session_state.pop("active_panel", None)

    conversation = None
    labels = {}
    if active is None:
        conversation = ensure_conversation(user_id)
        conversations = conversation_options(user_id)
        labels = {int(item["id"]): item["title"] for item in conversations}
        conversation_id = int(conversation["id"])
        if st.session_state.get("conversation_history_session_id") != conversation_id:
            st.session_state["conversation_history"] = load_conversation_history(
                user_id,
                conversation_id,
            )
            st.session_state["conversation_history_session_id"] = conversation_id

    header_left, header_middle, header_right = st.columns([3.3, 2.8, 2.4], vertical_alignment="center")
    with header_left:
        st.markdown(
            '<div class="todo-brand"><span class="todo-brand-mark">T</span>'
            '<span>Todo Agent <span class="todo-brand-subtitle">工作台</span></span></div>',
            unsafe_allow_html=True,
        )
    with header_middle:
        if active is None:
            selected = st.selectbox(
                "当前会话",
                list(labels),
                index=list(labels).index(int(conversation["id"])) if labels else 0,
                format_func=labels.get,
                label_visibility="collapsed",
                key="conversation_selector",
            )
            if selected != int(conversation["id"]):
                st.session_state["conversation_id"] = selected
                st.session_state["conversation_history"] = load_conversation_history(user_id, selected)
                st.session_state["conversation_history_session_id"] = int(selected)
                st.rerun()
        elif st.button("返回对话", key="return_to_conversation", use_container_width=True):
            st.session_state.pop("active_panel", None)
            st.rerun()
    with header_right:
        safety_column, user_column, logout_column = st.columns([1.45, 1, .8], vertical_alignment="center")
        with safety_column:
            st.checkbox("确认高风险操作", key="confirm_risky_actions")
        with user_column:
            st.markdown(f'<div class="todo-user-label">{user_id}</div>', unsafe_allow_html=True)
        with logout_column:
            if st.button("退出", key="logout"):
                for key in ("user_id", "conversation_id", "conversation_history", "conversation_history_session_id", "active_panel", "confirm_risky_actions"):
                    st.session_state.pop(key, None)
                st.rerun()

    st.markdown('<div class="todo-topline"></div>', unsafe_allow_html=True)

    if active is None:
        render_assistant(user_id, config)
        render_entry_bar()
        with st.expander("会话管理", expanded=False):
            action_left, action_middle, action_right = st.columns(3)
            with action_left:
                if st.button("新建会话", key="new_conversation"):
                    created = data_or_error(dispatch(user_id, "conversation_create", {
                        "title": "新会话",
                        "client_request_id": request_id("conversation"),
                    }))
                    if created is not None:
                        st.session_state["conversation_id"] = int(created["id"])
                        st.session_state["conversation_history"] = []
                        st.session_state["conversation_history_session_id"] = int(created["id"])
                        st.rerun()
            with action_middle:
                new_title = st.text_input("会话名称", value=conversation["title"], key="conversation_title")
                if st.button("重命名", key="rename_conversation"):
                    renamed = data_or_error(dispatch(user_id, "conversation_rename", {
                        "session_id": int(conversation["id"]),
                        "title": new_title,
                    }))
                    if renamed is not None:
                        st.success("会话名称已更新。")
            with action_right:
                if st.button("归档会话", key="archive_conversation"):
                    archived = data_or_error(dispatch(user_id, "conversation_archive", {
                        "session_id": int(conversation["id"]),
                    }))
                    if archived is not None:
                        st.session_state.pop("conversation_id", None)
                        st.session_state["conversation_history"] = []
                        st.session_state.pop("conversation_history_session_id", None)
                        st.rerun()
    else:
        with st.container(border=True):
            render_active_page(user_id, active)
        render_entry_bar()

__all__ = ["render_analytics", "render_assistant", "render_finance", "render_operations", "render_plans", "render_reminders_goals", "render_tasks", "render_workbench"]
