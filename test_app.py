import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import app


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "test.db"
        app.initialize_database()

    def tearDown(self):
        app.DB_PATH = self.previous_db_path
        self.temp_dir.cleanup()

    def test_folder_contents_can_be_saved_and_deleted_recursively(self):
        folder_id = app.create_item("folder", "學習")
        file_id = app.create_item(
            "file", "hello.py", folder_id, "Python", "print('hello')", ["basics"]
        )

        app.update_file(
            file_id, "hello.py", "print('updated')", ["basics", "loops"], "未完成"
        )
        saved_file = next(item for item in app.get_items() if item["id"] == file_id)
        self.assertEqual(saved_file["content"], "print('updated')")
        self.assertEqual(saved_file["language"], "Python")
        self.assertEqual(app.get_path(saved_file, {item["id"]: item for item in app.get_items()}), "學習/hello.py")
        self.assertEqual(
            app.get_expanded_folder_ids(saved_file, {item["id"]: item for item in app.get_items()}),
            {folder_id},
        )
        self.assertTrue(saved_file["uploaded_at"])
        self.assertTrue(saved_file["updated_at"])
        history = app.get_edit_history(file_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "print('hello')")

        app.update_file(file_id, "hello.cpp", "int main() {}", ["cpp"], "未完成")
        renamed_file = next(item for item in app.get_items() if item["id"] == file_id)
        self.assertEqual(renamed_file["language"], "C++")
        with self.assertRaisesRegex(ValueError, "副檔名"):
            app.update_file(file_id, "notes.txt", "content", [], "完成")
        with self.assertRaisesRegex(ValueError, "狀態"):
            app.update_file(file_id, "hello.cpp", "int main() {}", ["cpp"], "進行中")

        app.update_file(file_id, "hello.cpp", "int main() { return 0; }", ["cpp"], "完成")
        completed_file = next(item for item in app.get_items() if item["id"] == file_id)
        self.assertEqual(completed_file["status"], "完成")
        self.assertEqual(app.get_edit_history(file_id)[0]["status"], "未完成")
        self.assertEqual(app.status_label(completed_file["status"]), "✅ 完成")

        app.delete_item(folder_id)
        self.assertEqual(app.get_items(), [])
        self.assertEqual(app.get_edit_history(file_id), [])

    def test_names_are_unique_per_folder_and_path_separators_are_rejected(self):
        folder_id = app.create_item("folder", "學習")
        app.create_item("file", "hello.py", folder_id)

        with self.assertRaisesRegex(ValueError, "相同名稱"):
            app.create_item("file", "hello.py", folder_id)
        with self.assertRaisesRegex(ValueError, "名稱不可為空"):
            app.create_item("folder", "../outside")

    def test_tags_and_password_checks(self):
        self.assertEqual(app.normalize_tags(" basics, #Python, basics "), ["Python", "basics"])
        self.assertTrue(app.password_matches("correct", "correct"))
        self.assertFalse(app.password_matches("incorrect", "correct"))

    def test_share_url_uses_read_only_share_route(self):
        self.assertEqual(
            app.make_share_url("file-123", "https://example.com/app?item=old"),
            "https://example.com/app?share=file-123",
        )

    def test_home_settings_pin_folders_and_reject_invalid_values(self):
        folder_id = app.create_item("folder", "我的資料夾")
        items = app.get_items()
        self.assertEqual(
            app.get_home_settings(items),
            {"home_title": app.DEFAULT_HOME_TITLE, "pinned_folders": []},
        )

        app.save_home_settings("我的學習空間", [folder_id], items)
        self.assertEqual(
            app.get_home_settings(items),
            {"home_title": "我的學習空間", "pinned_folders": [folder_id]},
        )
        with self.assertRaisesRegex(ValueError, "不可為空"):
            app.save_home_settings(" ", [], items)
        with self.assertRaisesRegex(ValueError, "現有資料夾"):
            app.save_home_settings("我的學習空間", ["missing"], items)

    def test_home_separates_file_status_and_sorts_recent_files(self):
        completed_id = app.create_item(
            "file", "done.py", language="Python", status="完成"
        )
        app.create_item("file", "todo.cpp", language="C++")
        items = app.get_items()
        files = [item for item in items if item["kind"] == "file"]
        self.assertEqual(
            {item["name"] for item in files if item["status"] == "完成"},
            {"done.py"},
        )
        self.assertEqual(
            app.get_recent_files(files),
            sorted(files, key=lambda item: (item["updated_at"], item["name"].casefold()), reverse=True),
        )
        self.assertEqual(
            next(item for item in files if item["id"] == completed_id)["status"],
            "完成",
        )

    def test_uploads_supported_source_files_and_detects_cpp_language(self):
        uploaded_file = SimpleNamespace(name="main.cpp", getvalue=lambda: b"#include <iostream>\n")

        self.assertEqual(app.upload_files([uploaded_file]), 1)
        saved_file = app.get_items()[0]
        self.assertEqual(saved_file["language"], "C++")
        self.assertEqual(saved_file["content"], "#include <iostream>\n")
        self.assertEqual(app.get_code_language(saved_file), "cpp")

    def test_uploads_reject_unsupported_or_non_utf8_files(self):
        executable = SimpleNamespace(name="program.exe", getvalue=lambda: b"not source")
        invalid_text = SimpleNamespace(name="main.cpp", getvalue=lambda: b"\xff")

        with self.assertRaisesRegex(ValueError, "不支援"):
            app.upload_files([executable])
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            app.upload_files([invalid_text])
        self.assertEqual(app.get_items(), [])

    def test_uploads_reject_duplicates_without_partially_saving_files(self):
        app.create_item("file", "main.cpp")
        uploads = [
            SimpleNamespace(name="new.cpp", getvalue=lambda: b"int main() {}"),
            SimpleNamespace(name="main.cpp", getvalue=lambda: b"int main() {}"),
        ]

        with self.assertRaisesRegex(ValueError, "同名檔案"):
            app.upload_files(uploads)
        self.assertEqual([item["name"] for item in app.get_items()], ["main.cpp"])

    def test_existing_database_receives_uploaded_at_migration(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        app.DB_PATH = legacy_path
        with app.database() as conn:
            conn.execute(
                """
                CREATE TABLE items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    parent_id TEXT,
                    language TEXT,
                    content TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO items VALUES (
                    'legacy', 'file', 'old.py', NULL, 'Python', '', '[]',
                    '2025-01-01T00:00:00+00:00', '2025-02-01T00:00:00+00:00'
                )
                """
            )

        app.initialize_database()
        legacy_file = app.get_items()[0]
        self.assertEqual(legacy_file["uploaded_at"], legacy_file["created_at"])
        self.assertEqual(legacy_file["status"], "未完成")
        self.assertEqual(app.get_edit_history("legacy"), [])


if __name__ == "__main__":
    unittest.main()
