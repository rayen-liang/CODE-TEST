import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PROGRAMS_DB_PATH", APP_DIR / "programs.db"))
LANGUAGES = {
    ".py": ("Python", "python"),
    ".js": ("JavaScript", "javascript"),
    ".cpp": ("C++", "c_cpp"),
    ".cc": ("C++", "c_cpp"),
    ".cxx": ("C++", "c_cpp"),
    ".c": ("C++", "c_cpp"),
    ".h": ("C++", "c_cpp"),
    ".hh": ("C++", "c_cpp"),
    ".hpp": ("C++", "c_cpp"),
    ".hxx": ("C++", "c_cpp"),
}
MAX_UPLOAD_FILES = 50
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
FILE_STATUSES = ("未完成", "完成")
DEFAULT_HOME_TITLE = "我的程式學習空間"


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database():
    with database() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('folder', 'file')),
                name TEXT NOT NULL,
                parent_id TEXT REFERENCES items(id) ON DELETE CASCADE,
                language TEXT,
                content TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT '未完成',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT '未完成',
                edited_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_root_item_name
                ON items(name COLLATE NOCASE) WHERE parent_id IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS unique_child_item_name
                ON items(parent_id, name COLLATE NOCASE) WHERE parent_id IS NOT NULL;
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        if "uploaded_at" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN uploaded_at TEXT")
            conn.execute("UPDATE items SET uploaded_at = created_at WHERE uploaded_at IS NULL")
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE items ADD COLUMN status TEXT NOT NULL DEFAULT '未完成'"
            )
        history_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(edit_history)")
        }
        if "status" not in history_columns:
            conn.execute(
                "ALTER TABLE edit_history ADD COLUMN status TEXT NOT NULL DEFAULT '未完成'"
            )


def normalize_name(value):
    name = value.strip()
    if not name or name in {".", ".."} or re.search(r'[\\/:*?"<>|]', name):
        raise ValueError("名稱不可為空，且不能包含 / \\ : * ? \" < > | 等字元。")
    return name


def normalize_tags(value):
    return sorted({tag.strip().lstrip("#") for tag in value.split(",") if tag.strip().lstrip("#")})


