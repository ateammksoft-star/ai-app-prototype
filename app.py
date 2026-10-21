import os
import json
import sqlite3
from datetime import datetime

from flask import Flask, abort, redirect, render_template, request, url_for
from google import genai

app = Flask(__name__)
app.config["DATABASE"] = os.path.join(app.instance_path, "tasks.db")

PRIORITIES = ["高", "中", "低"]
STATUSES = ["未着手", "進行中", "保留", "完了"]
SORT_OPTIONS = {
    "created_at": "作成日",
    "updated_at": "更新日",
    "due_date": "期限日",
    "priority": "優先度",
}
SORT_ORDERS = {"asc": "昇順", "desc": "降順"}
AI_MODEL = "gemini-3.6-flash"


def generate_ai_response(mode, user_input="", task_data=None):
    with open(
        os.path.join(app.root_path, "prompts", "task_ai_prompt.txt"),
        "r",
        encoding="utf-8",
    ) as prompt_file:
        prompt_template = prompt_file.read()
    request_data = json.dumps(
        {
            "mode": mode,
            "today": datetime.now().date().isoformat(),
            "user_input": user_input,
            "task_data": task_data or [],
        },
        ensure_ascii=False,
    )
    prompt = prompt_template.replace("{REQUEST_DATA}", request_data)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEYが設定されていません。")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=AI_MODEL, contents=prompt)
    if not response.text:
        raise ValueError("Geminiから応答を取得できませんでした。")
    return response.text.strip()


def generate_ai_json(mode, user_input="", task_data=None):
    response = generate_ai_response(mode, user_input, task_data)
    if response.startswith("```"):
        response = response.strip("`").removeprefix("json").strip()
    result = json.loads(response)
    if not isinstance(result, dict):
        raise ValueError("Geminiの応答形式が正しくありません。")
    return result


