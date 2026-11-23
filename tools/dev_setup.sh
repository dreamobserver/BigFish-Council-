#!/usr/bin/env bash
# 独立开发环境安装脚本（Linux / VPS 容器 / WSL）
#
# 用途：在一台没有 PySide6 图形依赖的机器上，也能跑完整单测 + 自检 + 服务。
# 不修改系统包、不用 apt（本容器 apt 索引不可写），全部装进项目内 .venv。
#
#   bash tools/dev_setup.sh            # 装依赖
#   bash tools/dev_setup.sh --test     # 装完顺手跑全量测试
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
echo "== 项目根目录: $ROOT"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 $PY，请先装 Python 3.10+" >&2
  exit 1
fi
echo "== Python: $("$PY" -V 2>&1)"

if [ ! -x .venv/bin/python ]; then
  echo "== 创建 venv"
  "$PY" -m venv .venv || { echo "venv 创建失败：某些发行版需要 python3-venv" >&2; exit 1; }
fi

echo "== 升级 pip（缺 pip 时先引导）"
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  echo "   venv 内没有 pip，尝试 get-pip.py"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py || { echo "下载 get-pip.py 失败" >&2; exit 1; }
  .venv/bin/python /tmp/get-pip.py || exit 1
fi
.venv/bin/python -m pip install -U pip >/dev/null || { echo "升级 pip 失败" >&2; exit 1; }

echo "== 安装 requirements.txt"
.venv/bin/python -m pip install -r requirements.txt || exit 1

echo
echo "== 完成。常用命令："
echo "   跑单测:   .venv/bin/python -m unittest discover -s tests -t ."
echo "   自检:     .venv/bin/python tools/selfcheck.py"
echo "   网络检查: .venv/bin/python tools/net_check.py"
echo "   启动服务: .venv/bin/python -m bigfish --headless --port 3096"
echo
echo "   无显示器跑 GUI 冒烟，需要先准备图形库（见 docs/TESTLOG-v0.1.1.md 第 2.2 节）："
echo "   LD_LIBRARY_PATH=<图形库目录> QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_smoke.py"

if [ "${1:-}" = "--test" ]; then
  echo
  echo "== 跑全量测试"
  # 先把输出落到临时文件，再 tail：直接用管道会让 tail 的退出码覆盖 unittest 的，
  # 测试失败时脚本仍然返回 0，CI 与人工都会误判成通过。
  LOG="${TMPDIR:-/tmp}/bfc-dev-setup-test.log"
  # 注意：这里不能写成 "\$LOG"，反斜杠会把变量转义成字面量，
  # 日志会落到当前目录下名为 $LOG 的文件里，$? 也取不到真实退出码。
  RC=0
  QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t . >"${LOG}" 2>&1 || RC=$?
  tail -6 "${LOG}"
  if [ "${RC}" -ne 0 ]; then
    echo "== 测试失败（退出码 ${RC}），完整日志见 ${LOG}" >&2
    exit "${RC}"
  fi
  echo "== 测试通过"
fi
