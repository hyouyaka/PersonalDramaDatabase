from __future__ import annotations

import codecs
import copy
import math
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import json
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QDate, QProcess, QRegularExpression, QThread, Qt, Signal
from PySide6.QtGui import QAction, QFont, QFontDatabase, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QInputDialog,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

from upstash_editor import (
    RESOURCE_SPECS,
    CollectionRef,
    LoadedResource,
    SaveResult,
    collection_refs,
    find_list_item_by_identity,
    load_resource,
    save_resource,
)


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "DramasByCV.sqlite"
RANK_SQL_PATH = ROOT / "DramaByCV.rank.sql"
PYTHON_EXE = sys.executable

READ_ONLY_SQL_RE = QRegularExpression(r"^\s*(select|with)\b", QRegularExpression.CaseInsensitiveOption)
BASE_FONT_SIZE = 10
MONO_FONT_SIZE = 10
HEADING_FONT_SIZE = 10
CONTROL_HEIGHT = 30
COMPACT_CONTROL_HEIGHT = 24
BUTTON_PADDING_H = 10
BUTTON_PADDING_V = 4
FIELD_PADDING_H = 8
FIELD_PADDING_V = 4
TAB_PADDING_H = 14
TAB_PADDING_V = 6
SECTION_SPACING = 8
SQL_EDITOR_MIN_HEIGHT = 82
RANK_TITLES = [
    "全平台 全作品",
    "全平台 纯爱",
    "猫耳 纯爱",
    "漫播 纯爱",
    "全平台 去有声剧",
    "全平台 纯爱 去有声剧",
    "猫耳 纯爱 去有声剧",
    "漫播 纯爱 去有声剧",
]


def split_ids(text: str) -> list[str]:
    raw = text.replace("\n", " ").replace("\t", " ").replace("，", ",")
    parts: list[str] = []
    for chunk in raw.split(","):
        for piece in chunk.split():
            item = piece.strip()
            if item and item not in parts:
                parts.append(item)
    return parts


def clean_sql(text: str) -> str:
    sql = text.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def is_read_only_query(sql: str) -> bool:
    normalized = clean_sql(sql)
    if not normalized or ";" in normalized:
        return False
    return READ_ONLY_SQL_RE.match(normalized).hasMatch()


def build_sync_remote_libraries_command() -> list[str]:
    return [PYTHON_EXE, "sync_remote_libraries.py"]