def create_item(
    kind,
    name,
    parent_id=None,
    language=None,
    content="",
    tags=None,
    status="未完成",
):
    name = normalize_name(name)
    if kind == "file" and status not in FILE_STATUSES:
        raise ValueError("檔案狀態必須是「完成」或「未完成」。")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    item_id = uuid.uuid4().hex
    with database() as conn:
        if parent_id:
            parent = conn.execute(
                "SELECT kind FROM items WHERE id = ?", (parent_id,)
            ).fetchone()
            if not parent or parent["kind"] != "folder":
                raise ValueError("選取的資料夾不存在。")
        try:
            conn.execute(
                """
                INSERT INTO items (id, kind, name, parent_id, language, content, tags, status, created_at, updated_at, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    kind,
                    name,
                    parent_id,
                    language,
                    content,
                    json.dumps(tags or [], ensure_ascii=False),
                    status if kind == "file" else "未完成",
                    now,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("同一個資料夾中已經有相同名稱的項目。") from exc
    return item_id


def update_file(item_id, name, content, tags, status):
    name = normalize_name(name)
    if status not in FILE_STATUSES:
        raise ValueError("檔案狀態必須是「完成」或「未完成」。")
    language_settings = LANGUAGES.get(Path(name).suffix.lower())
    if not language_settings:
        raise ValueError("檔案副檔名必須是支援的 Python、JavaScript 或 C++ 原始碼格式。")
    language = language_settings[0]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with database() as conn:
        try:
            previous = conn.execute(
                "SELECT name, content, tags, status FROM items WHERE id = ? AND kind = 'file'",
                (item_id,),
            ).fetchone()
            if not previous:
                raise ValueError("找不到要更新的程式。")
            if (
                previous["name"] == name
                and previous["content"] == content
                and previous["tags"] == json.dumps(tags, ensure_ascii=False)
                and previous["status"] == status
            ):
                return
            conn.execute(
                """
                INSERT INTO edit_history (item_id, name, content, tags, status, edited_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    previous["name"],
                    previous["content"],
                    previous["tags"],
                    previous["status"],
                    now,
                ),
            )
            result = conn.execute(
                """
                UPDATE items SET name = ?, language = ?, content = ?, tags = ?, status = ?, updated_at = ?
                WHERE id = ? AND kind = 'file'
                """,
                (
                    name,
                    language,
                    content,
                    json.dumps(tags, ensure_ascii=False),
                    status,
                    now,
                    item_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("同一個資料夾中已經有相同名稱的項目。") from exc
        if result.rowcount != 1:
            raise ValueError("找不到要更新的程式。")


def prepare_uploads(uploaded_files):
    if len(uploaded_files) > MAX_UPLOAD_FILES:
        raise ValueError(f"一次最多可上傳 {MAX_UPLOAD_FILES} 個程式檔。")
    prepared = []
    total_bytes = 0
    names = set()
    for uploaded_file in uploaded_files:
        filename = uploaded_file.name.replace("\\", "/").rsplit("/", 1)[-1]
        name = normalize_name(filename)
        extension = Path(name).suffix.lower()
        if extension not in LANGUAGES:
            raise ValueError(f"不支援 {name}；只接受 Python、JavaScript 或 C++ 原始碼。")
        if name.casefold() in names:
            raise ValueError(f"上傳檔案中有重複名稱：{name}")
        names.add(name.casefold())
        content_bytes = uploaded_file.getvalue()
        total_bytes += len(content_bytes)
        if total_bytes > MAX_UPLOAD_BYTES:
            raise ValueError("單次上傳總容量不可超過 25 MB。")
        try:
            content = content_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{name} 不是有效的 UTF-8 文字檔。") from exc
        language, _ = LANGUAGES[extension]
        prepared.append((name, language, content))
    return prepared


def upload_files(uploaded_files, parent_id=None):
    prepared = prepare_uploads(uploaded_files)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with database() as conn:
        if parent_id:
            parent = conn.execute(
                "SELECT kind FROM items WHERE id = ?", (parent_id,)
            ).fetchone()
            if not parent or parent["kind"] != "folder":
                raise ValueError("選取的資料夾不存在。")
        existing = {
            row["name"].casefold()
            for row in conn.execute(
                "SELECT name FROM items WHERE parent_id IS ?",
                (parent_id,),
            )
        }
        duplicates = existing.intersection(name.casefold() for name, _, _ in prepared)
        if duplicates:
            raise ValueError(f"此資料夾已有同名檔案：{', '.join(sorted(duplicates))}")
        try:
            conn.executemany(
                """
                INSERT INTO items (id, kind, name, parent_id, language, content, tags, status, created_at, updated_at, uploaded_at)
                VALUES (?, 'file', ?, ?, ?, ?, '[]', '未完成', ?, ?, ?)
                """,
                [
                    (uuid.uuid4().hex, name, parent_id, language, content, now, now, now)
                    for name, language, content in prepared
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("上傳的檔案名稱重複，請重新確認資料夾內容。") from exc
    return len(prepared)


def delete_item(item_id):
    with database() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def get_items():
    with database() as conn:
        rows = conn.execute(
            """
            SELECT id, kind, name, parent_id, language, content, tags, status, created_at, updated_at, uploaded_at
            FROM items
            ORDER BY CASE kind WHEN 'folder' THEN 0 ELSE 1 END, name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_edit_history(item_id):
    with database() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT name, content, tags, status, edited_at
                FROM edit_history
                WHERE item_id = ?
                ORDER BY id DESC
                """,
                (item_id,),
            )
        ]


def get_home_settings(items=None):
    with database() as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('home_title', 'pinned_folders')"
        ).fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    pinned_folders = json.loads(settings.get("pinned_folders", "[]"))
    folder_ids = {
        item["id"]
        for item in (items if items is not None else get_items())
        if item["kind"] == "folder"
    }
    return {
        "home_title": settings.get("home_title", DEFAULT_HOME_TITLE),
        "pinned_folders": [
            folder_id for folder_id in pinned_folders if folder_id in folder_ids
        ],
    }


def save_home_settings(title, pinned_folder_ids, items):
    title = title.strip()
    if not title:
        raise ValueError("首頁標題不可為空。")
    if len(title) > 80:
        raise ValueError("首頁標題不可超過 80 個字元。")
    valid_folder_ids = {
        item["id"] for item in items if item["kind"] == "folder"
    }
    if not set(pinned_folder_ids).issubset(valid_folder_ids):
        raise ValueError("釘選項目必須是現有資料夾。")
    with database() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            [
                ("home_title", title),
                (
                    "pinned_folders",
                    json.dumps(list(dict.fromkeys(pinned_folder_ids))),
                ),
            ],
        )


def get_recent_files(files, limit=6):
    return sorted(
        files,
        key=lambda item: (item["updated_at"], item["name"].casefold()),
        reverse=True,
    )[:limit]


def get_path(item, items_by_id):
    parts = [item["name"]]
    parent_id = item["parent_id"]
    while parent_id and parent_id in items_by_id:
        parent = items_by_id[parent_id]
        parts.append(parent["name"])
        parent_id = parent["parent_id"]
    return "/".join(reversed(parts))


def get_code_language(item):
    return LANGUAGES.get(Path(item["name"]).suffix.lower(), ("", "text"))[1].replace("c_cpp", "cpp")


def format_timestamp(value):
    if not value:
        return "—"
    timestamp = datetime.fromisoformat(value).astimezone()
    return timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")


def status_label(status):
    return "✅ 完成" if status == "完成" else "🟡 未完成"


def select_item(item_id):
    st.query_params["item"] = item_id
    st.rerun()


def get_expanded_folder_ids(selected, items_by_id):
    expanded_ids = set()
    parent_id = selected["id"] if selected["kind"] == "folder" else selected["parent_id"]
    while parent_id and parent_id in items_by_id:
        parent = items_by_id[parent_id]
        if parent["kind"] == "folder":
            expanded_ids.add(parent["id"])
        parent_id = parent["parent_id"]
    return expanded_ids


def render_file_tree(items, parent_id, selected_id, visible_file_ids, expanded_folder_ids):
    children = [
        item
        for item in items
        if item["parent_id"] == parent_id
        and (item["kind"] == "folder" or item["id"] in visible_file_ids)
    ]
    children.sort(key=lambda item: (item["kind"] != "folder", item["name"].casefold()))
    for item in children:
        if item["kind"] == "folder":
            with st.expander(
                f"📁 {item['name']}", expanded=item["id"] in expanded_folder_ids
            ):
                if st.button(
                    "開啟資料夾",
                    key=f"tree_{item['id']}",
                    disabled=item["id"] == selected_id,
                    use_container_width=True,
                ):
                    st.query_params["item"] = item["id"]
                    st.rerun()
                render_file_tree(
                    items, item["id"], selected_id, visible_file_ids, expanded_folder_ids
                )
        elif st.button(
            f"📄 {item['name']} · {status_label(item['status'])}",
            key=f"tree_{item['id']}",
            disabled=item["id"] == selected_id,
            use_container_width=True,
        ):
            st.query_params["item"] = item["id"]
            st.rerun()


def render_home_settings(items, settings):
    folders = [item for item in items if item["kind"] == "folder"]
    folder_by_id = {item["id"]: item["name"] for item in folders}
    with st.sidebar.expander("自訂首頁", expanded=False):
        if not folders:
            st.caption("先建立資料夾，便可釘選至首頁。")
        with st.form("home_customization"):
            title = st.text_input(
                "首頁標題",
                value=settings["home_title"],
                max_chars=80,
            )
            pinned_folder_ids = st.multiselect(
                "釘選資料夾",
                options=list(folder_by_id),
                default=settings["pinned_folders"],
                format_func=folder_by_id.get,
                key=f"pinned_folders_{hash(tuple(folder_by_id))}",
            )
            save = st.form_submit_button("儲存首頁設定", use_container_width=True)
        if save:
            try:
                save_home_settings(title, pinned_folder_ids, items)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("首頁設定已儲存。")
                st.rerun()


def render_home_file(item, items_by_id, section):
    title = f"{status_label(item['status'])} · {item['name']}"
    st.caption(get_path(item, items_by_id))
    if st.button(
        title,
        key=f"home_file_{section}_{item['id']}",
        use_container_width=True,
    ):
        select_item(item["id"])
    st.caption(f"最近編輯：{format_timestamp(item['updated_at'])}")


def render_read_only_file(item):
    st.title(item["name"])
    st.caption(f"最近編輯：{format_timestamp(item['updated_at'])}")
    st.code(item["content"], language=get_code_language(item))


def render_file_editor(item):
    st.caption(
        f"上傳時間：{format_timestamp(item['uploaded_at'])} · "
        f"最近編輯：{format_timestamp(item['updated_at'])}"
    )
    tags = json.loads(item["tags"])
    edited_name = st.text_input(
        "檔案名稱", value=item["name"], key=f"name_{item['id']}"
    )
    edited_content = st.text_area(
        "編輯程式碼",
        value=item["content"],
        height=430,
        key=f"content_{item['id']}",
    )
    st.caption("程式碼語法上色預覽")
    st.code(edited_content, language=get_code_language(item))
    edited_tags = st.text_input(
        "標籤（使用逗號分隔）",
        value=", ".join(tags),
        key=f"tags_{item['id']}",
    )
    edited_status = st.selectbox(
        "檔案狀態",
        FILE_STATUSES,
        index=FILE_STATUSES.index(item["status"]),
        key=f"status_{item['id']}",
    )
    if st.button("儲存變更", type="primary"):
        try:
            update_file(
                item["id"],
                edited_name,
                edited_content,
                normalize_tags(edited_tags),
                edited_status,
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success("程式已儲存。")
            st.rerun()

    history = get_edit_history(item["id"])
    with st.expander(f"編輯紀錄（{len(history)} 筆）"):
        if not history:
            st.caption("尚無編輯紀錄；儲存第一次修改後會留下原始版本。")
        for index, revision in enumerate(history, start=1):
            st.markdown(
                f"**第 {len(history) - index + 1} 次編輯前的版本** · "
                f"{format_timestamp(revision['edited_at'])} · "
                f"{status_label(revision['status'])}"
            )
            st.caption(
                f"檔名：{revision['name']} · "
                f"標籤：{'、'.join('#' + tag for tag in json.loads(revision['tags'])) or '無'}"
            )
            st.code(revision["content"], language=get_code_language(revision))

    if st.button("刪除程式", type="secondary"):
        st.session_state["confirm_delete"] = item["id"]
    if st.session_state.get("confirm_delete") == item["id"]:
        st.warning("刪除後無法復原，相關編輯紀錄也會一併刪除。")
        if st.button("確認永久刪除程式", type="primary"):
            delete_item(item["id"])
            st.session_state.pop("confirm_delete", None)
            st.query_params.clear()
            st.rerun()


def get_configured_password():
    return os.environ.get("APP_EDIT_PASSWORD") or st.secrets.get("edit_password", "")


def password_matches(candidate, configured):
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8")).digest(),
        hashlib.sha256(configured.encode("utf-8")).digest(),
    )


def make_share_url(item_id, base_url=None):
    current = urlsplit(base_url or st.context.url)
    return urlunsplit(
        (current.scheme, current.netloc, current.path, urlencode({"share": item_id}), "")
    )


def login_sidebar(configured_password):
    with st.sidebar:
        st.header("編輯權限")
        if st.session_state.get("is_editor"):
            st.success("已取得編輯權限")
            if st.button("登出", use_container_width=True):
                st.session_state["is_editor"] = False
                st.rerun()
        else:
            with st.form("login"):
                password = st.text_input("編輯密碼", type="password")
                submitted = st.form_submit_button("解鎖編輯", use_container_width=True)
            if submitted:
                if configured_password and password_matches(password, configured_password):
                    st.session_state["is_editor"] = True
                    st.rerun()
                else:
                    st.error("密碼錯誤，或尚未設定編輯密碼。")
        st.caption(
            "已解鎖編輯功能。"
            if st.session_state.get("is_editor")
            else "未登入時僅能檢視與分享程式。"
        )


def render_creation_forms(folders):
    with st.sidebar:
        st.divider()
        st.subheader("建立內容")
        folder_options = [("根目錄", None)] + [(item["name"], item["id"]) for item in folders]
        with st.form("create_folder", clear_on_submit=True):
            folder_name = st.text_input("新資料夾名稱")
            folder_parent = st.selectbox(
                "資料夾位置",
                folder_options,
                format_func=lambda option: option[0],
                key="folder_parent",
            )
            create_folder = st.form_submit_button("建立資料夾", use_container_width=True)
        if create_folder:
            try:
                folder_id = create_item("folder", folder_name, folder_parent[1])
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.query_params["item"] = folder_id
                st.rerun()
        with st.form("create_file", clear_on_submit=True):
            file_name = st.text_input("程式名稱（不含副檔名）")
            file_language = st.selectbox("程式語言", ["Python", "JavaScript", "C++"])
            file_parent = st.selectbox(
                "儲存位置",
                folder_options,
                format_func=lambda option: option[0],
                key="file_parent",
            )
            create_file = st.form_submit_button("新增程式", use_container_width=True)
        if create_file:
            try:
                clean_name = normalize_name(file_name)
                selected_extension = {
                    "Python": ".py",
                    "JavaScript": ".js",
                    "C++": ".cpp",
                }[file_language]
                existing_extension = Path(clean_name).suffix.lower()
                if existing_extension and (
                    existing_extension not in LANGUAGES
                    or LANGUAGES[existing_extension][0] != file_language
                ):
                    raise ValueError("檔案副檔名與所選程式語言不符。")
                if not existing_extension:
                    clean_name += selected_extension
                file_id = create_item(
                    "file", clean_name, file_parent[1], language=file_language
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.query_params["item"] = file_id
                st.rerun()


def render_upload_form(parent_id):
    with st.sidebar:
        st.divider()
        st.subheader("上傳程式檔")
        st.caption("支援 Python、JavaScript 和 C++，最多 50 個檔案、總計 25 MB。")
        with st.form(f"upload_files_{parent_id or 'root'}", clear_on_submit=True):
            uploaded_files = st.file_uploader(
                "選取一個或多個原始碼檔",
                type=list(LANGUAGES),
                accept_multiple_files=True,
                help="支援 .py、.js、.cpp、.cc、.cxx、.c、.h、.hh、.hpp、.hxx。",
            )
            submit_upload = st.form_submit_button("上傳檔案", use_container_width=True)
        if submit_upload:
            if not uploaded_files:
                st.error("請先選取至少一個程式檔。")
            else:
                try:
                    count = upload_files(uploaded_files, parent_id)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"已上傳 {count} 個檔案。")
                    st.rerun()


def render_workspace_sidebar(items, selected_id, configured_password):
    files = [item for item in items if item["kind"] == "file"]
    folders = [item for item in items if item["kind"] == "folder"]
    settings = get_home_settings(items)
    items_by_id = {item["id"]: item for item in items}
    is_editor = bool(st.session_state.get("is_editor"))

    login_sidebar(configured_password)
    if is_editor:
        render_creation_forms(folders)
        render_home_settings(items, settings)
        render_upload_form(None)

    all_tags = sorted({tag for item in files for tag in json.loads(item["tags"])})
    with st.sidebar:
        st.divider()
        search = st.text_input("搜尋程式")
        selected_tags = st.multiselect("依標籤篩選", all_tags)

    visible_files = [
        item
        for item in files
        if search.casefold() in get_path(item, items_by_id).casefold()
        and set(selected_tags).issubset(json.loads(item["tags"]))
    ]
    visible_ids = {item["id"] for item in visible_files}
    selected_item = items_by_id.get(selected_id)
    with st.sidebar:
        st.divider()
        st.subheader("檔案樹狀圖")
        render_file_tree(
            items,
            None,
            selected_id,
            visible_ids,
            get_expanded_folder_ids(selected_item, items_by_id)
            if selected_item
            else set(),
        )
        if not visible_ids and (search or selected_tags):
            st.caption("目前的搜尋或標籤條件沒有符合的程式。")
    return visible_files


def render_editor_navigation(item):
    back_column, _, share_column = st.columns([1, 8, 1])
    with back_column:
        if st.button("←", help="返回首頁", key=f"back_home_{item['id']}"):
            st.query_params.clear()
            st.rerun()
    if item["kind"] != "file":
        return
    with share_column:
        if st.button("分享", key=f"share_{item['id']}"):
            state_key = f"show_share_{item['id']}"
            st.session_state[state_key] = not st.session_state.get(state_key, False)
    state_key = f"show_share_{item['id']}"
    if item["kind"] == "file" and st.session_state.get(state_key):
        st.caption("此連結為唯讀檢視，只會顯示檔案名稱、程式碼與最近編輯時間。")
        st.code(make_share_url(item["id"]), language=None)


def main():
    st.set_page_config(page_title="MY CODE", page_icon="💻", layout="wide")
    initialize_database()
    configured_password = get_configured_password()
    if not configured_password:
        st.error(
            "尚未設定編輯密碼。請在 `.streamlit/secrets.toml` 設定 `edit_password`，"
            "或設定 `APP_EDIT_PASSWORD` 環境變數。"
        )
        st.stop()

    query_share = st.query_params.get("share", "")
    if query_share:
        shared_item = next(
            (
                item
                for item in get_items()
                if item["id"] == query_share and item["kind"] == "file"
            ),
            None,
        )
        if not shared_item:
            st.title("MY CODE")
            st.error("找不到這個分享程式。")
            return
        render_read_only_file(shared_item)
        return

    query_item = st.query_params.get("item", "")
    if query_item:
        items = get_items()
        items_by_id = {item["id"]: item for item in items}
        selected = items_by_id.get(query_item)
        if not selected:
            st.title("MY CODE")
            st.error("找不到這個分享項目。")
            return
        if selected["kind"] == "file":
            if st.session_state.get("is_editor"):
                render_workspace_sidebar(items, selected["id"], configured_password)
                render_editor_navigation(selected)
                st.title(selected["name"])
                render_file_editor(selected)
            else:
                render_read_only_file(selected)
            return

        if st.session_state.get("is_editor"):
            render_workspace_sidebar(items, selected["id"], configured_password)
            render_editor_navigation(selected)
        st.title(selected["name"])
        for child in items_by_id.values():
            if child["parent_id"] == selected["id"]:
                st.write(
                    f"{'📁' if child['kind'] == 'folder' else '📄'} "
                    f"{child['name']}"
                    if child["kind"] == "folder"
                    else f"📄 {child['name']} · {status_label(child['status'])}"
                )
        if st.session_state.get("is_editor"):
            if st.button("刪除資料夾及其所有內容", type="secondary"):
                st.session_state["confirm_delete"] = selected["id"]
            if st.session_state.get("confirm_delete") == selected["id"]:
                st.warning("刪除資料夾也會永久刪除其中的檔案。")
                if st.button("確認永久刪除資料夾", type="primary"):
                    delete_item(selected["id"])
                    st.session_state.pop("confirm_delete", None)
                    st.query_params.clear()
                    st.rerun()
        return

    items = get_items()
    items_by_id = {item["id"]: item for item in items}
    files = [item for item in items if item["kind"] == "file"]
    settings = get_home_settings(items)
    visible_files = render_workspace_sidebar(items, None, configured_password)

    st.title("MY CODE")
    st.header(settings["home_title"])

    pinned_folders = [
        items_by_id[folder_id]
        for folder_id in settings["pinned_folders"]
        if folder_id in items_by_id
    ]
    if pinned_folders:
        st.subheader("釘選資料夾")
        pinned_columns = st.columns(min(len(pinned_folders), 4))
        for index, folder in enumerate(pinned_folders):
            with pinned_columns[index % len(pinned_columns)]:
                if st.button(
                    f"📁 {folder['name']}",
                    key=f"pinned_folder_{folder['id']}",
                    use_container_width=True,
                ):
                    select_item(folder["id"])

    completed_files = [item for item in visible_files if item["status"] == "完成"]
    incomplete_files = [item for item in visible_files if item["status"] != "完成"]
    recent_files = get_recent_files(visible_files)
    completed_column, recent_column, incomplete_column = st.columns([1, 1.25, 1])

    with completed_column:
        st.subheader("✅ 完成")
        if not completed_files:
            st.caption("沒有已完成的程式。")
        for item in completed_files:
            render_home_file(item, items_by_id, "completed")

    with recent_column:
        st.subheader("🕒 近期檔案")
        if not recent_files:
            st.caption("目前沒有符合條件的檔案。")
        for item in recent_files:
            render_home_file(item, items_by_id, "recent")

    with incomplete_column:
        st.subheader("🟡 未完成")
        if not incomplete_files:
            st.caption("沒有待完成的程式。")
        for item in incomplete_files:
            render_home_file(item, items_by_id, "incomplete")

if __name__ == "__main__":
    main()
