"""BigFish Council 桌面端（PySide6）.

五个界面：总览 / 任务列表 / 双模型议事厅 / 人类审批 / DSH 执行监控。
人类审批只在这里完成——本地 API 不暴露任何审批能力。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QPlainTextEdit, QProgressBar, QSplitter, QStackedWidget,
    QSystemTrayIcon, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import APP_NAME_CN, __version__, state as S
from .backup import export_bundle, inspect_bundle
from .config import DEFAULT_API_PORT, DEFAULT_PROXY_PORT, Settings
from .core import Council, CouncilError
from .runtime import open_council

DARK_QSS = """
QWidget { background: #16181d; color: #e6e8ee; font-family: "Microsoft YaHei UI","Segoe UI"; font-size: 13px; }
QMainWindow, QDialog { background: #12141a; }
QFrame#Card { background: #1c1f26; border: 1px solid #262a33; border-radius: 10px; }
QLabel#H1 { font-size: 20px; font-weight: 600; }
QLabel#H2 { font-size: 15px; font-weight: 600; color: #f2f4f8; }
QLabel#Dim { color: #98a0b0; }
QLabel#Pill { border-radius: 9px; padding: 2px 10px; background: #232833; color: #b9c2d4; }
QListWidget { background: #15181e; border: 1px solid #262a33; border-radius: 8px; outline: none; }
QListWidget::item { padding: 9px 10px; border-bottom: 1px solid #1f232b; }
QListWidget::item:selected { background: #26303f; }
QPlainTextEdit, QTextEdit, QLineEdit, QComboBox { background: #15181e; border: 1px solid #2a2f3a;
  border-radius: 8px; padding: 6px 8px; selection-background-color: #2f6bff; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus { border-color: #3f7bff; }
QPushButton { background: #232833; border: 1px solid #2e3441; border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: #2b3140; }
QPushButton:disabled { color: #5d6472; background: #1a1d24; }
QPushButton#Primary { background: #2f6bff; border-color: #2f6bff; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #3f7bff; }
QPushButton#Danger { background: #43212a; border-color: #5b2d38; color: #ff8a9b; }
QPushButton#Ghost { background: transparent; border: 1px solid #2e3441; }
QTabWidget::pane { border: 1px solid #262a33; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #191c22; padding: 7px 16px; border: 1px solid #262a33; border-bottom: none;
  border-top-left-radius: 8px; border-top-right-radius: 8px; color: #98a0b0; }
QTabBar::tab:selected { background: #1c1f26; color: #e6e8ee; }
QSplitter::handle { background: #1c1f26; }
QProgressBar { background: #1a1d24; border: 1px solid #262a33; border-radius: 6px; text-align: center; height: 8px; }
QProgressBar::chunk { background: #2f6bff; border-radius: 6px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #2e3441; border-radius: 5px; min-height: 24px; }
"""

MODEL_KEYS = ("gpt", "gemini")
MODEL_TITLES = {"gpt": "GPT", "gemini": "Gemini"}

STATUS_TONE = {
    S.DRAFT: "#8b93a3", S.IN_REVIEW: "#c9a227", S.REVIEW_PASSED: "#3f9a63",
    S.PENDING_APPROVAL: "#e0863a", S.APPROVED: "#3f7bff", S.DISPATCHED: "#4f9ad6",
    S.RUNNING: "#39a8d6", S.SUBMITTED: "#a875d6", S.DONE: "#3f9a63",
    S.BLOCKED: "#d64545", S.RETURNED: "#c9a227", S.REJECTED: "#8b93a3",
    S.CANCELLED: "#6b7280", S.CANCELLING: "#d08a3a",
}


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("Card")
    return f


def pill(text: str, tone: str = "#8b93a3") -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("Pill")
    lb.setStyleSheet(f"QLabel#Pill {{ background: rgba(255,255,255,0.06); color: {tone}; }}")
    return lb


class ApiServer(QObject):
    started = Signal(bool, str)

    def __init__(self, council: Council, host: str, port: int,
                 watchdog_interval: float | None = 10.0):
        super().__init__()
        self.council, self.host, self.port = council, host, port
        self.watchdog_interval = watchdog_interval
        self.thread: threading.Thread | None = None
        self._server = None

    def start(self):
        import uvicorn
        from .server import BindError, create_production_app
        # 纵深防御（SECURITY-REVIEW rc4 R10 / BFC-03）：直接构造 MainWindow 的脚本/
        # 测试绕过了 gui.run() 的闸门，所以这里再校验一次。闸门现在由
        # create_production_app() 统一执行，GUI 不再自己写一套判断。
        if self.thread is not None and self.thread.is_alive():
            return
        # lifespan 必须开启：看门狗挂在 FastAPI 生命周期上，不依赖 GUI 存活（P1-1）
        try:
            app, self.host = create_production_app(
                self.council, self.host, watchdog_interval=self.watchdog_interval)
        except BindError as exc:
            self.started.emit(False, f"拒绝启动：{exc}")
            return
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning",
                                access_log=False, lifespan="on")
        self._server = uvicorn.Server(config)
        self.running_host: str | None = None
        self.running_port: int | None = None
        self.started_ok = False

        def run():
            try:
                self._server.run()
            except BaseException as exc:      # 端口占用会抛 SystemExit，必须是 BaseException
                self.started.emit(False, f"启动失败：{type(exc).__name__}: {exc}")
                return
            self.started.emit(False, "服务已停止")

        def watch():
            # 不能在线程一启动就报「已监听」：uvicorn 绑定失败时连 started 都不会置位（A.2）
            deadline = time.time() + 20.0
            while time.time() < deadline:
                if getattr(self._server, "started", False):
                    self._resolve_endpoint()
                    self.started_ok = True
                    self.started.emit(True, f"http://{self.running_host}:{self.running_port}")
                    return
                if not self.thread.is_alive():
                    return                    # run() 已经 emit 过具体失败原因
                time.sleep(0.02)
            if not getattr(self._server, "started", False) and self.thread.is_alive():
                self.started.emit(False, "启动超时：20 秒内未就绪")

        self.thread = threading.Thread(target=run, daemon=True, name="bfc-api")
        self.thread.start()
        self._watcher = threading.Thread(target=watch, daemon=True, name="bfc-api-watch")
        self._watcher.start()

    def _resolve_endpoint(self):
        """读取真实监听端点：port=0 时设置里的 0 不是实际端口。"""
        host, port = self.host, self.port
        try:
            servers = getattr(self._server, "servers", None) or []
            socks = getattr(servers[0], "sockets", None) or []
            if socks:
                addr = socks[0].getsockname()
                host, port = addr[0], addr[1]
        except Exception:
            pass
        self.running_host, self.running_port = host, port

    def stop_watcher(self, timeout: float = 5.0):
        """等 watch 线程退出，避免它在界面关闭后再 emit。"""
        th = getattr(self, "_watcher", None)
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=timeout)

    def stop(self, timeout: float = 15.0) -> None:
        """停 API 线程。**超时未退必须报错**，不能安静地把「还在跑」当成已停。

        F-05：原来 join 完就返回，调用方（恢复备份）会以为服务已经停了，
        于是继续替换数据库文件——正是这条路径会让恢复和写请求撞在一起。
        """
        if self._server is not None:
            self._server.should_exit = True
        th = self.thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=timeout)
            if th.is_alive():
                raise TimeoutError(
                    f"API 线程 {timeout:.1f} 秒内没有退出，仍可能有请求在写库，"
                    "已中止本次操作")
        self.stop_watcher()

    def is_running(self) -> bool:
        th = self.thread
        return bool(th is not None and th.is_alive())


# --------------------------------------------------------------------- 总览
class OverviewPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(14)

        head = QHBoxLayout()
        t = QLabel("总览")
        t.setObjectName("H1")
        head.addWidget(t)
        head.addStretch(1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        lay.addLayout(head)

        self.status_card = card()
        sl = QVBoxLayout(self.status_card)
        sl.setContentsMargins(16, 14, 16, 14)
        self.conn_row = QHBoxLayout()
        self.conn_label = QLabel("DSH 连接：未启动")
        self.conn_label.setObjectName("H2")
        self.conn_row.addWidget(self.conn_label)
        self.conn_row.addStretch(1)
        sl.addLayout(self.conn_row)
        port = win.settings.api_port
        self.conn_hint = QLabel(
            f"本地 API 仅监听 {win.settings.api_host}:{port}；VPS 侧经 127.0.0.1:{DEFAULT_PROXY_PORT}"
            f" 跳板访问，需人工确认后才用 Tailscale Serve 暴露给 Tailnet。")
        self.conn_hint.setObjectName("Dim")
        self.conn_hint.setWordWrap(True)
        sl.addWidget(self.conn_hint)
        lay.addWidget(self.status_card)

        grid = QHBoxLayout()
        grid.setSpacing(12)
        self.counters: dict[str, QLabel] = {}
        for key, title in (("active", "执行中"), ("waiting", "等待审批"), ("blocked", "已阻断")):
            c = card()
            cl = QVBoxLayout(c)
            cl.setContentsMargins(16, 12, 16, 12)
            h = QLabel(title)
            h.setObjectName("Dim")
            v = QLabel("0")
            v.setObjectName("H1")
            cl.addWidget(h)
            cl.addWidget(v)
            self.counters[key] = v
            grid.addWidget(c)
        lay.addLayout(grid)

        lists = QHBoxLayout()
        lists.setSpacing(12)
        for key, title in (("active", "当前任务"), ("waiting", "等待审批"), ("blocked", "阻断任务")):
            c = card()
            cl = QVBoxLayout(c)
            cl.setContentsMargins(14, 12, 14, 12)
            h = QLabel(title)
            h.setObjectName("H2")
            cl.addWidget(h)
            lw = QListWidget()
            lw.setFrameShape(QFrame.NoFrame)
            lw.setMinimumHeight(140)
            self.counters[key + "_list"] = lw
            cl.addWidget(lw)
            lists.addWidget(c)
        lay.addLayout(lists, 1)

        self.review_state = QLabel("ChatGPT / Gemini：尚未导入审查")
        self.review_state.setObjectName("Dim")
        lay.addWidget(self.review_state)

    def refresh(self):
        ov = self.win.council.overview()
        self.counters["active"].setText(str(len(ov["active"])))
        self.counters["waiting"].setText(str(len(ov["waiting_approval"])))
        self.counters["blocked"].setText(str(len(ov["blocked"])))
        for key, items in (("active", ov["active"]), ("waiting", ov["waiting_approval"]),
                           ("blocked", ov["blocked"])):
            lw: QListWidget = self.counters[key + "_list"]
            lw.clear()
            if not items:
                it = QListWidgetItem("（暂无）")
                it.setForeground(QColor("#5d6472"))
                lw.addItem(it)
            for t in items:
                lw.addItem(QListWidgetItem(f"{t['title']}  ·  v{t['current_version']}"))
        on = self.win.server_running
        self.conn_label.setText(f"DSH 连接：{'本地 API 已监听' if on else '本地 API 未启动'}")
        self.conn_label.setStyleSheet(f"color: {'#3f9a63' if on else '#8b93a3'}")
        votes = 0
        for t in self.win.council.list_tasks():
            votes += len([r for r in self.win.council.reviews(t["id"], t["current_version"])
                          if r["kind"] == "vote"])
        self.review_state.setText(
            f"ChatGPT / Gemini：当前版本正式投票 {votes} 条（人工导入）")


# --------------------------------------------------------------------- 任务列表
class TaskPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)

        head = QHBoxLayout()
        h = QLabel("任务列表")
        h.setObjectName("H1")
        head.addWidget(h)
        head.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("检索标题或目标…")
        self.search.setFixedWidth(220)
        self.search.returnPressed.connect(self.refresh)
        head.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.addItem("全部状态", None)
        for s in S.ALL:
            self.filter.addItem(S.label(s), s)
        self.filter.currentIndexChanged.connect(self.refresh)
        head.addWidget(self.filter)
        b = QPushButton("新建任务")
        b.setObjectName("Primary")
        b.clicked.connect(self.new_task)
        head.addWidget(b)
        lay.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.on_pick)
        body.addWidget(self.list, 1)

        right = card()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 12, 14, 12)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        rl.addWidget(self.detail, 1)
        row = QHBoxLayout()
        self.btn_review = QPushButton("送审")
        self.btn_review.clicked.connect(lambda: self.act("review"))
        self.btn_prompt = QPushButton("改 Prompt（新版本）")
        self.btn_prompt.clicked.connect(lambda: self.act("edit"))
        self.btn_version = QPushButton("版本差异")
        self.btn_version.clicked.connect(lambda: self.act("diff"))
        for b in (self.btn_review, self.btn_prompt, self.btn_version):
            row.addWidget(b)
        rl.addLayout(row)
        body.addWidget(right, 1)
        lay.addLayout(body, 1)
        self.current: str | None = None

    def refresh(self):
        self.list.clear()
        for t in self.win.council.list_tasks(self.filter.currentData(), self.search.text().strip()):
            it = QListWidgetItem(f"{t['title']}\n{S.label(t['status'])} · v{t['current_version']} · "
                                 f"风险 {t['risk']}")
            it.setData(Qt.UserRole, t["id"])
            it.setForeground(QColor(STATUS_TONE.get(t["status"], "#e6e8ee")))
            self.list.addItem(it)

    def on_pick(self, cur: QListWidgetItem, _prev):
        if cur is None:
            return
        tid = cur.data(Qt.UserRole)
        self.current = tid
        try:
            t = self.win.council.get_task(tid)
        except CouncilError:
            return
        vs = self.win.council.votes_text(tid, t["current_version"])
        self.detail.setPlainText(
            f"{t['title']}\n"
            f"状态：{S.label(t['status'])}    版本：v{t['current_version']}    风险：{t['risk']}\n"
            f"目标：{t['goal'] or '—'}\n范围：{t['scope'] or '—'}\n"
            f"审查：{vs}\n"
            f"{'阻断原因：' + (t['blocked_reason'] or '') if t.get('blocked_reason') else ''}\n"
            f"——— 最终 Prompt ———\n{t['prompt'] or '（空）'}"
        )
        self.btn_review.setEnabled(t["status"] in (S.DRAFT, S.RETURNED, S.IN_REVIEW))
        self.btn_prompt.setEnabled(t["status"] not in S.TERMINAL)
        self.btn_version.setEnabled(t["current_version"] > 1)

    def new_task(self):
        dlg = TaskDialog(self.win)
        if dlg.exec() == QDialog.Accepted:
            self.refresh()
            self.win.pages["overview"].refresh()

    def act(self, kind: str):
        if not self.current:
            return
        try:
            if kind == "review":
                self.win.council.submit_for_review(self.current)
            elif kind == "edit":
                t = self.win.council.get_task(self.current)
                txt, ok = PromptDialog.get(self, "修改最终 Prompt", t["prompt"])
                if not ok:
                    return
                # 带当前版本做乐观并发控制：别人改过就拒绝，不静默覆盖（P1-2.4）
                self.win.council.update_prompt(
                    self.current, txt, "GUI 修改", expected_version=t["current_version"])
            elif kind == "diff":
                t = self.win.council.get_task(self.current)
                if t["current_version"] < 2:
                    return
                d = self.win.council.diff_versions(self.current, t["current_version"] - 1,
                                                   t["current_version"])
                PromptDialog.show_readonly(self, f"v{t['current_version']-1} → v{t['current_version']}", d)
        except CouncilError as e:
            QMessageBox.warning(self, "操作被拒绝", str(e))
        self.on_pick(self.list.currentItem(), None)
        self.win.pages["overview"].refresh()
        self.win.pages["approval"].refresh()
        self.win.pages["monitor"].refresh()


class TaskDialog(QDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("新建任务")
        self.resize(620, 520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit()
        self.goal = QLineEdit()
        self.scope = QLineEdit()
        self.risk = QComboBox()
        for r, l in (("low", "低"), ("medium", "中"), ("high", "高")):
            self.risk.addItem(l, r)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("经人类批准后原样下发的最终 Prompt")
        form.addRow("标题", self.title)
        form.addRow("目标", self.goal)
        form.addRow("修改范围", self.scope)
        form.addRow("风险等级", self.risk)
        lay.addLayout(form)
        lay.addWidget(QLabel("最终 Prompt"))
        lay.addWidget(self.prompt, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("创建")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def save(self):
        try:
            self.win.council.create_task(self.title.text(), self.goal.text(), self.scope.text(),
                                         self.risk.currentData(), self.prompt.toPlainText())
        except CouncilError as e:
            QMessageBox.warning(self, "创建失败", str(e))
            return
        self.accept()


class PromptDialog(QDialog):
    @staticmethod
    def show_readonly(parent, title, text):
        d = PromptDialog(parent, title, text, True)
        d.exec()

    @staticmethod
    def get(parent, title, text):
        d = PromptDialog(parent, title, text, False)
        ok = d.exec() == QDialog.Accepted
        return d.editor.toPlainText(), ok

    def __init__(self, parent, title, text, readonly):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        self.editor = QPlainTextEdit(text)
        self.editor.setReadOnly(readonly)
        lay.addWidget(self.editor, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(QPushButton("关闭", clicked=self.reject))
        if not readonly:
            b = QPushButton("保存")
            b.setObjectName("Primary")
            b.clicked.connect(self.accept)
            row.addWidget(b)
        lay.addLayout(row)


# --------------------------------------------------------------------- 议事厅
class CouncilPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        # 草稿按 (任务, 版本, 模型) 存，切换任务/版本不互相污染；_loaded 记录当前槽位
        self._drafts: dict = {}
        self._loaded = None
        self._vote_labels: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)

        head = QHBoxLayout()
        h = QLabel("双模型议事厅")
        h.setObjectName("H1")
        head.addWidget(h)
        head.addStretch(1)
        self.task_box = QComboBox()
        self.task_box.setFixedWidth(280)
        self.task_box.currentIndexChanged.connect(self.load)
        head.addWidget(QLabel("任务"))
        head.addWidget(self.task_box)
        lay.addLayout(head)

        note = QLabel("审查评论与正式投票分离：评论只留痕，投票才计数；GPT 与 Gemini 必须对同一版本都投 approve。")
        note.setObjectName("Dim")
        note.setWordWrap(True)
        lay.addWidget(note)

        split = QSplitter(Qt.Horizontal)
        self.gpt_view = self._pane(split, "GPT 方案", "gpt")
        self.gem_view = self._pane(split, "Gemini 审查", "gemini")
        split.setSizes([500, 500])
        lay.addWidget(split, 2)

        diff_card = card()
        dl = QVBoxLayout(diff_card)
        dl.setContentsMargins(14, 10, 14, 10)
        row = QHBoxLayout()
        lbl = QLabel("方案版本差异")
        lbl.setObjectName("H2")
        row.addWidget(lbl)
        row.addStretch(1)
        self.v1 = QComboBox()
        self.v2 = QComboBox()
        self.btn_diff = QPushButton("比较")
        self.btn_diff.clicked.connect(self.show_diff)
        row.addWidget(QLabel("从"))
        row.addWidget(self.v1)
        row.addWidget(QLabel("到"))
        row.addWidget(self.v2)
        row.addWidget(self.btn_diff)
        dl.addLayout(row)
        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setFixedHeight(150)
        dl.addWidget(self.diff_view)
        lay.addWidget(diff_card, 1)

        concl = card()
        cl = QHBoxLayout(concl)
        cl.setContentsMargins(14, 10, 14, 10)
        self.conclusion = QLabel("当前审查结论：—")
        cl.addWidget(self.conclusion)
        cl.addStretch(1)
        self.btn_import = QPushButton("导入审查文本…")
        self.btn_import.clicked.connect(self.import_text)
        cl.addWidget(self.btn_import)
        lay.addWidget(concl)


    def _pane(self, split, title, model):
        c = card()
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 12)
        top = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("H2")
        top.addWidget(t)
        top.addStretch(1)
        self._vote_labels = getattr(self, "_vote_labels", {})
        vote = QLabel("未投票")
        vote.setObjectName("Pill")
        self._vote_labels[model] = vote
        top.addWidget(vote)
        v.addLayout(top)

        # 上半：已落库的历史（只读）。它跟着任务版本走，不会被草稿覆盖。
        hist_lbl = QLabel("已记录（只读）")
        hist_lbl.setObjectName("Dim")
        v.addWidget(hist_lbl)
        hist = QPlainTextEdit()
        hist.setReadOnly(True)
        hist.setPlaceholderText("该模型在此版本下还没有任何记录")
        v.addWidget(hist, 1)

        # 下半：本轮草稿。按 (任务, 版本, 模型) 存档，切任务/切版本都不串味。
        draft_lbl = QLabel("本轮输入（草稿，未提交）")
        draft_lbl.setObjectName("Dim")
        v.addWidget(draft_lbl)
        ed = QPlainTextEdit()
        ed.setPlaceholderText("粘贴模型输出，或点击下方按钮保存为评论/投票")
        v.addWidget(ed, 1)

        row = QHBoxLayout()
        b1 = QPushButton("保存为评论")
        b1.clicked.connect(lambda: self.save(model, "comment"))
        b2 = QPushButton("投 approve")
        b2.setObjectName("Primary")
        b2.clicked.connect(lambda: self.save(model, "approve"))
        b3 = QPushButton("投 reject")
        b3.setObjectName("Danger")
        b3.clicked.connect(lambda: self.save(model, "reject"))
        for b in (b1, b2, b3):
            row.addWidget(b)
        v.addLayout(row)
        setattr(self, f"editor_{model}", ed)
        setattr(self, f"history_{model}", hist)
        split.addWidget(c)
        return c



    # ---------------------------------------------------------------- 草稿存档
    def _draft_key(self, tid: str, version: int, model: str) -> tuple:
        """草稿的归属键：任务 + 版本 + 模型。三者任一变化都不共用草稿。"""
        return (tid, version, model)

    def _stash_draft(self) -> None:
        """把当前编辑器里的文字存回它所属的 (任务,版本,模型) 槽位。"""
        if not hasattr(self, "_drafts"):
            return
        cur = getattr(self, "_loaded", None)
        if not cur:
            return
        tid, version, model = cur
        if model not in MODEL_KEYS:
            return
        ed = getattr(self, f"editor_{model}", None)
        if ed is not None:
            self._drafts[self._draft_key(tid, version, model)] = ed.toPlainText()


    def refresh_tasks(self):
        cur = self.task_box.currentData()
        self.task_box.blockSignals(True)
        self.task_box.clear()
        for t in self.win.council.list_tasks():
            self.task_box.addItem(f"{t['title']} · {S.label(t['status'])}", t["id"])
        idx = self.task_box.findData(cur) if cur else 0
        self.task_box.setCurrentIndex(max(idx, 0))
        self.task_box.blockSignals(False)
        self.load()

    def load(self):
        # C：切任务/切版本前先把当前草稿存回它所属的槽位，不再「切一下就冲掉」
        if not hasattr(self, "_drafts"):
            self._drafts = {}
        self._stash_draft()
        tid = self.task_box.currentData()
        if not tid:
            return
        try:
            t = self.win.council.get_task(tid)
        except CouncilError:
            return
        version = t["current_version"]
        self._loaded = (tid, version)

        self.v1.clear()
        self.v2.clear()
        for v in sorted({x["version"] for x in self.win.council.versions(tid)}):
            self.v1.addItem(f"v{v}", v)
            self.v2.addItem(f"v{v}", v)
        self.v2.setCurrentIndex(self.v2.count() - 1)
        self.v1.setCurrentIndex(max(0, self.v2.count() - 2))

        rows_all = self.win.council.reviews(tid, version)
        votes = t["votes"]          # 后端按「每个模型最新一票（含撤销）」算，前端不再自己判断
        for model in MODEL_KEYS:
            rows = [r for r in rows_all if r["model"] == model]
            hist: QPlainTextEdit = getattr(self, f"history_{model}")
            hist.setPlainText("\n\n".join(
                f"[{r['kind']} {r['verdict'] or ''}] {r['content']}"
                for r in reversed(rows)) or "")

            v = votes.get(model) or {}
            if v.get("revoked"):
                tag, tone = "已撤票", "#d64545"
            elif v.get("verdict") == "approve":
                tag, tone = "approve", "#3f9a63"
            elif v.get("verdict") == "reject":
                tag, tone = "reject", "#d64545"
            elif rows:
                tag, tone = "仅评论", "#c9a227"
            else:
                tag, tone = "未投票", "#8b93a3"
            lb = self._vote_labels[model]
            lb.setText(tag)
            lb.setStyleSheet(
                f"QLabel#Pill {{ background: rgba(255,255,255,0.06); color: {tone}; }}")

            ed: QPlainTextEdit = getattr(self, f"editor_{model}")
            ed.setPlainText(self._drafts.get((tid, version, model), ""))

        concl = []
        for model in MODEL_KEYS:
            v = votes.get(model) or {}
            tag = "已撤票" if v.get("revoked") else (v.get("verdict") or "—")
            concl.append(f"{MODEL_TITLES[model]} {tag}")
        self.conclusion.setText(
            "当前审查结论：" + " / ".join(concl) + f"　→　{S.label(t['status'])}")

    def save(self, model: str, verdict: str):
        tid = self.task_box.currentData()
        if not tid:
            return
        if not hasattr(self, "_drafts"):
            self._drafts = {}
        ed: QPlainTextEdit = getattr(self, f"editor_{model}")
        content = ed.toPlainText().strip()
        if not content:
            QMessageBox.information(self, "内容为空", "请先输入要登记的审查内容")
            return
        try:
            if verdict == "comment":
                self.win.council.add_review(tid, model, content, None, kind="comment")
            else:
                self.win.council.add_review(tid, model, content, verdict, kind="vote")
        except CouncilError as e:
            QMessageBox.warning(self, "写入被拒绝", str(e))
            return
        # 提交成功：草稿槽位作废、编辑器清空，避免下次切回来看到已落库的旧文字
        cur = getattr(self, "_loaded", None)
        if cur:
            self._drafts.pop((cur[0], cur[1], model), None)
        ed.clear()
        self.load()
        self.win.refresh_all()


    def import_text(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入审查文本", "", "文本 (*.txt *.md);;所有文件 (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            QMessageBox.warning(self, "读取失败", str(e))
            return
        ed: QPlainTextEdit = getattr(self, f"editor_{'gpt'}")
        ed.setPlainText(text)

    def show_diff(self):
        tid = self.task_box.currentData()
        if not tid or self.v1.currentData() is None:
            return
        try:
            d = self.win.council.diff_versions(tid, self.v1.currentData(), self.v2.currentData())
        except CouncilError as e:
            QMessageBox.warning(self, "无法比较", str(e))
            return
        self.diff_view.setPlainText(d or "（两个版本内容一致）")


# --------------------------------------------------------------------- 人类审批
class ApprovalPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        h = QLabel("人类审批")
        h.setObjectName("H1")
        lay.addWidget(h)
        warn = QLabel("审批只能在这个本地窗口完成。远程接口未注册任何审批工具，模型无法代替人类批准。")
        warn.setObjectName("Dim")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = card()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(14, 12, 14, 12)
        ll.addWidget(QLabel("待审批 / 已阻断任务"))
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.on_pick)
        ll.addWidget(self.list, 1)
        body.addWidget(left, 1)

        right = card()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 12, 14, 12)
        self.head = QLabel("选择左侧任务")
        self.head.setObjectName("H2")
        rl.addWidget(self.head)
        form = QFormLayout()
        self.f_goal = QLabel("—"); self.f_scope = QLabel("—"); self.f_risk = QLabel("—")
        self.f_review = QLabel("—")
        for w in (self.f_goal, self.f_scope, self.f_risk, self.f_review):
            w.setWordWrap(True)
        form.addRow("任务目标", self.f_goal)
        form.addRow("修改范围", self.f_scope)
        form.addRow("风险等级", self.f_risk)
        form.addRow("双模型审查", self.f_review)
        rl.addLayout(form)
        rl.addWidget(QLabel("最终 Prompt（原样下发）"))
        self.prompt = QPlainTextEdit()
        self.prompt.setReadOnly(True)
        rl.addWidget(self.prompt, 1)
        self.note = QLineEdit()
        self.note.setPlaceholderText("审批备注（可选）")
        rl.addWidget(self.note)
        row = QHBoxLayout()
        self.b_ok = QPushButton("批准并派工")
        self.b_ok.setObjectName("Primary")
        self.b_ok.clicked.connect(lambda: self.decide("approve"))
        self.b_back = QPushButton("退回修改")
        self.b_back.clicked.connect(lambda: self.decide("return"))
        self.b_no = QPushButton("拒绝")
        self.b_no.setObjectName("Danger")
        self.b_no.clicked.connect(lambda: self.decide("reject"))
        self.b_done = QPushButton("验收通过")
        self.b_done.clicked.connect(lambda: self.verify(True))
        self.b_fail = QPushButton("验收不通过")
        self.b_fail.clicked.connect(lambda: self.verify(False))
        for b in (self.b_ok, self.b_back, self.b_no, self.b_done, self.b_fail):
            row.addWidget(b)
        rl.addLayout(row)
        body.addWidget(right, 2)
        lay.addLayout(body, 1)
        self.current = None

    def refresh(self):
        self.list.clear()
        rows = [t for t in self.win.council.list_tasks()
                if t["status"] in (S.PENDING_APPROVAL, S.REVIEW_PASSED, S.BLOCKED, S.SUBMITTED)]
        for t in rows:
            it = QListWidgetItem(f"{t['title']}\n{S.label(t['status'])} · v{t['current_version']}")
            it.setData(Qt.UserRole, t["id"])
            it.setForeground(QColor(STATUS_TONE.get(t["status"], "#e6e8ee")))
            self.list.addItem(it)
        if not rows:
            it = QListWidgetItem("（暂无待办）")
            it.setForeground(QColor("#5d6472"))
            self.list.addItem(it)

    def on_pick(self, cur, _p):
        if cur is None:
            return
        tid = cur.data(Qt.UserRole)
        if not tid:
            return
        self.current = tid
        t = self.win.council.get_task(tid)
        self.loaded_version = t["current_version"]
        self.head.setText(f"{t['title']}　·　{S.label(t['status'])}　·　v{t['current_version']}")
        self.f_goal.setText(t["goal"] or "—")
        self.f_scope.setText(t["scope"] or "—")
        self.f_risk.setText({"low": "低", "medium": "中", "high": "高"}.get(t["risk"], t["risk"]))
        self.f_review.setText(self.win.council.votes_text(tid, t["current_version"]))
        self.prompt.setPlainText(t["prompt"] or "（空）")
        can_approve = t["status"] in (S.PENDING_APPROVAL, S.REVIEW_PASSED)
        self.b_ok.setEnabled(can_approve)
        self.b_back.setEnabled(can_approve or t["status"] == S.IN_REVIEW)
        self.b_no.setEnabled(can_approve)
        self.b_done.setEnabled(t["status"] == S.SUBMITTED)
        self.b_fail.setEnabled(t["status"] in (S.SUBMITTED, S.BLOCKED))

    def decide(self, decision: str):
        if not self.current:
            return
        if decision == "approve":
            ok = QMessageBox.question(self, "确认批准",
                                      "批准后该任务将可被 DSH 执行器领取并执行。确认？")
            if ok != QMessageBox.Yes:
                return
        try:
            # v0.1.1：带上打开页面时看到的版本号，中途被改就拒绝（P0-2）
            self.win.council.approve(self.current, decision, self.note.text(),
                                     expected_version=self.loaded_version)
        except CouncilError as e:
            QMessageBox.warning(self, "审批被拒绝", str(e))
            return
        self.win.refresh_all()

    def verify(self, accept: bool):
        if not self.current:
            return
        try:
            self.win.council.verify(self.current, accept, self.note.text())
        except CouncilError as e:
            QMessageBox.warning(self, "验收失败", str(e))
            return
        self.win.refresh_all()


# --------------------------------------------------------------------- 执行监控
class MonitorPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        head = QHBoxLayout()
        h = QLabel("DSH 执行监控")
        h.setObjectName("H1")
        head.addWidget(h)
        head.addStretch(1)
        self.btn_pause = QPushButton("请求暂停")
        self.btn_pause.clicked.connect(lambda: self.control("pause"))
        self.btn_cancel = QPushButton("取消任务")
        self.btn_cancel.setObjectName("Danger")
        self.btn_cancel.clicked.connect(lambda: self.control("cancel"))
        self.btn_escalate = QPushButton("申请 Codex/Astra 升级")
        self.btn_escalate.clicked.connect(self.escalate)
        for b in (self.btn_pause, self.btn_cancel, self.btn_escalate):
            head.addWidget(b)
        lay.addLayout(head)

        self.tasks = QComboBox()
        self.tasks.currentIndexChanged.connect(self.load)
        lay.addWidget(self.tasks)

        info = card()
        il = QVBoxLayout(info)
        il.setContentsMargins(14, 10, 14, 10)
        self.status_line = QLabel("—")
        self.status_line.setObjectName("H2")
        il.addWidget(self.status_line)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        il.addWidget(self.progress)
        self.hb = QLabel("心跳：—")
        self.hb.setObjectName("Dim")
        il.addWidget(self.hb)
        lay.addWidget(info)

        tabs = QTabWidget()
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True)
        self.diff_view = QPlainTextEdit(); self.diff_view.setReadOnly(True)
        self.test_view = QPlainTextEdit(); self.test_view.setReadOnly(True)
        self.event_view = QPlainTextEdit(); self.event_view.setReadOnly(True)
        tabs.addTab(self.log_view, "执行日志")
        tabs.addTab(self.diff_view, "Diff")
        tabs.addTab(self.test_view, "测试结果")
        tabs.addTab(self.event_view, "事件")
        lay.addWidget(tabs, 1)
        self.current = None

    def refresh(self):
        cur = self.tasks.currentData()
        self.tasks.blockSignals(True)
        self.tasks.clear()
        rows = self.win.council.list_tasks()
        for t in rows:
            self.tasks.addItem(f"{t['title']} · {S.label(t['status'])}", t["id"])
        if cur:
            i = self.tasks.findData(cur)
            if i >= 0:
                self.tasks.setCurrentIndex(i)
        self.tasks.blockSignals(False)
        self.load()

    def load(self):
        tid = self.tasks.currentData()
        if not tid:
            return
        self.current = tid
        t = self.win.council.get_task(tid)
        runs = t["runs"]
        last = runs[0] if runs else None
        self.status_line.setText(f"{t['title']}　·　{S.label(t['status'])}　·　v{t['current_version']}")
        self.progress.setValue({S.DONE: 100, S.SUBMITTED: 85, S.RUNNING: 55,
                                S.DISPATCHED: 25, S.CANCELLING: 60,
                                S.BLOCKED: 100}.get(t["status"], 5))
        if last:
            age = time.time() - (last["last_beat_at"] or last["started_at"])
            self.hb.setText(f"心跳：{age:.0f} 秒前　·　执行器 {last['executor_id']}　·　"
                            f"运行状态 {last['status']}"
                            + (f"　·　失败原因 {last['exit_note']}" if last.get("exit_note") else ""))
            arts = last.get("artifacts", {})
            self.log_view.setPlainText(arts.get("log", "（无日志）"))
            self.diff_view.setPlainText(arts.get("diff", "（无 Diff）"))
            self.test_view.setPlainText(arts.get("tests", "（无测试结果）"))
        else:
            self.hb.setText("心跳：尚未派工")
            self.log_view.setPlainText("（无日志）")
            self.diff_view.setPlainText("（无 Diff）")
            self.test_view.setPlainText("（无测试结果）")
        if t.get("blocked_reason"):
            self.hb.setText(self.hb.text() + f"\n阻断原因：{t['blocked_reason']}")
        evs = self.win.council.events(tid, 100)
        self.event_view.setPlainText("\n".join(
            f"{time.strftime('%m-%d %H:%M:%S', time.localtime(e['created_at']))}  "
            f"{e['kind']}  {e['detail']}" for e in reversed(evs)))

    def control(self, kind: str):
        if not self.current:
            return
        try:
            if kind == "pause":
                self.win.council.request_pause(self.current, True)
            else:
                if QMessageBox.question(self, "确认取消", "取消会吊销执行票，确认？") != QMessageBox.Yes:
                    return
                self.win.council.request_cancel(self.current)
        except CouncilError as e:
            QMessageBox.warning(self, "操作失败", str(e))
            return
        self.load()

    def escalate(self):
        if not self.current:
            return
        box = QMessageBox(self)
        box.setWindowTitle("升级申请")
        box.setText("第一版只创建升级申请，不会调用 Codex/Astra 或消耗订阅额度。")
        box.setInformativeText("申请将写入任务事件，等待你单独授权。")
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        if box.exec() != QMessageBox.Ok:
            return
        self.win.council.log_event(self.current, "escalation.requested",
                                   "GUI 创建升级申请（未调用任何模型）")
        self.load()


# --------------------------------------------------------------------- 主窗口
class SettingsDialog(QDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("设置")
        self.resize(560, 240)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.dir = QLineEdit(str(win.settings.data_dir))
        browse = QPushButton("选择…")
        browse.clicked.connect(self.pick)
        row = QHBoxLayout(); row.addWidget(self.dir); row.addWidget(browse)
        form.addRow("数据目录", row)
        self.port = QLineEdit(str(win.settings.api_port))
        form.addRow("本地 API 端口", self.port)
        lay.addLayout(form)
        tip = QLabel("修改数据目录后需要重启软件；旧数据库不会自动迁移。")
        tip.setObjectName("Dim")
        lay.addWidget(tip)
        btns = QHBoxLayout()
        btns.addStretch(1)
        b1 = QPushButton("备份数据…")
        b1.clicked.connect(self.backup)
        b2 = QPushButton("恢复数据…")
        b2.clicked.connect(self.restore)
        b3 = QPushButton("保存")
        b3.setObjectName("Primary")
        b3.clicked.connect(self.save)
        for b in (b1, b2, b3):
            btns.addWidget(b)
        lay.addLayout(btns)

    def pick(self):
        d = QFileDialog.getExistingDirectory(self, "选择数据目录", self.dir.text())
        if d:
            self.dir.setText(d)

    def save(self):
        s = self.win.settings
        raw = (self.port.text() or "").strip()
        try:
            port = int(raw) if raw else DEFAULT_API_PORT
        except ValueError:
            QMessageBox.warning(self, "端口无效", "端口必须是数字。")
            return
        if not (1024 <= port <= 65535):
            QMessageBox.warning(self, "端口无效", "端口需在 1024-65535 之间。")
            return
        if port == DEFAULT_PROXY_PORT:
            QMessageBox.warning(self, "端口冲突",
                                f"{DEFAULT_PROXY_PORT} 是 VPS 跳板专用端口，请换一个。")
            return
        s.data_dir = Path(self.dir.text()).expanduser()
        s.api_port = port
        s.ensure_dirs()
        s.save()
        QMessageBox.information(self, "已保存", "配置已写入；重启软件后生效。")
        self.accept()

    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, "备份", f"bigfish-backup-{int(time.time())}.zip",
                                              "ZIP (*.zip)")
        if not path:
            return
        try:
            # v0.1.1：走 sqlite3 backup API 的一致快照，WAL 里的数据不会丢（P0-1）
            export_bundle(self.win.council.db, path)
            QMessageBox.information(self, "备份完成", path)
        except Exception as e:
            QMessageBox.warning(self, "备份失败", str(e))

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择备份", "", "ZIP (*.zip)")
        if not path:
            return
        info = inspect_bundle(path)
        if info.get("error"):
            QMessageBox.warning(self, "备份不可用", str(info["error"]))
            return
        ok = QMessageBox.question(
            self, "确认恢复",
            f"备份时间：{info.get('created_at_iso', '未知')}\n"
            f"任务数：{info.get('table_counts', {}).get('tasks', 0)}\n\n"
            "恢复前会自动把当前数据库备份为 council.db.prerestore。继续吗？")
        if ok != QMessageBox.Yes:
            return
        try:
            # 走 council 层：内部会轮换执行票密钥、阻断在途任务、禁用全部远端身份、
            # 递增恢复世代（P1-4.2 + R1）。R8：整个替换期间停服并进入维护模式，
            # 期间任何写请求都拿到 503，不会出现「恢复失败但执行器以为成功」。
            self.win.pause_services()
            try:
                out = self.win.council.restore_from_bundle(
                    path, before_replace=self.win.pause_services)
            finally:
                self.win.resume_services()
            blocked = out.get("blocked_inflight") or 0
            frozen = out.get("frozen_approved") or 0
            disabled = out.get("executors_disabled") or 0
            QMessageBox.information(
                self, "恢复完成",
                f"已恢复（恢复世代 {out.get('restore_epoch')}）\n"
                f"原库备份在：\n{out.get('prerestore_path') or '(无)'}\n"
                f"执行票密钥已轮换：{'是' if out.get('key_rotated') else '否'}\n"
                f"在途任务已阻断：{blocked} 个（需人工确认副作用后重新审批）\n"
                f"待派工任务已冻结：{frozen} 个（需重新提交审批）\n"
                f"远端身份已禁用：{disabled} 个（需人工重新授权并更换凭据）")
        except Exception as e:
            QMessageBox.warning(self, "恢复失败", str(e))


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, council: Council, db=None):
        super().__init__()
        self.settings = settings
        self.council = council
        # 数据库由入口创建并只创建一次（A.1）；窗口负责在退出时关掉它（A.3）
        self.db = db if db is not None else getattr(council, "db", None)
        self.server_running = False
        self._closed = False
        self.setWindowTitle(f"{APP_NAME_CN} v{__version__}")
        self.resize(1240, 820)
        self.setStyleSheet(DARK_QSS)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        nav = QFrame()
        nav.setFixedWidth(190)
        nav.setStyleSheet("QFrame { background: #101319; border-right: 1px solid #1f232b; }")
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(12, 16, 12, 16)
        logo = QLabel("大肥鱼议事厅")
        logo.setObjectName("H2")
        nl.addWidget(logo)
        sub = QLabel("BigFish Council")
        sub.setObjectName("Dim")
        nl.addWidget(sub)
        nl.addSpacing(16)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "overview": OverviewPage(self),
            "tasks": TaskPage(self),
            "council": CouncilPage(self),
            "approval": ApprovalPage(self),
            "monitor": MonitorPage(self),
        }
        order = [("overview", "总览"), ("tasks", "任务列表"), ("council", "双模型议事厅"),
                 ("approval", "人类审批"), ("monitor", "执行监控")]
        self.nav_buttons = {}
        for key, label in order:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet("QPushButton { text-align: left; padding: 9px 12px; border: none;"
                            " border-radius: 8px; background: transparent; }"
                            "QPushButton:checked { background: #232833; color: #ffffff; }"
                            "QPushButton:hover { background: #1a1e26; }")
            b.clicked.connect(lambda _=False, k=key: self.goto(k))
            nl.addWidget(b)
            self.nav_buttons[key] = b
            self.stack.addWidget(self.pages[key])
        nl.addStretch(1)

        self.api_label = QLabel("API：未启动")
        self.api_label.setObjectName("Dim")
        self.api_label.setWordWrap(True)
        nl.addWidget(self.api_label)
        sb = QPushButton("设置")
        sb.setObjectName("Ghost")
        sb.clicked.connect(lambda: SettingsDialog(self).exec())
        nl.addWidget(sb)

        root.addWidget(nav)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._services_paused = False
        self.server = ApiServer(council, settings.api_host, settings.api_port,
                                watchdog_interval=settings.watchdog_interval_sec)
        self.server.started.connect(self.on_server)
        self.server.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(3000)

        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip(APP_NAME_CN)
        menu = QMenu()
        act_show = QAction("显示窗口", self)
        act_show.triggered.connect(self.showNormal)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.really_quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.showNormal() if r == QSystemTrayIcon.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self._force_quit = False
        self.goto("overview")

    def goto(self, key: str):
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        page = self.pages[key]
        if hasattr(page, "refresh"):
            page.refresh()
        if key == "council":
            page.refresh_tasks()
        if key == "monitor":
            page.refresh()

    def pause_services(self):
        """R8：恢复备份前先排空——停 API 线程与看门狗，别再放新请求进来。

        幂等：restore_bundle 内部也会把它当 before_replace 调一次。

        F-05：两处语义都要收紧——
          1) 只有 API 与看门狗**都确认停了**才置 _services_paused；
          2) 任何一步失败都向上抛，让恢复**中止**，不能带着还在跑的线程换库。
             已经停掉的那半套服务在抛错前会尝试接回来（尽力而为，失败也抛）。
        """
        if self._services_paused:
            return
        try:
            if self.server.receivers(self.server.started):
                self.server.started.disconnect(self.on_server)
        except Exception:
            pass
        try:
            self.server.stop()
            self.council.stop_watchdog()
        except Exception as exc:
            # 半停状态：把已经停掉的 API 接回来，然后如实报错中止恢复。
            try:
                self.server.start()
                self.council.start_watchdog(self.settings.watchdog_interval_sec)
            except Exception:                # pragma: no cover
                pass
            raise RuntimeError(
                f"停服失败，已中止本次操作（服务可能仍在运行）：{exc}") from exc
        self._services_paused = True

    def resume_services(self):
        """恢复结束后把服务接回来（失败也接，否则用户界面永远连不上执行器）。"""
        if not self._services_paused or self._closed:
            return
        self._services_paused = False
        try:
            self.server.started.connect(self.on_server)
        except Exception:
            pass
        self.server.start()
        try:
            self.council.start_watchdog(self.settings.watchdog_interval_sec)
        except Exception:
            pass

    def on_server(self, ok: bool, msg: str):
        if self._closed:
            return                      # 关闭过程中 watch 线程仍可能 emit，此时库已关
        self.server_running = ok
        if ok:
            host = getattr(self.server, "running_host", None) or self.settings.api_host
            port = getattr(self.server, "running_port", None) or self.settings.api_port
            self.api_label.setText(f"API：已监听 {host}:{port}")
        else:
            # 绑定失败必须显示真实原因，不能只写「未启动」（A.2）
            self.api_label.setText(f"API：未启动（{msg}）" if msg else "API：未启动")
        self.pages["overview"].refresh()

    def tick(self):
        if self._closed or not self.isVisible() or self.isMinimized():
            return
        self.pages["overview"].refresh()
        cur = self.stack.currentWidget()
        if hasattr(cur, "refresh") and cur is not self.pages["tasks"]:
            cur.refresh()

    def refresh_all(self):
        if self._closed:
            return
        for p in self.pages.values():
            if hasattr(p, "refresh"):
                p.refresh()
        self.pages["council"].refresh_tasks()

    def closeEvent(self, ev):
        if self._force_quit or not QSystemTrayIcon.isSystemTrayAvailable():
            self._shutdown()
            ev.accept()
            return
        box = QMessageBox(self)
        box.setWindowTitle("关闭窗口")
        box.setText("要继续在托盘运行吗？")
        box.setInformativeText("继续运行可以让 DSH 执行器保持连接。")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("最小化到托盘")
        box.button(QMessageBox.No).setText("退出程序")
        r = box.exec()
        if r == QMessageBox.Cancel:
            ev.ignore()
        elif r == QMessageBox.Yes:
            ev.ignore()
            self.hide()
            self.tray.showMessage(APP_NAME_CN, "已最小化到托盘，服务继续运行。")
        else:
            self._shutdown()
            ev.accept()

    def really_quit(self):
        self._force_quit = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        """幂等关闭：停 API 线程 → 停看门狗 → 关数据库 → 收托盘（A.3）。

        顺序不能反：先停 HTTP 与看门狗，最后才关连接，否则会有关线程
        拿着已关闭的连接去查询（sqlite3.ProgrammingError）。
        """
        if self._closed:
            return
        self._closed = True
        # 先切断信号，避免 watch 线程在库关闭后再回调界面（已关连接查询会抛 ProgrammingError）
        try:
            if self.server.receivers(self.server.started):
                self.server.started.disconnect(self.on_server)
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass
        try:
            self.server.stop_watcher()
        except Exception:
            pass
        try:
            self.council.stop_watchdog()
        except Exception:
            pass
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                pass
        try:
            self.tray.hide()
        except Exception:
            pass


def run(data_dir: str | None = None, port: int | None = None,
        host: str | None = None, db=None, council=None) -> int:
    """启动 GUI。

    db / council 由入口创建时直接传入，本函数不再二次 open_council——
    同进程开两个 SQLite 连接会让 Windows 的恢复操作撞文件锁（A.1）。
    只有在没人传的时候（工具脚本、独立调试）才自己开一次，并在退出时关掉。
    """
    settings = Settings.load(Path(data_dir).expanduser() if data_dir else None)
    if port:
        settings.api_port = int(port)
    if host:
        settings.api_host = host
    # 监听闸门统一由 server.check_bind_host 执行，GUI 不再另写一套判断（P1-3.1）
    from .server import BindError, check_bind_host
    try:
        check_bind_host(settings.api_host)
    except BindError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    own_db = False
    if db is None or council is None:
        db, council = open_council(settings)
        own_db = True

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME_CN)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow(settings, council, db=db)
    win.show()
    try:
        rc = app.exec()
    finally:
        try:
            council.stop_watchdog()
        except Exception:
            pass
        if own_db and db is not None:
            try:
                db.close()
            except Exception:
                pass
    return rc