def make_ui_font(size: int = BASE_FONT_SIZE, *, heading: bool = False) -> QFont:
    font = QFont("Segoe UI", size)
    font.setFamilies(["Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "sans-serif"])
    font.setHintingPreference(QFont.PreferFullHinting)
    if heading:
        font.setWeight(QFont.DemiBold)
    return font


def make_mono_font() -> QFont:
    font = QFont("Consolas", MONO_FONT_SIZE)
    mono_families = ["Consolas", "Cascadia Mono", "Microsoft YaHei UI"]
    available = set(QFontDatabase().families())
    font.setFamilies([item for item in mono_families if item in available] or mono_families)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def build_stylesheet() -> str:
    return f"""
    QWidget {{
        font-family: "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
        font-size: {BASE_FONT_SIZE}pt;
    }}
    QGroupBox {{
        font-weight: 600;
        margin-top: {SECTION_SPACING}px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }}
    QPushButton {{
        min-height: {CONTROL_HEIGHT}px;
        padding: {BUTTON_PADDING_V}px {BUTTON_PADDING_H}px;
    }}
    QLineEdit, QComboBox, QDateEdit {{
        min-height: {CONTROL_HEIGHT}px;
        padding: {FIELD_PADDING_V}px {FIELD_PADDING_H}px;
    }}
    QCheckBox {{
        min-height: {COMPACT_CONTROL_HEIGHT}px;
    }}
    QPlainTextEdit {{
        padding: {FIELD_PADDING_V}px;
    }}
    QTabBar::tab {{
        padding: {TAB_PADDING_V}px {TAB_PADDING_H}px;
        min-width: 88px;
    }}
    QHeaderView::section {{
        padding: {FIELD_PADDING_V}px {FIELD_PADDING_H}px;
    }}
    """


def parse_rank_queries(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    parts = text.split("--")
    queries: list[tuple[str, str]] = []
    title_index = 0
    for part in parts:
        chunk = part.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        if not lines:
            continue
        if not lines[0].strip()[:1].isdigit():
            continue
        sql = "\n".join(lines[1:]).strip()
        if not sql:
            continue
        title = RANK_TITLES[title_index] if title_index < len(RANK_TITLES) else lines[0].strip()
        queries.append((title, sql))
        title_index += 1
    return queries


def python_command(command: list[str]) -> list[str]:
    if not command:
        return command
    executable = Path(command[0]).name.lower()
    if executable.startswith("python"):
        return [command[0], "-X", "utf8", *command[1:]]
    return command


class ResultTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.columns: list[str] = []
        self.rows: list[tuple[object, ...]] = []

    def set_result(self, columns: list[str], rows: list[tuple[object, ...]]) -> None:
        self.beginResetModel()
        self.columns = columns
        self.rows = rows
        self.endResetModel()

    def clear(self) -> None:
        self.set_result([], [])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
        if not index.isValid():
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            value = self.rows[index.row()][index.column()]
            return "" if value is None else str(value)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> object:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal and 0 <= section < len(self.columns):
            return self.columns[section]
        if orientation == Qt.Vertical:
            return section + 1
        return None


class ResultTableView(QTableView):
    def __init__(self) -> None:
        super().__init__()
        self.setSelectionBehavior(QTableView.SelectItems)
        self.setSelectionMode(QTableView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setSortingEnabled(False)
        self.setCornerButtonEnabled(False)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)

    def apply_density(self) -> None:
        table_font = make_ui_font(BASE_FONT_SIZE)
        header_font = make_ui_font(HEADING_FONT_SIZE, heading=True)
        self.setFont(table_font)
        self.horizontalHeader().setFont(header_font)
        self.verticalHeader().setFont(table_font)

        table_metrics = self.fontMetrics()
        header_metrics = self.horizontalHeader().fontMetrics()
        row_height = math.ceil(table_metrics.height() * 1.35 + FIELD_PADDING_V * 2)
        header_height = math.ceil(header_metrics.height() * 1.4 + BUTTON_PADDING_V * 2)
        self.verticalHeader().setDefaultSectionSize(row_height)
        self.verticalHeader().setMinimumSectionSize(row_height)
        self.horizontalHeader().setFixedHeight(header_height)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.Copy):
            self.copy_selection()
            return
        super().keyPressEvent(event)

    def selected_text(self) -> str:
        indexes = self.selectedIndexes()
        if not indexes:
            return ""
        ordered = sorted(indexes, key=lambda item: (item.row(), item.column()))
        lines: list[str] = []
        current_row = ordered[0].row()
        current_parts: list[str] = []
        for index in ordered:
            if index.row() != current_row:
                lines.append("\t".join(current_parts))
                current_row = index.row()
                current_parts = []
            current_parts.append(index.data(Qt.DisplayRole) or "")
        if current_parts:
            lines.append("\t".join(current_parts))
        return "\n".join(lines)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def show_context_menu(self, position) -> None:
        menu = QMenu(self)
        copy_action = QAction("复制选中内容", self)
        copy_action.triggered.connect(self.copy_selection)
        menu.addAction(copy_action)
        menu.exec(self.viewport().mapToGlobal(position))


class QueryWorker(QThread):
    succeeded = Signal(object, object, object, float)
    failed = Signal(str)

    def __init__(self, db_path: Path, base_sql: str, count_sql: str | None, page_size: int | None, page_index: int = 0) -> None:
        super().__init__()
        self.db_path = db_path
        self.base_sql = base_sql
        self.count_sql = count_sql
        self.page_size = page_size
        self.page_index = page_index

    def run(self) -> None:
        started = time.perf_counter()
        try:
            conn = sqlite3.connect(self.db_path)
            total_rows: int | None = None
            if self.count_sql:
                total_rows = int(conn.execute(self.count_sql).fetchone()[0])
            sql = self.base_sql
            if self.page_size is not None:
                offset = self.page_index * self.page_size
                sql = f"SELECT * FROM ({self.base_sql}) LIMIT {self.page_size} OFFSET {offset}"
            cursor = conn.execute(sql)
            rows = [tuple(row) for row in cursor.fetchall()]
            columns = [item[0] for item in (cursor.description or [])]
            conn.close()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(columns, rows, total_rows, time.perf_counter() - started)


class SQLitePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.worker: QueryWorker | None = None
        self.current_page = 0
        self.total_rows: int | None = None
        self.last_base_sql = ""
        self.last_count_sql: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        controls_box = QGroupBox("SQLite 浏览")
        controls = QGridLayout(controls_box)
        root.addWidget(controls_box)

        self.table_box = QComboBox()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("按标题 / CV / dramaids / drama_id 模糊筛选")
        self.page_size_box = QComboBox()
        self.page_size_box.addItems(["100", "200", "500"])
        self.page_size_box.setCurrentText("200")
        self.load_button = QPushButton("加载表")
        self.execute_button = QPushButton("执行 SQL")
        self.prev_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 页")
        self.result_label = QLabel("SQLite 浏览就绪")

        controls.addWidget(QLabel("表"), 0, 0)
        controls.addWidget(self.table_box, 0, 1)
        controls.addWidget(QLabel("筛选"), 0, 2)
        controls.addWidget(self.filter_edit, 0, 3, 1, 3)
        controls.addWidget(QLabel("每页"), 0, 6)
        controls.addWidget(self.page_size_box, 0, 7)
        controls.addWidget(self.load_button, 0, 8)
        controls.addWidget(self.execute_button, 0, 9)
        controls.addWidget(self.result_label, 1, 0, 1, 6)
        controls.addWidget(self.page_label, 1, 6, 1, 2)
        controls.addWidget(self.prev_button, 1, 8)
        controls.addWidget(self.next_button, 1, 9)
        controls.setColumnStretch(3, 1)

        self.query_edit = QPlainTextEdit()
        self.query_edit.setPlaceholderText("输入只读 SQL，例如 SELECT * FROM cv_works ORDER BY COALESCE(total_play_count, 0) DESC")
        self.query_edit.setFixedHeight(SQL_EDITOR_MIN_HEIGHT)
        root.addWidget(self.query_edit)

        self.table_view = ResultTableView()
        self.model = ResultTableModel()
        self.table_view.setModel(self.model)
        root.addWidget(self.table_view, stretch=1)

        self.load_button.clicked.connect(self.load_table)
        self.execute_button.clicked.connect(self.execute_raw_query)
        self.prev_button.clicked.connect(self.prev_page)
        self.next_button.clicked.connect(self.next_page)
        self.filter_edit.returnPressed.connect(self.load_table)
        self.page_size_box.currentTextChanged.connect(self.reload_current_page)

        self.refresh_tables()
        self.apply_density()
        self.load_table()

    def apply_density(self) -> None:
        self.query_edit.setFont(make_mono_font())
        self.query_edit.setMinimumHeight(SQL_EDITOR_MIN_HEIGHT)
        self.table_view.apply_density()

    def set_busy(self, busy: bool) -> None:
        for widget in [self.table_box, self.filter_edit, self.page_size_box, self.load_button, self.execute_button, self.prev_button, self.next_button, self.query_edit]:
            widget.setEnabled(not busy)

    def refresh_tables(self) -> None:
        if not DB_PATH.exists():
            self.table_box.clear()
            self.result_label.setText("未找到 DramasByCV.sqlite")
            return
        conn = sqlite3.connect(DB_PATH)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        conn.close()
        self.table_box.clear()
        self.table_box.addItems(tables)
        if tables:
            self.table_box.setCurrentText("cv_works" if "cv_works" in tables else tables[0])

    def page_size(self) -> int:
        return int(self.page_size_box.currentText())

    def build_generated_queries(self) -> tuple[str, str]:
        table = self.table_box.currentText().strip()
        where_parts: list[str] = []
        filter_text = self.filter_edit.text().strip().replace("'", "''")
        if filter_text:
            like = f"'%{filter_text}%'"
            if table == "cv_works":
                where_parts.append(f"(title LIKE {like} OR cv_name LIKE {like} OR dramaids_text LIKE {like})")
            else:
                where_parts.append(f"drama_id LIKE {like}")
        where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        if table == "cv_works":
            base_sql = f"SELECT * FROM {table}{where_clause} ORDER BY COALESCE(total_play_count, 0) DESC, title, cv_name"
        else:
            base_sql = f"SELECT * FROM {table}{where_clause} ORDER BY ROWID DESC"
        count_sql = f"SELECT COUNT(*) FROM {table}{where_clause}"
        return base_sql, count_sql

    def load_table(self) -> None:
        if not DB_PATH.exists():
            QMessageBox.critical(self, "错误", "未找到 DramasByCV.sqlite")
            return
        self.current_page = 0
        base_sql, count_sql = self.build_generated_queries()
        self.query_edit.setPlainText(base_sql)
        self.start_query(base_sql, count_sql)

    def execute_raw_query(self) -> None:
        if not DB_PATH.exists():
            QMessageBox.critical(self, "错误", "未找到 DramasByCV.sqlite")
            return
        sql = clean_sql(self.query_edit.toPlainText())
        if not is_read_only_query(sql):
            QMessageBox.warning(self, "仅支持只读查询", "SQLite 页只允许 SELECT / WITH 开头的只读 SQL。")
            return
        self.current_page = 0
        self.start_query(sql, f"SELECT COUNT(*) FROM ({sql})")

    def reload_current_page(self) -> None:
        if self.last_base_sql and self.worker is None:
            self.current_page = 0
            self.start_query(self.last_base_sql, self.last_count_sql)

    def prev_page(self) -> None:
        if self.current_page <= 0 or self.worker is not None:
            return
        self.current_page -= 1
        self.start_query(self.last_base_sql, self.last_count_sql)

    def next_page(self) -> None:
        if self.worker is not None:
            return
        if self.total_rows is not None and (self.current_page + 1) * self.page_size() >= self.total_rows:
            return
        self.current_page += 1
        self.start_query(self.last_base_sql, self.last_count_sql)

    def start_query(self, base_sql: str, count_sql: str | None) -> None:
        if self.worker is not None:
            return
        self.last_base_sql = base_sql
        self.last_count_sql = count_sql
        self.result_label.setText("正在查询 SQLite...")
        self.page_label.setText(f"第 {self.current_page + 1} 页")
        self.set_busy(True)
        self.worker = QueryWorker(DB_PATH, base_sql, count_sql, self.page_size(), self.current_page)
        self.worker.succeeded.connect(self.on_query_success)
        self.worker.failed.connect(self.on_query_error)
        self.worker.finished.connect(self.on_query_finished)
        self.worker.start()

    def on_query_success(self, columns: list[str], rows: list[tuple[object, ...]], total_rows: int | None, elapsed: float) -> None:
        self.total_rows = total_rows
        self.model.set_result(columns, rows)
        self.table_view.resizeColumnsToContents()
        shown = len(rows)
        if total_rows is None:
            self.result_label.setText(f"查询完成，当前页 {shown} 行，用时 {elapsed:.2f}s")
        else:
            start = self.current_page * self.page_size() + 1 if shown else 0
            end = self.current_page * self.page_size() + shown
            self.result_label.setText(f"查询完成，显示 {start}-{end} / {total_rows} 行，用时 {elapsed:.2f}s")

    def on_query_error(self, message: str) -> None:
        self.result_label.setText("查询失败")
        QMessageBox.critical(self, "SQL 错误", message)

    def on_query_finished(self) -> None:
        self.worker = None
        self.set_busy(False)


class RankPreviewPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.worker: QueryWorker | None = None
        self.queries = parse_rank_queries(RANK_SQL_PATH)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        controls_box = QGroupBox("榜单预览")
        controls = QGridLayout(controls_box)
        root.addWidget(controls_box)

        self.rank_box = QComboBox()
        self.refresh_button = QPushButton("刷新当前榜单")
        self.result_label = QLabel("榜单预览就绪")
        for title, _sql in self.queries:
            self.rank_box.addItem(title)

        controls.addWidget(QLabel("榜单"), 0, 0)
        controls.addWidget(self.rank_box, 0, 1)
        controls.addWidget(self.refresh_button, 0, 2)
        controls.addWidget(self.result_label, 1, 0, 1, 3)
        controls.setColumnStretch(1, 1)

        self.table_view = ResultTableView()
        self.model = ResultTableModel()
        self.table_view.setModel(self.model)
        root.addWidget(self.table_view, stretch=1)

        self.refresh_button.clicked.connect(self.load_selected_rank)
        self.rank_box.currentIndexChanged.connect(self.load_selected_rank)
        self.apply_density()
        if self.queries:
            self.load_selected_rank()
        else:
            self.result_label.setText("未找到榜单 SQL")

    def apply_density(self) -> None:
        self.table_view.apply_density()

    def set_busy(self, busy: bool) -> None:
        self.rank_box.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)

    def load_selected_rank(self) -> None:
        if self.worker is not None:
            return
        if not DB_PATH.exists():
            self.result_label.setText("未找到 DramasByCV.sqlite")
            return
        index = self.rank_box.currentIndex()
        if index < 0 or index >= len(self.queries):
            return
        _title, sql = self.queries[index]
        self.result_label.setText("正在查询榜单...")
        self.set_busy(True)
        self.worker = QueryWorker(DB_PATH, sql, None, None)
        self.worker.succeeded.connect(self.on_query_success)
        self.worker.failed.connect(self.on_query_error)
        self.worker.finished.connect(self.on_query_finished)
        self.worker.start()

    def on_query_success(self, columns: list[str], rows: list[tuple[object, ...]], _total_rows: int | None, elapsed: float) -> None:
        self.model.set_result(columns, rows)
        self.table_view.resizeColumnsToContents()
        self.result_label.setText(f"榜单加载完成，共 {len(rows)} 行，用时 {elapsed:.2f}s")

    def on_query_error(self, message: str) -> None:
        self.result_label.setText("榜单查询失败")
        QMessageBox.critical(self, "榜单错误", message)

    def on_query_finished(self) -> None:
        self.worker = None
        self.set_busy(False)


class JSONSearchWorker(QThread):
    succeeded = Signal(object, object, object, object, float)
    failed = Signal(str)

    def __init__(self, file_path: Path, query: str, use_regex: bool = False, limit: int = 2000) -> None:
        super().__init__()
        self.file_path = file_path
        self.query = query
        self.use_regex = use_regex
        self.limit = limit

    def run(self) -> None:
        started = time.perf_counter()
        try:
            text = self.file_path.read_text(encoding="utf-8")
            data = json.loads(text)
            items = []
            source_info_list: list[dict] = []

            if isinstance(data, dict):
                # try common mapping structures
                if "works" in data and isinstance(data["works"], list):
                    for idx, item in enumerate(data["works"]):
                        items.append(item)
                        source_info_list.append({"location": "list_in_dict", "key": "works", "index": idx})
                elif "records" in data and isinstance(data["records"], list):
                    for idx, item in enumerate(data["records"]):
                        items.append(item)
                        source_info_list.append({"location": "list_in_dict", "key": "records", "index": idx})
                elif "counts" in data and isinstance(data["counts"], dict):
                    # Special handling for structures like {_meta: {...}, counts: {id: {...}}}
                    for drama_id, record in data["counts"].items():
                        if isinstance(record, dict):
                            item = dict(record)
                            item.setdefault("dramaId", drama_id)
                            items.append(item)
                            source_info_list.append({"location": "counts", "dramaId": drama_id})
                        else:
                            items.append({"dramaId": drama_id, "value": record})
                            source_info_list.append({"location": "counts", "dramaId": drama_id})
                else:
                    # If the JSON is a mapping {dramaId: {...}} or {title: {season: {...}}}, try to normalize
                    for k, v in data.items():
                        # Skip metadata keys
                        if k.startswith("_"):
                            continue
                        if isinstance(v, dict):
                            # Check if this is a nested structure (e.g., {season1: {...}, season2: {...}})
                            # by seeing if all values are dicts with common drama info keys
                            has_nested_seasons = all(
                                isinstance(val, dict) and any(key in val for key in ["title", "dramaId", "drama_id", "id", "soundIds"])
                                for val in v.values()
                            )
                            if has_nested_seasons:
                                # Flatten nested structure (e.g., missevan-drama-info)
                                for season_key, season_data in v.items():
                                    if isinstance(season_data, dict):
                                        item = dict(season_data)
                                        if not (item.get("dramaId") or item.get("drama_id") or item.get("id")):
                                            item["dramaId"] = k
                                        items.append(item)
                                        source_info_list.append({
                                            "location": "nested_season",
                                            "title": k,
                                            "season": season_key,
                                            "dramaId": item.get("dramaId") or item.get("drama_id") or item.get("id") or k
                                        })
                            else:
                                # Single-level dict
                                item = dict(v)
                                if not (item.get("dramaId") or item.get("drama_id") or item.get("id")):
                                    item["dramaId"] = k
                                items.append(item)
                                source_info_list.append({
                                    "location": "direct_dict",
                                    "dramaId": k
                                })
                        elif isinstance(v, list):
                            # flatten lists under keys like 'records'
                            for sub in v:
                                if isinstance(sub, dict):
                                    s = dict(sub)
                                    if not (s.get("dramaId") or s.get("drama_id") or s.get("id")):
                                        s.setdefault("dramaId", k)
                                    items.append(s)
                                    source_info_list.append({
                                        "location": "nested_list",
                                        "key": k,
                                        "dramaId": s.get("dramaId") or s.get("drama_id") or s.get("id") or ""
                                    })
                                else:
                                    items.append(sub)
                                    source_info_list.append({"location": "nested_list", "key": k})
                        else:
                            # scalar value, wrap with source key
                            items.append({"_key": k, "value": v})
                            source_info_list.append({"location": "scalar", "key": k})
            elif isinstance(data, list):
                for idx, item in enumerate(data):
                    items.append(item)
                    source_info_list.append({"location": "list", "index": idx})
            else:
                items = [data]
                source_info_list = [{"location": "root"}]

            rows = []
            full_items: list[object] = []
            search_source_info: list[dict] = []
            q = self.query or ""
            q_lower = q.lower()
            regex = None
            if self.use_regex and q:
                try:
                    regex = re.compile(q)
                except Exception as exc:
                    self.failed.emit(f"正则编译错误: {exc}")
                    return

            count = 0
            for idx, item in enumerate(items):
                if count >= self.limit:
                    break
                # build preview string
                preview = ""
                if isinstance(item, dict):
                    preview = (item.get("title") or item.get("name") or "")
                    if not preview:
                        preview = str(item.get("dramaId") or item.get("drama_id") or item.get("id") or "")
                    if not preview:
                        preview = json.dumps(item, ensure_ascii=False)[:200]
                else:
                    preview = str(item)[:200]

                hay = (preview + json.dumps(item, ensure_ascii=False))
                matched = False
                if regex is not None:
                    if regex.search(hay):
                        matched = True
                else:
                    if q_lower in hay.lower():
                        matched = True

                if matched:
                    drama_id = ""
                    if isinstance(item, dict):
                        drama_id = str(
                            item.get("dramaId")
                            or item.get("drama_id")
                            or item.get("id")
                            or item.get("_key")
                            or ""
                        )
                    rows.append((idx + 1, drama_id, preview))
                    full_items.append(item)
                    search_source_info.append(source_info_list[idx])
                    count += 1

            columns = ["#", "dramaId", "Preview"]
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(columns, rows, full_items, search_source_info, time.perf_counter() - started)


class JSONBrowserPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.worker: JSONSearchWorker | None = None
        self.full_items: list[object] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        controls_box = QGroupBox("JSON 浏览")
        controls = QGridLayout(controls_box)
        root.addWidget(controls_box)

        self.file_box = QComboBox()
        self.refresh_files_button = QPushButton("刷新文件列表")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索（子串或正则）")
        self.regex_checkbox = QCheckBox("正则")
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清除")

        controls.addWidget(QLabel("文件"), 0, 0)
        controls.addWidget(self.file_box, 0, 1)
        controls.addWidget(self.refresh_files_button, 0, 2)
        controls.addWidget(QLabel("搜索"), 1, 0)
        controls.addWidget(self.search_edit, 1, 1)
        controls.addWidget(self.regex_checkbox, 1, 2)
        controls.addWidget(self.search_button, 1, 3)
        controls.addWidget(self.clear_button, 1, 4)
        controls.setColumnStretch(1, 1)

        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, stretch=1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.table_view = ResultTableView()
        self.model = ResultTableModel()
        self.table_view.setModel(self.model)
        left_layout.addWidget(self.table_view)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(False)
        self.save_button = QPushButton("保存到文件")
        self.revert_button = QPushButton("还原")
        right_layout.addWidget(self.save_button)
        right_layout.addWidget(self.revert_button)
        right_layout.addWidget(self.detail_view, stretch=1)

        split.addWidget(left_panel)
        split.addWidget(right_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        self.refresh_files_button.clicked.connect(self.populate_json_files)
        self.search_button.clicked.connect(self.on_search)
        self.clear_button.clicked.connect(self.on_clear)
        self.table_view.clicked.connect(self.on_row_clicked)
        self.save_button.clicked.connect(self.on_save)
        self.revert_button.clicked.connect(self.on_revert)

        self.populate_json_files()

        self.apply_density()

    def apply_density(self) -> None:
        self.detail_view.setFont(make_mono_font())
        self.table_view.apply_density()

    def set_busy(self, busy: bool) -> None:
        for widget in [self.file_box, self.refresh_files_button, self.search_edit, self.regex_checkbox, self.search_button, self.clear_button, self.save_button, self.revert_button]:
            try:
                widget.setEnabled(not busy)
            except Exception:
                pass

    def on_choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 JSON 文件", str(ROOT), "JSON Files (*.json);;All Files (*)")
        if path:
            # set dropdown to the selected filename if it's in ROOT
            p = Path(path)
            if p.parent == ROOT:
                name = p.name
            else:
                name = p.name
            idx = self.file_box.findText(name)
            if idx >= 0:
                self.file_box.setCurrentIndex(idx)
            else:
                # add to box
                self.file_box.addItem(name)
                self.file_box.setCurrentIndex(self.file_box.count() - 1)

    def on_search(self) -> None:
        path_text = self.file_box.currentText().strip()
        if not path_text:
            QMessageBox.warning(self, "缺少文件", "请选择要打开的 JSON 文件")
            return
        query = self.search_edit.text().strip()
        use_regex = self.regex_checkbox.isChecked()
        if self.worker is not None:
            return
        file_path = ROOT / path_text
        if not file_path.exists():
            QMessageBox.critical(self, "文件不存在", "选择的文件不存在")
            return
        self.set_busy(True)
        self.detail_view.clear()
        self.model.clear()
        self.full_items = []
        self.worker = JSONSearchWorker(file_path, query, use_regex)
        self.worker.succeeded.connect(self.on_search_success)
        self.worker.failed.connect(self.on_search_failed)
        self.worker.finished.connect(self.on_search_finished)
        self.worker.start()

    def on_clear(self) -> None:
        self.file_box.setCurrentIndex(-1)
        self.search_edit.clear()
        self.model.clear()
        self.detail_view.clear()
        self.full_items = []

    def populate_json_files(self) -> None:
        self.file_box.clear()
        try:
            files = sorted([p.name for p in ROOT.glob("*.json")])
            self.file_box.addItems(files)
            if files:
                self.file_box.setCurrentIndex(0)
        except Exception:
            pass

    def on_search_success(self, columns: list[str], rows: list[tuple[object, ...]], full_items: list[object], source_info_list: list[dict], elapsed: float) -> None:
        self.full_items = full_items
        self._items_source_info = {i: info for i, info in enumerate(source_info_list)}
        self.model.set_result(columns, rows)
        self.table_view.resizeColumnsToContents()
        self.detail_view.setPlainText(f"搜索完成，共 {len(rows)} 项，用时 {elapsed:.2f}s")

    def on_search_failed(self, message: str) -> None:
        QMessageBox.critical(self, "搜索失败", message)

    def on_search_finished(self) -> None:
        self.worker = None
        self.set_busy(False)

    def on_row_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if row < 0 or row >= len(self.full_items):
            return
        item = self.full_items[row]
        # store original source index (first column is 1-based index into source list)
        try:
            idx_value = int(self.model.rows[row][0]) - 1
        except Exception:
            idx_value = None
        self._current_source_index = idx_value

        # Store source information for saving back to file
        # This will be populated by the search worker
        if not hasattr(self, "_items_source_info"):
            self._items_source_info = {}

        source_info = self._items_source_info.get(row, {})
        self._current_item_source = source_info

        try:
            pretty = json.dumps(item, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(item)
        self.detail_view.setPlainText(pretty)

    def on_save(self) -> None:
        # Save edited JSON for the selected item back to file
        if not hasattr(self, "_current_source_index") or self._current_source_index is None:
            QMessageBox.warning(self, "未选择条目", "请先在结果表中选择要保存的条目")
            return
        file_text = self.file_box.currentText().strip()
        if not file_text:
            QMessageBox.warning(self, "缺少文件", "请选择要保存的目标 JSON 文件")
            return
        file_path = ROOT / file_text
        if not file_path.exists():
            QMessageBox.critical(self, "文件不存在", "目标文件不存在")
            return
        try:
            new_text = self.detail_view.toPlainText()
            new_obj = json.loads(new_text)
        except Exception as exc:
            QMessageBox.critical(self, "JSON 解析失败", f"无法解析编辑内容: {exc}")
            return
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            QMessageBox.critical(self, "读取文件失败", str(exc))
            return

        # Store the original item for tracking the source location
        if not hasattr(self, "_current_item_source") or self._current_item_source is None:
            QMessageBox.warning(self, "源信息丢失", "无法确定编辑项的来源位置")
            return

        source_info = self._current_item_source
        success = False

        try:
            if isinstance(data, list):
                # Top-level list structure
                idx = self._current_source_index
                if idx < 0 or idx >= len(data):
                    QMessageBox.critical(self, "索引错误", "源文件中的索引超出范围")
                    return
                data[idx] = new_obj
                success = True
            elif isinstance(data, dict):
                # Dictionary structure - need to find and update the item
                if source_info.get("location") == "list_in_dict":
                    # Handle {records: [...]} structure or similar
                    key = source_info.get("key")
                    idx = source_info.get("index")
                    if not key or key not in data or not isinstance(data[key], list):
                        QMessageBox.critical(self, "索引错误", f"无法在文件中找到 '{key}' 列表")
                        return
                    if idx < 0 or idx >= len(data[key]):
                        QMessageBox.critical(self, "索引错误", "源文件中的索引超出范围")
                        return
                    data[key][idx] = new_obj
                    success = True
                elif source_info.get("location") == "counts":
                    # Handle {_meta, counts: {id: {...}}} structure
                    drama_id = source_info.get("dramaId")
                    if not drama_id or drama_id not in data.get("counts", {}):
                        QMessageBox.critical(self, "索引错误", "无法在文件中找到对应项")
                        return
                    data["counts"][drama_id] = new_obj
                    success = True
                elif source_info.get("location") == "nested_season":
                    # Handle {title: {season: {...}}} structure
                    title = source_info.get("title")
                    season = source_info.get("season")
                    if not (title and season and title in data and season in data[title]):
                        QMessageBox.critical(self, "索引错误", "无法在文件中找到对应项")
                        return
                    data[title][season] = new_obj
                    success = True
                elif source_info.get("location") == "direct_dict":
                    # Handle {id: {...}} structure
                    drama_id = source_info.get("dramaId")
                    if not drama_id or drama_id not in data:
                        QMessageBox.critical(self, "索引错误", "无法在文件中找到对应项")
                        return
                    data[drama_id] = new_obj
                    success = True

            if not success:
                QMessageBox.warning(self, "不支持的文件结构", "无法确定如何保存到此文件结构")
                return

            file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            QMessageBox.information(self, "保存成功", "已保存到文件")
            self.on_search()
        except Exception as exc:
            QMessageBox.critical(self, "写入失败", str(exc))

    def on_revert(self) -> None:
        # revert detail view to the last loaded item
        if not hasattr(self, "_current_source_index") or self._current_source_index is None:
            return
        row = None
        # find in model the matching row with the same source index
        for r_idx, rowdata in enumerate(self.model.rows):
            try:
                source_idx = int(rowdata[0]) - 1
            except Exception:
                continue
            if source_idx == self._current_source_index:
                row = r_idx
                break
        if row is None:
            return
        item = self.full_items[row]
        try:
            pretty = json.dumps(item, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(item)
        self.detail_view.setPlainText(pretty)
        # Also restore source info
        if hasattr(self, "_items_source_info") and row in self._items_source_info:
            self._current_item_source = self._items_source_info[row]


class UpstashResourceWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        operation: str,
        *,
        key: str | None = None,
        loaded: LoadedResource | None = None,
        payload: object = None,
    ) -> None:
        super().__init__()
        self.operation = operation
        self.key = key
        self.loaded = loaded
        self.payload = payload

    def run(self) -> None:
        try:
            from sync_new_drama_ids import load_env_file, upstash_request

            load_env_file(ROOT / ".env")
            if self.operation == "load":
                if not self.key:
                    raise RuntimeError("Missing Upstash key.")
                result = load_resource(self.key, upstash=upstash_request)
            elif self.operation == "save":
                if self.loaded is None:
                    raise RuntimeError("No Upstash resource is loaded.")
                result = save_resource(self.loaded, self.payload, upstash=upstash_request)
            else:
                raise RuntimeError(f"Unsupported Upstash worker operation: {self.operation}")
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class UpstashEditorPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.worker: UpstashResourceWorker | None = None
        self.loaded: LoadedResource | None = None
        self.working_payload: object = None
        self.original_payload: object = None
        self.collections: list[CollectionRef] = []
        self.visible_items: list[tuple[object, object]] = []
        self.current_identity: object = None
        self.current_collection_index: int | None = None
        self.dirty = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        remote_box = QGroupBox("Upstash v2 权威数据")
        remote_layout = QGridLayout(remote_box)
        self.key_box = QComboBox()
        self.key_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.key_box.setMinimumContentsLength(28)
        for spec in RESOURCE_SPECS.values():
            self.key_box.addItem(f"{spec.label}  ·  {spec.key}", spec.key)
        self.load_button = QPushButton("载入并备份")
        self.save_remote_button = QPushButton("保存到 Upstash")
        self.resource_status = QLabel("尚未载入远端数据")
        self.resource_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.resource_status.setWordWrap(True)
        self.resource_status.setMinimumWidth(0)
        self.resource_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        remote_layout.addWidget(QLabel("资源"), 0, 0)
        remote_layout.addWidget(self.key_box, 0, 1)
        remote_layout.addWidget(self.load_button, 0, 2)
        remote_layout.addWidget(self.save_remote_button, 0, 3)
        remote_layout.addWidget(self.resource_status, 1, 0, 1, 4)
        remote_layout.setColumnStretch(1, 1)
        root.addWidget(remote_box)

        controls_box = QGroupBox("条目检索")
        controls = QGridLayout(controls_box)
        self.collection_box = QComboBox()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按 ID、标题、CV 或 JSON 内容搜索")
        self.regex_checkbox = QCheckBox("正则")
        self.search_button = QPushButton("筛选")
        self.clear_search_button = QPushButton("清除")
        controls.addWidget(QLabel("集合"), 0, 0)
        controls.addWidget(self.collection_box, 0, 1)
        controls.addWidget(QLabel("搜索"), 0, 2)
        controls.addWidget(self.search_edit, 0, 3)
        controls.addWidget(self.regex_checkbox, 0, 4)
        controls.addWidget(self.search_button, 0, 5)
        controls.addWidget(self.clear_search_button, 0, 6)
        controls.setColumnStretch(3, 1)
        root.addWidget(controls_box)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        left_panel = QWidget()
        left_panel.setMinimumWidth(0)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.table_view = ResultTableView()
        self.model = ResultTableModel()
        self.table_view.setModel(self.model)
        left_layout.addWidget(self.table_view)

        right_panel = QWidget()
        right_panel.setMinimumWidth(0)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        action_row = QGridLayout()
        self.apply_button = QPushButton("应用条目修改")
        self.new_button = QPushButton("新增")
        self.delete_button = QPushButton("删除")
        self.revert_button = QPushButton("还原条目")
        action_row.addWidget(self.apply_button, 0, 0)
        action_row.addWidget(self.new_button, 0, 1)
        action_row.addWidget(self.delete_button, 0, 2)
        action_row.addWidget(self.revert_button, 0, 3)
        self.detail_view = QPlainTextEdit()
        self.detail_view.setPlaceholderText("从左侧选择条目，或点击“新增”。")
        right_layout.addLayout(action_row)
        right_layout.addWidget(self.detail_view, stretch=1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.load_button.clicked.connect(self.load_selected_resource)
        self.save_remote_button.clicked.connect(self.save_remote)
        self.collection_box.currentIndexChanged.connect(self.on_collection_changed)
        self.search_button.clicked.connect(self.refresh_items)
        self.search_edit.returnPressed.connect(self.refresh_items)
        self.clear_search_button.clicked.connect(self.clear_search)
        self.table_view.clicked.connect(self.on_row_clicked)
        self.apply_button.clicked.connect(self.apply_current_edit)
        self.new_button.clicked.connect(self.add_item)
        self.delete_button.clicked.connect(self.delete_item)
        self.revert_button.clicked.connect(self.revert_item)
        self.detail_view.textChanged.connect(self.on_detail_changed)
        self.apply_density()
        self.set_editor_enabled(False)

    def apply_density(self) -> None:
        self.table_view.apply_density()
        self.detail_view.setFont(make_mono_font())

    def set_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.collection_box,
            self.search_edit,
            self.regex_checkbox,
            self.search_button,
            self.clear_search_button,
            self.table_view,
            self.apply_button,
            self.new_button,
            self.delete_button,
            self.revert_button,
            self.detail_view,
            self.save_remote_button,
        ):
            widget.setEnabled(enabled)

    def set_busy(self, busy: bool) -> None:
        self.key_box.setEnabled(not busy)
        self.load_button.setEnabled(not busy)
        if busy:
            self.set_editor_enabled(False)
        else:
            self.set_editor_enabled(self.loaded is not None)

    def load_selected_resource(self) -> None:
        if self.worker is not None:
            return
        if self.dirty:
            answer = QMessageBox.question(
                self,
                "放弃未保存修改",
                "当前资源有尚未保存到 Upstash 的修改，确定重新载入吗？",
            )
            if answer != QMessageBox.Yes:
                return
        key = self.key_box.currentData()
        if not key:
            QMessageBox.warning(self, "缺少资源", "请选择要载入的 Upstash 资源。")
            return
        self.resource_status.setText(f"正在载入并备份 {key} ...")
        self.set_busy(True)
        self.worker = UpstashResourceWorker("load", key=str(key))
        self.worker.succeeded.connect(self.on_load_success)
        self.worker.failed.connect(self.on_worker_error)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def on_load_success(self, result: object) -> None:
        if not isinstance(result, LoadedResource):
            self.on_worker_error("载入结果类型错误。")
            return
        self.loaded = result
        self.working_payload = copy.deepcopy(result.payload)
        self.original_payload = copy.deepcopy(result.payload)
        self.dirty = False
        self.current_identity = None
        self.current_collection_index = None
        self.detail_view.blockSignals(True)
        self.detail_view.clear()
        self.detail_view.blockSignals(False)
        self.collections = collection_refs(result.spec, self.working_payload)
        self.collection_box.blockSignals(True)
        self.collection_box.clear()
        self.collection_box.addItems([collection.name for collection in self.collections])
        self.collection_box.blockSignals(False)
        if self.collections:
            self.collection_box.setCurrentIndex(0)
        self.refresh_items()
        updated_at = result.updated_at or "未知"
        self.resource_status.setText(
            f"{result.spec.key} · {result.spec.redis_type} · {result.byte_count:,} bytes · "
            f"SHA1 {result.content_sha1} · 更新时间 {updated_at} · "
            f"Meta {result.meta_status} · 备份 {result.backup_path}"
        )

    def on_worker_error(self, message: str) -> None:
        QMessageBox.critical(self, "Upstash 操作失败", message)
        if self.worker is not None and self.worker.operation == "load" and self.loaded is not None:
            previous_index = self.key_box.findData(self.loaded.spec.key)
            if previous_index >= 0:
                self.key_box.setCurrentIndex(previous_index)
            self.resource_status.setText(
                f"载入失败：{message}；继续显示原资源 {self.loaded.spec.key}"
            )
        else:
            self.resource_status.setText(f"失败：{message}")

    def on_worker_finished(self) -> None:
        self.worker = None
        self.set_busy(False)

    def clear_search(self) -> None:
        self.search_edit.clear()
        self.regex_checkbox.setChecked(False)
        self.refresh_items()

    def current_collection(self) -> CollectionRef | None:
        index = self.collection_box.currentIndex()
        if 0 <= index < len(self.collections):
            return self.collections[index]
        return None

    def on_collection_changed(self, _index: int) -> None:
        self.current_identity = None
        self.current_collection_index = None
        self.detail_view.blockSignals(True)
        self.detail_view.clear()
        self.detail_view.blockSignals(False)
        self.apply_button.setText("应用条目修改")
        self.refresh_items()

    def _collection_entries(self, collection: CollectionRef) -> list[tuple[object, object]]:
        if isinstance(collection.container, dict):
            return list(collection.container.items())
        return list(enumerate(collection.container))

    def _display_identity(self, collection: CollectionRef, identity: object, item: object) -> str:
        if isinstance(collection.container, dict):
            return str(identity)
        if collection.identity_field and isinstance(item, dict):
            return str(item.get(collection.identity_field) or f"#{int(identity) + 1}")
        return f"#{int(identity) + 1}"

    def refresh_items(self) -> None:
        collection = self.current_collection()
        if collection is None:
            self.visible_items = []
            self.model.clear()
            return
        query = self.search_edit.text().strip()
        matcher = None
        if query and self.regex_checkbox.isChecked():
            try:
                matcher = re.compile(query, re.I)
            except re.error as exc:
                QMessageBox.warning(self, "正则无效", str(exc))
                return
        visible: list[tuple[object, object]] = []
        rows: list[tuple[object, ...]] = []
        for identity, item in self._collection_entries(collection):
            display_identity = self._display_identity(collection, identity, item)
            serialized = json.dumps(item, ensure_ascii=False)
            searchable = f"{display_identity}\n{serialized}"
            if query:
                matched = bool(matcher.search(searchable)) if matcher else query.casefold() in searchable.casefold()
                if not matched:
                    continue
            visible.append((identity, item))
            preview = serialized if len(serialized) <= 180 else f"{serialized[:177]}..."
            rows.append((display_identity, preview))
        self.visible_items = visible
        self.model.set_result(["标识", "预览"], rows)
        header = self.table_view.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        identity_width = max(100, min(self.table_view.sizeHintForColumn(0), 240))
        self.table_view.setColumnWidth(0, identity_width)

    def on_row_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if row < 0 or row >= len(self.visible_items):
            return
        identity, item = self.visible_items[row]
        self.current_identity = identity
        self.current_collection_index = self.collection_box.currentIndex()
        self.detail_view.blockSignals(True)
        self.detail_view.setPlainText(json.dumps(item, ensure_ascii=False, indent=2))
        self.detail_view.blockSignals(False)

    def on_detail_changed(self) -> None:
        if self.current_identity is not None:
            self.apply_button.setText("应用条目修改 *")

    def _parse_detail(self) -> object | None:
        try:
            return json.loads(self.detail_view.toPlainText())
        except json.JSONDecodeError as exc:
            QMessageBox.critical(self, "JSON 解析失败", str(exc))
            return None

    def apply_current_edit(self) -> bool:
        collection = self.current_collection()
        if collection is None or self.current_identity is None:
            QMessageBox.warning(self, "未选择条目", "请先选择或新增一个条目。")
            return False
        new_item = self._parse_detail()
        if new_item is None:
            return False
        if isinstance(collection.container, dict) and isinstance(new_item, dict) and self.loaded:
            expected_field = None
            expected_value = str(self.current_identity)
            if self.loaded.spec.kind == "info_missevan":
                expected_field = "dramaId"
            elif self.loaded.spec.kind == "trend_normal":
                expected_field = "id"
            elif self.loaded.spec.kind == "trend_peak":
                expected_field = "name"
            elif self.loaded.spec.kind == "trend_cv":
                expected_field = "cvName"
                expected_value = str(self.current_identity).partition(":")[2]
            elif self.loaded.spec.kind == "ranks_latest" and collection.name.endswith("/ 剧目"):
                expected_field = "dramaId" if "dramaId" in new_item else None
            if expected_field and str(new_item.get(expected_field) or "") != expected_value:
                QMessageBox.warning(
                    self,
                    "标识不可修改",
                    f"{expected_field} 必须与集合标识 {self.current_identity} 一致；"
                    "如需改名，请删除旧条目后新增。",
                )
                return False
        if collection.identity_field:
            if not isinstance(new_item, dict):
                QMessageBox.warning(self, "条目结构错误", "当前条目必须是 JSON 对象。")
                return False
            old_item = (
                collection.container[self.current_identity]
                if isinstance(collection.container, (dict, list))
                else None
            )
            old_identity = str((old_item or {}).get(collection.identity_field) or "")
            new_identity = str(new_item.get(collection.identity_field) or "")
            if old_identity and new_identity != old_identity:
                QMessageBox.warning(
                    self,
                    "标识不可修改",
                    f"{collection.identity_field} 不可直接修改；请删除旧条目后新增。",
                )
                return False
        collection.container[self.current_identity] = new_item
        self.dirty = True
        self.apply_button.setText("应用条目修改")
        self.refresh_items()
        return True

    def add_item(self) -> None:
        collection = self.current_collection()
        if collection is None:
            return
        label = collection.identity_field or "Hash/字典键"
        identity, accepted = QInputDialog.getText(self, "新增条目", f"输入唯一的 {label}：")
        identity = identity.strip()
        if not accepted or not identity:
            return
        if isinstance(collection.container, dict):
            if identity in collection.container:
                QMessageBox.warning(self, "条目已存在", f"集合中已存在 {identity}。")
                return
            if self.loaded and self.loaded.spec.kind == "info_missevan":
                if not identity.isascii() or not identity.isdigit():
                    QMessageBox.warning(self, "ID 无效", "dramaId 必须为 ASCII 数字。")
                    return
                new_item: object = {"dramaId": int(identity), "title": ""}
            elif self.loaded and self.loaded.spec.kind == "trend_normal":
                new_item = {"version": 2, "id": identity, "name": identity, "samples": {}}
            elif self.loaded and self.loaded.spec.kind == "trend_peak":
                new_item = {"version": 2, "name": identity, "samples": {}}
            elif self.loaded and self.loaded.spec.kind == "trend_cv":
                platform, separator, cv_name = identity.partition(":")
                if separator != ":" or platform not in ("missevan", "manbo") or not cv_name:
                    QMessageBox.warning(self, "标识无效", "CV trend 标识必须为 missevan:CV名 或 manbo:CV名。")
                    return
                new_item = {"version": 2, "cvName": cv_name, "samples": {}}
            else:
                new_item = {}
            collection.container[identity] = new_item
            self.current_identity = identity
        else:
            if collection.identity_field:
                if any(
                    str((item or {}).get(collection.identity_field) or "") == identity
                    for item in collection.container
                    if isinstance(item, dict)
                ):
                    QMessageBox.warning(self, "条目已存在", f"集合中已存在 {identity}。")
                    return
                value = (
                    int(identity)
                    if collection.identity_field == "dramaId" and identity.isdigit()
                    else identity
                )
                new_item = {collection.identity_field: value}
            else:
                new_item = {}
            collection.container.append(new_item)
            self.current_identity = len(collection.container) - 1
        self.current_collection_index = self.collection_box.currentIndex()
        self.dirty = True
        self.refresh_items()
        self.detail_view.blockSignals(True)
        self.detail_view.setPlainText(json.dumps(new_item, ensure_ascii=False, indent=2))
        self.detail_view.blockSignals(False)

    def delete_item(self) -> None:
        collection = self.current_collection()
        if collection is None or self.current_identity is None:
            QMessageBox.warning(self, "未选择条目", "请先选择要删除的条目。")
            return
        item = collection.container[self.current_identity]
        display = self._display_identity(collection, self.current_identity, item)
        summary = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if len(summary) > 500:
            summary = f"{summary[:497]}..."
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"标识：{display}\n所属集合：{collection.name}\n"
            f"差异摘要：删除 1 条，内容 {summary}\n\n"
            "此修改会在保存后写入 Upstash，确定继续吗？",
        )
        if answer != QMessageBox.Yes:
            return
        if isinstance(collection.container, dict):
            collection.container.pop(self.current_identity)
        else:
            collection.container.pop(int(self.current_identity))
        self.current_identity = None
        self.detail_view.blockSignals(True)
        self.detail_view.clear()
        self.detail_view.blockSignals(False)
        self.dirty = True
        self.refresh_items()

    def revert_item(self) -> None:
        if self.current_collection_index is None or self.current_identity is None:
            return
        original_collections = collection_refs(self.loaded.spec, self.original_payload) if self.loaded else []
        if not (0 <= self.current_collection_index < len(original_collections)):
            return
        original_collection = original_collections[self.current_collection_index]
        original = original_collection.container
        current = self.collections[self.current_collection_index].container
        try:
            if isinstance(original, list) and original_collection.identity_field:
                current_item = current[int(self.current_identity)]
                stable_identity = current_item[original_collection.identity_field]
                item = find_list_item_by_identity(
                    original,
                    original_collection.identity_field,
                    stable_identity,
                )
            else:
                item = original[self.current_identity]
        except (KeyError, IndexError, TypeError):
            QMessageBox.information(self, "无法还原", "该条目是本次新增的，可直接删除。")
            return
        current[self.current_identity] = copy.deepcopy(item)
        self.detail_view.blockSignals(True)
        self.detail_view.setPlainText(json.dumps(item, ensure_ascii=False, indent=2))
        self.detail_view.blockSignals(False)
        self.dirty = self.working_payload != self.original_payload
        self.refresh_items()

    def save_remote(self) -> None:
        if self.worker is not None or self.loaded is None:
            return
        if self.current_identity is not None and self.apply_button.text().endswith("*"):
            if not self.apply_current_edit():
                return
        if not self.dirty:
            QMessageBox.information(self, "没有修改", "当前资源没有需要保存的修改。")
            return
        before = json.dumps(self.original_payload, ensure_ascii=False, sort_keys=True)
        after = json.dumps(self.working_payload, ensure_ascii=False, sort_keys=True)
        answer = QMessageBox.question(
            self,
            "确认保存到 Upstash",
            f"资源：{self.loaded.spec.key}\n原始字符数：{len(before):,}\n修改后字符数：{len(after):,}\n"
            "保存将使用 CAS；若远端已变化会拒绝写入。确定继续吗？",
        )
        if answer != QMessageBox.Yes:
            return
        self.resource_status.setText(f"正在校验并保存 {self.loaded.spec.key} ...")
        self.set_busy(True)
        self.worker = UpstashResourceWorker(
            "save",
            loaded=self.loaded,
            payload=copy.deepcopy(self.working_payload),
        )
        self.worker.succeeded.connect(self.on_save_success)
        self.worker.failed.connect(self.on_worker_error)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def on_save_success(self, result: object) -> None:
        if not isinstance(result, SaveResult):
            self.on_worker_error("保存结果类型错误。")
            return
        self.dirty = False
        self.original_payload = copy.deepcopy(self.working_payload)
        local_state = f"本地镜像失败：{result.local_error}" if result.local_error else "本地镜像已同步"
        self.resource_status.setText(
            f"远端保存并回读验证成功 · {result.byte_count:,} bytes · SHA1 {result.content_sha1} · {local_state}"
        )
        if result.local_error:
            QMessageBox.warning(self, "远端成功，本地失败", result.local_error)
        else:
            QMessageBox.information(self, "保存成功", "Upstash 已原子保存并通过回读校验。")
        # The loaded CAS token is now stale; reload also creates the required next-session backup.
        self.loaded = None
        self.set_editor_enabled(False)


class OperationsPage(QWidget):
    run_command_requested = Signal(list, str)

    def __init__(self) -> None:
        super().__init__()
        self.command_buttons: list[QPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        top_split = QSplitter(Qt.Horizontal)
        root.addWidget(top_split, stretch=1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        ids_box = QGroupBox("源库更新")
        ids_layout = QGridLayout(ids_box)
        self.missevan_ids_edit = QLineEdit()
        self.manbo_ids_edit = QLineEdit()
        missevan_button = QPushButton("追加/刷新猫耳")
        manbo_button = QPushButton("追加/刷新漫播")
        sync_new_button = QPushButton("同步新剧")
        self.command_buttons.extend([missevan_button, manbo_button, sync_new_button])
        ids_layout.addWidget(QLabel("猫耳 IDs"), 0, 0)
        ids_layout.addWidget(self.missevan_ids_edit, 1, 0)
        ids_layout.addWidget(missevan_button, 1, 1)
        ids_layout.addWidget(QLabel("漫播 IDs"), 2, 0)
        ids_layout.addWidget(self.manbo_ids_edit, 3, 0)
        ids_layout.addWidget(manbo_button, 3, 1)
        ids_layout.addWidget(sync_new_button, 4, 0, 1, 2)
        ids_layout.setColumnStretch(0, 1)
        left_layout.addWidget(ids_box)

        batch_box = QGroupBox("批处理")
        batch_layout = QGridLayout(batch_box)
        refresh_button = QPushButton("刷新播放量")
        clean_button = QPushButton("清理漫播收费")
        rebuild_button = QPushButton("重建 SQLite")
        sync_remote_button = QPushButton("拉取远程数据")
        self.export_checkbox = QCheckBox("重建时导出 Excel")
        self.export_checkbox.setChecked(True)
        self.command_buttons.extend([refresh_button, clean_button, rebuild_button, sync_remote_button])
        batch_layout.addWidget(refresh_button, 0, 0)
        batch_layout.addWidget(clean_button, 0, 1)
        batch_layout.addWidget(self.export_checkbox, 1, 0)
        batch_layout.addWidget(rebuild_button, 1, 1)
        batch_layout.addWidget(sync_remote_button, 2, 0, 1, 2)
        left_layout.addWidget(batch_box)

        render_box = QGroupBox("出图")
        render_layout = QGridLayout(render_box)
        self.missevan_date_edit = QDateEdit(QDate.currentDate())
        self.manbo_date_edit = QDateEdit(QDate.currentDate())
        for widget in [self.missevan_date_edit, self.manbo_date_edit]:
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy/M/d")
        rank_button = QPushButton("生成榜单图")
        detail_button = QPushButton("生成明细图")
        self.command_buttons.extend([rank_button, detail_button])
        render_layout.addWidget(QLabel("猫耳日期"), 0, 0)
        render_layout.addWidget(self.missevan_date_edit, 0, 1)
        render_layout.addWidget(QLabel("漫播日期"), 1, 0)
        render_layout.addWidget(self.manbo_date_edit, 1, 1)
        render_layout.addWidget(rank_button, 2, 0)
        render_layout.addWidget(detail_button, 2, 1)
        left_layout.addWidget(render_box)

        rank_data_box = QGroupBox("榜单数据")
        rank_data_layout = QGridLayout(rank_data_box)
        self.rank_platform_box = QComboBox()
        self.rank_platform_box.addItems(["全部", "仅猫耳", "仅漫播"])
        self.rank_skip_danmaku = QCheckBox("跳过弹幕")
        self.rank_force = QCheckBox("强制刷新")
        fetch_rank_button = QPushButton("抓取榜单数据")
        only_danmaku_button = QPushButton("仅更新弹幕")
        self.command_buttons.extend([fetch_rank_button, only_danmaku_button])
        rank_data_layout.addWidget(QLabel("平台"), 0, 0)
        rank_data_layout.addWidget(self.rank_platform_box, 0, 1)
        rank_data_layout.addWidget(self.rank_skip_danmaku, 0, 2)
        rank_data_layout.addWidget(self.rank_force, 0, 3)
        rank_data_layout.addWidget(fetch_rank_button, 1, 0, 1, 2)
        rank_data_layout.addWidget(only_danmaku_button, 1, 2, 1, 2)
        rank_data_layout.setColumnStretch(1, 1)
        left_layout.addWidget(rank_data_box)
        left_layout.addStretch(1)

        log_box = QGroupBox("日志")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        clear_button = QPushButton("清空日志")
        self.command_buttons.append(clear_button)
        log_layout.addWidget(self.log_view, stretch=1)
        log_layout.addWidget(clear_button, alignment=Qt.AlignRight)

        top_split.addWidget(left_panel)
        top_split.addWidget(log_box)
        top_split.setStretchFactor(0, 0)
        top_split.setStretchFactor(1, 1)
        top_split.setSizes([420, 760])

        missevan_button.clicked.connect(self.run_append_missevan)
        manbo_button.clicked.connect(self.run_append_manbo)
        sync_new_button.clicked.connect(self.run_sync_new_drama)
        refresh_button.clicked.connect(self.run_refresh_watch_counts)
        clean_button.clicked.connect(self.run_clean_manbo)
        rebuild_button.clicked.connect(self.run_rebuild)
        sync_remote_button.clicked.connect(self.run_sync_remote_libraries)
        fetch_rank_button.clicked.connect(self.run_fetch_rank_data)
        only_danmaku_button.clicked.connect(self.run_only_danmaku)
        rank_button.clicked.connect(self.run_rank_images)
        detail_button.clicked.connect(self.run_rank_detail_images)
        clear_button.clicked.connect(self.log_view.clear)
        self.apply_density()

    def apply_density(self) -> None:
        self.log_view.setFont(make_mono_font())

    def append_log(self, text: str) -> None:
        if not text:
            return
        self.log_view.moveCursor(QTextCursor.End)
        self.log_view.insertPlainText(text)
        self.log_view.moveCursor(QTextCursor.End)

    def set_running(self, running: bool) -> None:
        for button in self.command_buttons:
            button.setEnabled(not running)

    def require_dates(self) -> tuple[str, str]:
        return (
            self.missevan_date_edit.date().toString("yyyy/M/d"),
            self.manbo_date_edit.date().toString("yyyy/M/d"),
        )

    def run_append_missevan(self) -> None:
        ids = split_ids(self.missevan_ids_edit.text())
        if not ids:
            QMessageBox.warning(self, "缺少 ID", "请输入至少一个猫耳 dramaId。")
            return
        self.run_command_requested.emit([PYTHON_EXE, "append_missevan_ids.py", *ids], "正在刷新猫耳源库")

    def run_append_manbo(self) -> None:
        ids = split_ids(self.manbo_ids_edit.text())
        if not ids:
            QMessageBox.warning(self, "缺少 ID", "请输入至少一个漫播 dramaId。")
            return
        self.run_command_requested.emit([PYTHON_EXE, "append_manbo_ids.py", *ids], "正在刷新漫播源库")

    def run_sync_new_drama(self) -> None:
        self.run_command_requested.emit([PYTHON_EXE, "sync_new_drama_ids.py"], "正在同步新剧")

    def run_refresh_watch_counts(self) -> None:
        self.run_command_requested.emit([PYTHON_EXE, "refresh_watch_counts.py"], "正在刷新播放量")

    def run_clean_manbo(self) -> None:
        if QMessageBox.question(self, "确认", "这会直接改写漫播源库和漫播播放量缓存，继续吗？") != QMessageBox.Yes:
            return
        self.run_command_requested.emit([PYTHON_EXE, "clean_manbo_pricing.py"], "正在清理漫播收费规则")

    def run_rebuild(self) -> None:
        command = [PYTHON_EXE, "rebuild_sqlite_from_libraries.py"]
        if self.export_checkbox.isChecked():
            command.append("--export-workbook")
        self.run_command_requested.emit(command, "正在重建 SQLite")

    def run_sync_remote_libraries(self) -> None:
        message = "这会从远程覆盖本地猫耳源库、漫播源库和 CV map，继续吗？"
        if QMessageBox.question(self, "确认拉取远程数据", message) != QMessageBox.Yes:
            return
        self.run_command_requested.emit(build_sync_remote_libraries_command(), "正在拉取远程数据")

    def _rank_platform_args(self) -> list[str]:
        index = self.rank_platform_box.currentIndex()
        if index == 1:
            return ["--missevan-only"]
        if index == 2:
            return ["--manbo-only"]
        return []

    def run_fetch_rank_data(self) -> None:
        command = [PYTHON_EXE, "fetch_rank_data.py"]
        command.extend(self._rank_platform_args())
        if self.rank_skip_danmaku.isChecked():
            command.append("--skip-danmaku")
        if self.rank_force.isChecked():
            command.append("--force")
        self.run_command_requested.emit(command, "正在抓取榜单数据")

    def run_only_danmaku(self) -> None:
        command = [PYTHON_EXE, "fetch_rank_data.py", "--only-danmaku"]
        command.extend(self._rank_platform_args())
        if self.rank_force.isChecked():
            command.append("--force")
        self.run_command_requested.emit(command, "正在更新弹幕数据")

    def run_rank_images(self) -> None:
        missevan_date, manbo_date = self.require_dates()
        self.run_command_requested.emit(
            [PYTHON_EXE, "render_rank_images.py", "--missevan-date", missevan_date, "--manbo-date", manbo_date],
            "正在生成榜单图",
        )

    def run_rank_detail_images(self) -> None:
        missevan_date, manbo_date = self.require_dates()
        self.run_command_requested.emit(
            [PYTHON_EXE, "render_rank_detail_images.py", "--missevan-date", missevan_date, "--manbo-date", manbo_date],
            "正在生成明细图",
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.process: QProcess | None = None
        self.log_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.local_fallback = codecs.getincrementaldecoder("mbcs")(errors="replace")

        self.setWindowTitle("CommenTasks GUI")

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        self.operations_page = OperationsPage()
        self.sqlite_page = SQLitePage()
        self.rank_page = RankPreviewPage()
        self.upstash_editor_page = UpstashEditorPage()
        tabs.addTab(self.operations_page, "操作")
        tabs.addTab(self.sqlite_page, "SQLite")
        tabs.addTab(self.rank_page, "榜单预览")
        tabs.addTab(self.upstash_editor_page, "Upstash 编辑")
        self.json_browser = JSONBrowserPage()
        tabs.addTab(self.json_browser, "JSON 浏览")

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

        self.operations_page.run_command_requested.connect(self.run_command)
        self.setMaximumWidth(1400)
        self.apply_density()
        self.resize_for_screen()

    def apply_density(self) -> None:
        app = QApplication.instance()
        app.setFont(make_ui_font(BASE_FONT_SIZE))
        app.setStyleSheet(build_stylesheet())
        self.operations_page.apply_density()
        self.sqlite_page.apply_density()
        self.rank_page.apply_density()
        self.upstash_editor_page.apply_density()
        self.json_browser.apply_density()

    def resize_for_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1280, 760)
            return
        available = screen.availableGeometry()
        width = min(1400, available.width(), max(1100, int(available.width() * 0.6)))
        height = max(720, int(available.height() * 0.6))
        left = available.x() + (available.width() - width) // 2
        top = available.y() + (available.height() - height) // 2
        self.setGeometry(left, top, width, height)

    def set_running(self, running: bool) -> None:
        self.operations_page.set_running(running)
        self.sqlite_page.setEnabled(not running)
        self.rank_page.setEnabled(not running)
        self.upstash_editor_page.setEnabled(not running)
        self.json_browser.setEnabled(not running)

    def append_log(self, text: str) -> None:
        self.operations_page.append_log(text)

    def run_command(self, command: list[str], status: str) -> None:
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "忙碌中", "当前已有任务在运行，请先等待它结束。")
            return

        self.log_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.local_fallback = codecs.getincrementaldecoder("mbcs")(errors="replace")
        self.set_running(True)
        real_command = python_command(command)
        self.status_bar.showMessage(status)
        self.append_log(f"$ {subprocess.list2cmdline(real_command)}\n")

        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.MergedChannels)
        env = process.processEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.on_process_output)
        process.finished.connect(self.on_process_finished)
        self.process = process
        process.start(real_command[0], real_command[1:])

        if not process.waitForStarted(5000):
            self.set_running(False)
            self.process = None
            QMessageBox.critical(self, "启动失败", "无法启动所选脚本。")
            self.status_bar.showMessage("失败：无法启动脚本")

    def decode_log_chunk(self, data: bytes, *, final: bool = False) -> str:
        if not data and not final:
            return ""
        try:
            return self.log_decoder.decode(data, final=final)
        except UnicodeDecodeError:
            return self.local_fallback.decode(data, final=final)

    def on_process_output(self) -> None:
        if self.process is None:
            return
        data = self.process.readAllStandardOutput().data()
        if data:
            self.append_log(self.decode_log_chunk(data))

    def on_process_finished(self, exit_code: int, exit_status) -> None:
        if self.process is not None:
            remaining = self.process.readAllStandardOutput().data()
            if remaining:
                self.append_log(self.decode_log_chunk(remaining))
            tail = self.decode_log_chunk(b"", final=True)
            if tail:
                self.append_log(tail)

        self.set_running(False)
        if exit_status == QProcess.NormalExit and exit_code == 0:
            self.status_bar.showMessage("完成")
        elif exit_status == QProcess.NormalExit:
            self.status_bar.showMessage(f"失败 / 退出码 {exit_code}")
        else:
            self.status_bar.showMessage("进程异常退出")

        self.sqlite_page.refresh_tables()
        self.process = None

    def closeEvent(self, event) -> None:
        if self.upstash_editor_page.worker is not None:
            QMessageBox.information(
                self,
                "Upstash 操作进行中",
                "请等待当前 Upstash 载入或保存操作完成后再关闭窗口。",
            )
            event.ignore()
            return
        if self.upstash_editor_page.dirty:
            answer = QMessageBox.question(
                self,
                "存在未保存修改",
                "Upstash 编辑页还有尚未保存到远端的修改，确定关闭吗？",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance()
    if app is not None:
        return app
    app = QApplication(argv or sys.argv)
    app.setApplicationName("CommenTasks GUI")
    app.setFont(make_ui_font(BASE_FONT_SIZE))
    return app


def main() -> None:
    app = create_application(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