def initialize_database(connection):
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            details TEXT NOT NULL,
            due_date TEXT NOT NULL,
            priority TEXT NOT NULL,
            assignee TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    columns = {
        column[1]
        for column in connection.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "created_at" not in columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN created_at TEXT")
    if "updated_at" not in columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")
        connection.execute(
            "UPDATE tasks SET updated_at = created_at WHERE created_at IS NOT NULL"
        )
    if "completed_at" not in columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def get_tasks(filters=None, sort_by="due_date", sort_order="asc"):
    os.makedirs(app.instance_path, exist_ok=True)
    filters = filters or {}
    conditions = []
    parameters = []
    for field in ("title", "assignee"):
        value = filters.get(field, "")
        if value:
            conditions.append(f"{field} LIKE ?")
            parameters.append(f"%{value}%")
    for field, choices in (("status", STATUSES), ("priority", PRIORITIES)):
        value = filters.get(field, "")
        if value in choices:
            conditions.append(f"{field} = ?")
            parameters.append(value)
    due_date = filters.get("due_date", "")
    if due_date:
        conditions.append("due_date = ?")
        parameters.append(due_date)
    for field, operator in (("due_from", ">="), ("due_to", "<=")):
        value = filters.get(field, "")
        if value:
            conditions.append(f"due_date {operator} ?")
            parameters.append(value)

    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sort_by = sort_by if sort_by in SORT_OPTIONS else "due_date"
    sort_order = sort_order if sort_order in SORT_ORDERS else "asc"
    sort_expression = (
        "CASE priority WHEN '高' THEN 1 WHEN '中' THEN 2 "
        "WHEN '低' THEN 3 ELSE 4 END"
        if sort_by == "priority"
        else sort_by
    )
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        return connection.execute(
            f"SELECT * FROM tasks{where_clause} "
            f"ORDER BY {sort_expression} {sort_order.upper()}, due_date ASC, id DESC",
            parameters,
        ).fetchall()


def get_task_counts():
    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN status = ? THEN 1 END) AS not_started,
                COUNT(CASE WHEN status = ? THEN 1 END) AS in_progress,
                COUNT(CASE WHEN status = ? THEN 1 END) AS completed,
                COUNT(CASE WHEN status = ? THEN 1 END) AS on_hold
            FROM tasks
            """,
            ("未着手", "進行中", "完了", "保留"),
        ).fetchone()
        return dict(row)


def get_task(task_id):
    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        return connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()


def get_task_comments(task_id):
    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        return connection.execute(
            "SELECT * FROM task_comments WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (task_id,),
        ).fetchall()


def get_ai_task_data(task_id=None):
    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        if task_id is None:
            tasks = connection.execute(
                "SELECT id, title, details, due_date, priority, assignee, status, "
                "created_at, updated_at, completed_at FROM tasks "
                "ORDER BY due_date, id DESC LIMIT 100"
            ).fetchall()
        else:
            tasks = connection.execute(
                "SELECT id, title, details, due_date, priority, assignee, status, "
                "created_at, updated_at, completed_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchall()
        if not tasks:
            return []

        task_ids = [task["id"] for task in tasks]
        placeholders = ", ".join("?" for _ in task_ids)
        comments = connection.execute(
            "SELECT task_id, content, created_at FROM task_comments "
            f"WHERE task_id IN ({placeholders}) ORDER BY created_at DESC, id DESC",
            task_ids,
        ).fetchall()
        comments_by_task = {}
        for comment in comments:
            entries = comments_by_task.setdefault(comment["task_id"], [])
            if len(entries) < 3:
                entries.append(
                    {
                        "content": comment["content"][:500],
                        "created_at": comment["created_at"],
                    }
                )
        return [
            {
                **dict(task),
                "comments": comments_by_task.get(task["id"], []),
            }
            for task in tasks
        ]


def get_dashboard_analysis_data():
    tasks = get_tasks()
    counts = get_task_counts()
    today = datetime.now().date()
    assignee_counts = {}
    priority_counts = {priority: 0 for priority in PRIORITIES}
    overdue_count = 0
    for task in tasks:
        assignee_counts[task["assignee"]] = (
            assignee_counts.get(task["assignee"], 0) + 1
        )
        if task["priority"] in priority_counts:
            priority_counts[task["priority"]] += 1
        if task["status"] != "完了" and task["due_date"] < today.isoformat():
            overdue_count += 1

    total = counts["total"]
    return {
        "today": today.isoformat(),
        "total": total,
        "status_counts": {
            "未着手": counts["not_started"],
            "進行中": counts["in_progress"],
            "完了": counts["completed"],
            "保留": counts["on_hold"],
        },
        "completion_rate": round(counts["completed"] / total * 100, 1)
        if total
        else 0,
        "overdue_count": overdue_count,
        "delay_rate": round(overdue_count / total * 100, 1) if total else 0,
        "assignee_counts": assignee_counts,
        "priority_counts": priority_counts,
    }


def add_task_comment(task_id, content):
    content = content.strip()
    if not content:
        return "コメントや作業メモを入力してください。"
    if len(content) > 2000:
        return "コメントは2000文字以内で入力してください。"

    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        initialize_database(connection)
        result = connection.execute(
            """
            INSERT INTO task_comments (task_id, content, created_at)
            SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM tasks WHERE id = ?)
            """,
            (
                task_id,
                content,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                task_id,
            ),
        )
        if result.rowcount == 0:
            return "タスクが見つかりません。"
    return None


def add_task(form):
    title = form.get("title", "").strip()
    details = form.get("details", "").strip()
    due_date = form.get("due_date", "")
    priority = form.get("priority", "")
    assignee = form.get("assignee", "").strip()
    status = form.get("status", "")

    if not title or not due_date or not assignee:
        return "タスク名、期限日、担当者を入力してください。"
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return "期限日を正しく入力してください。"
    if priority not in PRIORITIES or status not in STATUSES:
        return "優先度とステータスを選択してください。"

    os.makedirs(app.instance_path, exist_ok=True)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        initialize_database(connection)
        connection.execute(
            """
            INSERT INTO tasks (
                title, details, due_date, priority, assignee, status,
                created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                details,
                due_date,
                priority,
                assignee,
                status,
                created_at,
                created_at,
                created_at if status == "完了" else None,
            ),
        )
    return None


