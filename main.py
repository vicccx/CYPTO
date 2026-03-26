# intraday/main.py
"""
入口：多标的 IBKR 实时数据 → MultiEngine → Rich TUI
运行: python -m main
"""
import sys
import os
import signal
import logging
import time

# 修正導入路徑，確保根目錄在 sys.path 中
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 加入 C:\ 以便識別 intraday 包
parent_root = os.path.dirname(project_root)
if parent_root not in sys.path:
    sys.path.insert(0, parent_root)

from intraday.config.products import BTC_CONFIG, XAU_CONFIG, ETH_CONFIG
from intraday.app.multi_engine import MultiEngine, SymbolSpec
from intraday.display.terminal_rich import RichTerminalDisplay

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# ── 用户配置 ─────────────────────────────────────────────────────
TWS_PORT    = 7497       # paper=7497 / live=7496
MIN_SAMPLES = 5          # 每窗口最少N笔即结算

# ── 持久化配置 ───────────────────────────────────────────────────
DB_PATH     = r"C:\intraday\results\intraday.duckdb"
PARQUET_DIR = r"C:\intraday\results\parquet"
BATCH_SIZE  = 10         # 每累积 N 条批量写入一次（同时每条即时落盘）
# ── DuckDB Parquet 快照配置（供 Streamlit 实时看板读取）────────────
SNAPSHOT_DIR      = r"C:\intraday\results\snapshots"
SNAPSHOT_INTERVAL = 3.0    # 导出间隔（秒）
SNAPSHOT_TAIL     = 500    # 每次导出最新 N 行
ENABLE_SNAPSHOT   = True   # 设为 False 可全局关闭快照
SYMBOLS = [
    SymbolSpec(BTC_CONFIG),
    SymbolSpec(XAU_CONFIG),
    SymbolSpec(ETH_CONFIG),
]
# ─────────────────────────────────────────────────────────────────


def main() -> None:
    # ── 1. 多标的引擎 ──────────────────────────────────────────
    if not os.path.exists(os.path.dirname(DB_PATH)):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not os.path.exists(SNAPSHOT_DIR):
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    if not os.path.exists(PARQUET_DIR):
        os.makedirs(PARQUET_DIR, exist_ok=True)

    me = MultiEngine(
        port=TWS_PORT,
        min_samples=MIN_SAMPLES,
        base_client_id=10,
        db_path=DB_PATH,
        parquet_dir=PARQUET_DIR,
        batch_size=BATCH_SIZE,
        snapshot_dir=SNAPSHOT_DIR,
        snapshot_interval_sec=SNAPSHOT_INTERVAL,
        snapshot_tail_rows=SNAPSHOT_TAIL,
        enable_snapshot=ENABLE_SNAPSHOT,
    )
    for spec in SYMBOLS:
        me.add(spec)

    # ── 2. TUI ────────────────────────────────────────────────
    sym_list = me.symbols()

    # decay_stats_fns
    decay_stats_fns = {
        s: (lambda eng: lambda: eng.physics_stats.get_decay_stats(time.time()))(
            me.get_slot(s).engine
        )
        for s in sym_list
    }

    display = RichTerminalDisplay(
        symbols=sym_list,
        session_fn=me.get_slot(sym_list[0]).engine.get_current_session,
        dist_fns={
            s: me.get_slot(s).engine.get_price_distribution
            for s in sym_list
        },
        decay_stats_fns=decay_stats_fns,
        history_size=10,
        refresh_per_second=0.5,
    )
    me.bridge.add_handler(display.on_window)

    # ── 3. 优雅退出 ──────────────────────────────────────────
    def on_exit(sig, frame):
        try:
            print("\n── 最终状态 ────────────────────────")
            for sym, info in me.status().items():
                print(f"  {sym:6s}  {info['local_symbol']:12s}  "
                      f"ticks={info['tick_count']}  errors={info['error_count']}")
            try:
                counts = me.db_stats()
                print(f"  📦 window_results={counts['window_results']}  "
                      f"physics_stats={counts['physics_stats']}")
            except:
                pass
            me.stop_all()
        except:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT,  on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    # ── 4. 连接所有 Feed ──────────────────────────────────
    syms_str = " / ".join(sym_list)
    print(f"◈ 正在連接幣安行情 ({syms_str})...")

    if not me.connect_all(timeout=10.0):
        print("❌ 部分标的连接失败，请检查网络或币安 API 状态")
        on_exit(None, None)

    for sym, info in me.status().items():
        print(f"  ✅ {sym:6s} → {info['local_symbol']}")

    # ── 5. TUI 主线程阻塞 ────────────────
    display.start(flush_fn=me.flush_all)


if __name__ == "__main__":
    main()
