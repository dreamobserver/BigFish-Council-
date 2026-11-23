"""GUI 冒烟测试：离屏启动主窗口，确认四个页面都能构造且不崩.

用法： QT_QPA_PLATFORM=offscreen python tools/gui_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ok, fail = [], []


def check(name, cond, extra=""):
    (ok if cond else fail).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from bigfish import gui
    from bigfish.config import Settings
    from bigfish.core import Council
    from bigfish.db import Database

    tmp = Path(tempfile.mkdtemp(prefix="bfc-gui-"))
    s = Settings.load(tmp)
    s.api_port = 3097
    s.ensure_dirs()
    db = Database(s.db_path)
    db.init_schema()
    council = Council(db)

    t = council.create_task("GUI 冒烟", "看看页面会不会崩", "本机", "low", "print(1)")

    council.submit_for_review(t["id"])
    council.add_review(t["id"], "gpt", "可以", "approve", kind="vote")
    council.add_review(t["id"], "gemini", "可以", "approve", kind="vote")

    app = QApplication.instance() or QApplication(sys.argv)
    win = gui.MainWindow(s, council)
    check("主窗口构造成功", win is not None)
    keys = list(win.pages.keys())
    check("五个页面都在", win.stack.count() == len(keys) == 5,
          "count=%d keys=%d" % (win.stack.count(), len(keys)))

    for name in keys:
        try:
            win.goto(name)
            app.processEvents()
            check("页面可切换：" + name, win.stack.currentWidget() is win.pages[name])
        except Exception as exc:
            check("页面可切换：" + name, False, exc)

    check("状态栏文案", isinstance(win.api_label.text(), str), win.api_label.text())
    check("待审批任务被识别", council.get_task(t["id"])["status"] == "PENDING_APPROVAL")

    win.really_quit()
    app.processEvents()
    print()
    print("通过 %d 项，失败 %d 项" % (len(ok), len(fail)))
    if fail:
        print("失败项：" + "、".join(fail))
        return 1
    print("GUI 冒烟通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