def update_task(task_id, form):
    title = form.get("title", "").strip()
    details = form.get("details", "").strip()
    due_date = form.get("due_date", "")
    priority = form.get("priority", "")
    assignee = form.get("assignee", "").strip()
    status = form.get("status", "")

    if not title or not due_date or not assignee:
        return "タスク名、期限日、担当者を入力してください。"
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return "期限日を正しく入力してください。"
    if priority not in PRIORITIES or status not in STATUSES:
        return "優先度とステータスを選択してください。"

    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        initialize_database(connection)
        existing = connection.execute(
            "SELECT status, completed_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if existing is None:
            return "タスクが見つかりません。"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if status == "完了":
            completed_at = existing[1]
            if existing[0] != "完了" and completed_at is None:
                completed_at = now
        else:
            completed_at = None
        result = connection.execute(
            """
            UPDATE tasks
            SET title = ?, details = ?, due_date = ?, priority = ?, assignee = ?,
                status = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                title,
                details,
                due_date,
                priority,
                assignee,
                status,
                now,
                completed_at,
                task_id,
            ),
        )
        if result.rowcount == 0:
            return "タスクが見つかりません。"
    return None


def remove_task(task_id):
    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        initialize_database(connection)
        result = connection.execute(
            "DELETE FROM tasks WHERE id = ?", (task_id,)
        )
        return result.rowcount > 0


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    if not remove_task(task_id):
        abort(404)
    return redirect(url_for("index", deleted=1))


@app.route("/tasks/<int:task_id>", methods=["GET", "POST"])
def task_detail(task_id):
    task = get_task(task_id)
    if task is None:
        abort(404)

    error = ""
    form_data = {}
    comment_error = ""
    comment_form_data = {}
    ai_error = ""
    priority_suggestion = None
    progress_analysis = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "comment":
            comment_error = add_task_comment(
                task_id, request.form.get("content", "")
            ) or ""
            if not comment_error:
                return redirect(
                    url_for("task_detail", task_id=task_id, commented=1)
                )
            comment_form_data = request.form
        elif action in ("ai_priority", "ai_progress"):
            try:
                task_data = get_ai_task_data(task_id)
                if action == "ai_priority":
                    result = generate_ai_json("priority", task_data=task_data)
                    if result.get("priority") not in PRIORITIES:
                        raise ValueError("優先度の形式が正しくありません。")
                    priority_suggestion = {
                        "priority": result["priority"],
                        "reason": str(result.get("reason", "")),
                    }
                else:
                    progress_analysis = generate_ai_response(
                        "progress", task_data=task_data
                    )
            except Exception:
                app.logger.exception("Gemini task analysis failed")
                ai_error = "AI分析に失敗しました。API設定と入力内容を確認してください。"
        else:
            error = update_task(task_id, request.form) or ""
            if not error:
                return redirect(url_for("task_detail", task_id=task_id, updated=1))
            form_data = request.form

    return render_template(
        "task_detail.html",
        task=task,
        comments=get_task_comments(task_id),
        priorities=PRIORITIES,
        statuses=STATUSES,
        error=error,
        form_data=form_data,
        updated=request.args.get("updated") == "1",
        comment_error=comment_error,
        comment_form_data=comment_form_data,
        commented=request.args.get("commented") == "1",
        ai_error=ai_error,
        priority_suggestion=priority_suggestion,
        progress_analysis=progress_analysis,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    comment = ""
    error = ""
    form_data = {}
    ai_error = ""
    ai_create_message = ""
    ai_task_drafts = []
    ai_search_query = request.args.get("ai_search_query", "")
    dashboard_analysis = ""
    chat_question = ""
    chat_response = ""
    chat_history = []
    filters = {
        "title": request.args.get("title", "").strip(),
        "assignee": request.args.get("assignee", "").strip(),
        "status": request.args.get("status", ""),
        "priority": request.args.get("priority", ""),
        "due_from": request.args.get("due_from", "").strip(),
        "due_to": request.args.get("due_to", "").strip(),
    }
    sort_by = request.args.get("sort_by", "due_date")
    sort_order = request.args.get("sort_order", "asc")
    if sort_by not in SORT_OPTIONS:
        sort_by = "due_date"
    if sort_order not in SORT_ORDERS:
        sort_order = "asc"
    if filters["status"] not in STATUSES:
        filters["status"] = ""
    if filters["priority"] not in PRIORITIES:
        filters["priority"] = ""
    for field in ("due_from", "due_to"):
        if filters[field]:
            try:
                datetime.strptime(filters[field], "%Y-%m-%d")
            except ValueError:
                filters[field] = ""

    if request.method == "POST":
        action = request.form.get("action")
        if action == "register":
            error = add_task(request.form) or ""
            if not error:
                return redirect(url_for("index", registered=1))
            form_data = request.form
        elif action == "ai_create":
            description = request.form.get("description", "").strip()
            if not description:
                ai_error = "作成したいタスクの内容を入力してください。"
            elif len(description) > 1000:
                ai_error = "タスク作成の入力は1000文字以内で入力してください。"
            else:
                try:
                    generated = generate_ai_json("create_task", description)
                    drafts = generated.get("tasks")
                    if not isinstance(drafts, list) or not 1 <= len(drafts) <= 10:
                        raise ValueError("AIが作成したタスク案の形式が正しくありません。")
                    for draft in drafts:
                        if not isinstance(draft, dict):
                            raise ValueError("AIが作成したタスク案の形式が正しくありません。")
                        title = str(draft.get("title") or "").strip()
                        details = str(draft.get("details") or "").strip()
                        due_date = str(draft.get("due_date") or "").strip()
                        assignee = str(draft.get("assignee") or "未定").strip()
                        priority = draft.get("priority")
                        if not title or len(title) > 200 or len(details) > 2000:
                            raise ValueError("AIが作成したタスク情報を確認してください。")
                        if due_date:
                            datetime.strptime(due_date, "%Y-%m-%d")
                        if assignee not in ("", "未定") and len(assignee) > 100:
                            raise ValueError("AIが作成した担当者を確認してください。")
                        if priority not in PRIORITIES:
                            priority = "中"
                        ai_task_drafts.append(
                            {
                                "title": title,
                                "details": details,
                                "due_date": due_date,
                                "assignee": assignee or "未定",
                                "priority": priority,
                                "status": "未着手",
                            }
                        )
                    ai_create_message = (
                        f"AIが{len(ai_task_drafts)}件のタスク案を作成しました。"
                        "内容を確認し、必要な案だけ個別に登録してください。"
                    )
                except Exception:
                    app.logger.exception("Gemini task creation failed")
                    ai_error = "AIタスク作成に失敗しました。入力内容とAPI設定を確認してください。"
        elif action == "ai_search":
            search_query = request.form.get("query", "").strip()
            ai_search_query = search_query
            if not search_query:
                ai_error = "自然文の検索条件を入力してください。"
            elif len(search_query) > 300:
                ai_error = "検索条件は300文字以内で入力してください。"
            else:
                try:
                    result = generate_ai_json("parse_search", search_query)
                    search_filters = {
                        "title": str(result.get("title", "")).strip(),
                        "assignee": str(result.get("assignee", "")).strip(),
                        "status": result.get("status", ""),
                        "priority": result.get("priority", ""),
                        "due_from": str(result.get("due_from", "")).strip(),
                        "due_to": str(result.get("due_to", "")).strip(),
                    }
                    if search_filters["status"] not in STATUSES:
                        search_filters["status"] = ""
                    if search_filters["priority"] not in PRIORITIES:
                        search_filters["priority"] = ""
                    for field in ("due_from", "due_to"):
                        if search_filters[field]:
                            datetime.strptime(search_filters[field], "%Y-%m-%d")
                    if (
                        search_filters["due_from"]
                        and search_filters["due_to"]
                        and search_filters["due_from"] > search_filters["due_to"]
                    ):
                        raise ValueError("期限日の範囲が正しくありません。")
                    search_filters.update(
                        sort_by=sort_by,
                        sort_order=sort_order,
                        ai_search=1,
                        ai_search_query=search_query,
                    )
                    return redirect(url_for("index", **search_filters))
                except Exception:
                    app.logger.exception("Gemini task search parsing failed")
                    ai_error = "AI検索条件を作成できませんでした。入力内容を確認してください。"
        elif action == "ai_dashboard":
            try:
                dashboard_analysis = generate_ai_response(
                    "dashboard",
                    task_data=get_dashboard_analysis_data(),
                )
            except Exception:
                app.logger.exception("Gemini dashboard analysis failed")
                ai_error = "AIダッシュボード分析に失敗しました。API設定を確認してください。"
        elif action == "chat":
            chat_question = request.form.get("question", "").strip()
            try:
                submitted_history = json.loads(request.form.get("chat_history", "[]"))
                if isinstance(submitted_history, list):
                    chat_history = [
                        {
                            "role": message["role"],
                            "content": str(message["content"])[:1000],
                        }
                        for message in submitted_history[-10:]
                        if isinstance(message, dict)
                        and message.get("role") in ("user", "assistant")
                        and isinstance(message.get("content"), str)
                    ]
            except (json.JSONDecodeError, TypeError):
                chat_history = []
            if not chat_question:
                ai_error = "質問を入力してください。"
            elif len(chat_question) > 1000:
                ai_error = "質問は1000文字以内で入力してください。"
            else:
                try:
                    chat_response = generate_ai_response(
                        "chat",
                        chat_question,
                        {
                            "tasks": get_ai_task_data(),
                            "conversation": chat_history,
                        },
                    )
                    chat_history.extend(
                        [
                            {"role": "user", "content": chat_question},
                            {"role": "assistant", "content": chat_response},
                        ]
                    )
                    chat_history = chat_history[-10:]
                    chat_question = ""
                except Exception:
                    app.logger.exception("Gemini task chat failed")
                    ai_error = "AIアシスタントに接続できませんでした。API設定を確認してください。"
        else:
            task = request.form.get("task", "")
            with open("prompts/system_prompt.txt", "r", encoding="utf-8") as f:
                prompt_template = f.read()
            prompt = prompt_template.replace("{TASK}", task)
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
            )
            comment = response.text

    return render_template(
        "index.html",
        comment=comment,
        ai_error=ai_error,
        ai_create_message=ai_create_message,
        ai_task_drafts=ai_task_drafts,
        ai_search_query=ai_search_query,
        dashboard_analysis=dashboard_analysis,
        chat_question=chat_question,
        chat_response=chat_response,
        chat_history=chat_history,
        chat_history_json=json.dumps(chat_history, ensure_ascii=False),
        tasks=get_tasks(filters, sort_by, sort_order),
        task_counts=get_task_counts(),
        priorities=PRIORITIES,
        statuses=STATUSES,
        sort_options=SORT_OPTIONS,
        sort_orders=SORT_ORDERS,
        sort_by=sort_by,
        sort_order=sort_order,
        filters=filters,
        has_filters=(
            any(filters.values())
            or request.args.get("ai_search") == "1"
            or sort_by != "due_date"
            or sort_order != "asc"
        ),
        error=error,
        form_data=form_data,
        registered=request.args.get("registered") == "1",
        deleted=request.args.get("deleted") == "1",
        ai_search_applied=request.args.get("ai_search") == "1",
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)