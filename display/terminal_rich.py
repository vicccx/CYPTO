# intraday/display/terminal_rich.py
"""
Rich 终端实时 TUI
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from ..core.types import WindowResult
    from ..core.price_distribution import DeltaPStats
    from ..core.signals import SignalEvent


class RichTerminalDisplay:
    """
    终端實時 TUI
    """

    def __init__(
        self,
        # ── 单标的参数 (向后兼容) ──
        symbol: str = "",
        dist_fn: Optional[Callable] = None,
        # ── 多标的参数 ──
        symbols: Optional[List[str]] = None,
        dist_fns: Optional[Dict[str, Callable]] = None,
        # ── 衰减统计回调 ──
        decay_stats_fns: Optional[Dict[str, Callable]] = None,
        # ── 通用参数 ──
        session_fn: Optional[Callable[[], str]] = None,
        signal_fn: Optional[Callable] = None,
        history_size: int = 10,
        refresh_per_second: int = 4,
    ) -> None:
        # 统一为多标的模式
        if symbols:
            self._symbols: List[str] = list(symbols)
            self._dist_fns: Dict[str, Callable] = dist_fns or {}
        else:
            # 单标的向后兼容
            sym = symbol or "SYM"
            self._symbols = [sym]
            self._dist_fns = {sym: dist_fn} if dist_fn else {}

        self._decay_stats_fns: Dict[str, Callable] = decay_stats_fns or {}
        self.session_fn = session_fn
        self.signal_fn = signal_fn
        self.refresh_per_second = refresh_per_second

        # 每个标的独立历史队列 + 最新窗口
        self._history: Dict[str, deque["WindowResult"]] = {
            s: deque(maxlen=history_size) for s in self._symbols
        }
        self._latest: Dict[str, Optional["WindowResult"]] = {
            s: None for s in self._symbols
        }

        self._console = Console()
        self._live: Optional[Live] = None
        self._start_time = time.time()
        self._running = False

    # ── 向后兼容属性 ───────────────────────────────────────────
    @property
    def symbol(self) -> str:
        return self._symbols[0] if self._symbols else ""

    # ── 外部接口 ──────────────────────────────────────────────

    def on_window(self, result: "WindowResult") -> None:
        """Bridge 回调"""
        sym = getattr(result, "symbol", "") or self._symbols[0]
        if sym not in self._history:
            sym = self._symbols[0]
        self._history[sym].append(result)
        self._latest[sym] = result

    def start(self, flush_fn=None) -> None:
        """阻塞启动"""
        self._running = True
        with Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=self.refresh_per_second,
            screen=True,
        ) as live:
            self._live = live
            try:
                while self._running:
                    if flush_fn:
                        flush_fn()
                    live.update(self._build_layout())
                    time.sleep(1.0 / self.refresh_per_second)
            except KeyboardInterrupt:
                pass
        self._live = None
        self._running = False

    def run(self, flush_fn=None) -> None:
        """別名，與 main.py 一致"""
        self.start(flush_fn)

    def start_in_thread(self) -> None:
        """在后台线程启动"""
        import threading
        t = threading.Thread(target=self.start, daemon=True, name="rich-tui")
        t.start()

    def stop(self) -> None:
        self._running = False

    # ── 布局构建 ──────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        if len(self._symbols) == 1:
            return self._build_single_layout()
        return self._build_multi_layout()

    def _build_single_layout(self) -> Layout:
        sym = self._symbols[0]
        layout = Layout()
        layout.split_column(
            Layout(self._header(),           name="header", size=3),
            Layout(name="body"),
            Layout(self._history_table(sym), name="footer", size=10),
        )
        layout["body"].split_row(
            Layout(self._metrics_panel(sym), name="left",  ratio=1),
            Layout(self._dist_panel(sym),    name="right", ratio=1),
        )
        return layout

    def _build_multi_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header(),        name="header", size=3),
            Layout(name="body"),
            Layout(self._multi_history(), name="footer", size=10),
        )
        syms = self._symbols
        col_layouts = []
        for s in syms:
            col = Layout(name=f"col_{s}", ratio=1)
            col.split_column(
                Layout(self._metrics_panel(s), name=f"metrics_{s}", ratio=3),
                Layout(self._dist_panel(s),    name=f"dist_{s}",    ratio=4),
            )
            col_layouts.append(col)
        layout["body"].split_row(*col_layouts)
        return layout

    def _header(self) -> Panel:
        session = self.session_fn() if self.session_fn else "–"
        elapsed = int(time.time() - self._start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        sym_str = " / ".join(self._symbols)
        txt = Text()
        txt.append(f"  ◈ {sym_str}  Fill Quality Monitor", style="bold cyan")
        txt.append("    时段: ", style="white")
        txt.append(f"{session}", style="bold yellow")
        txt.append(f"    运行: {h:02d}:{m:02d}:{s:02d}", style="dim white")
        return Panel(txt, box=box.HORIZONTALS, style="on grey7")

    def _metrics_panel(self, sym: str) -> Panel:
        w = self._latest[sym]
        if not w:
            return Panel(f"[dim]等待 {sym} 第一个窗口...[/dim]",
                         title=f"[bold]{sym}[/bold] 实时指标")

        tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), expand=True)
        tbl.add_column("指标",  style="dim",        width=14)
        tbl.add_column("值",    style="bold white",  min_width=14)
        tbl.add_column("状态",  min_width=10)

        if w.impact_bps < 2:   ic = "[bold green]● 低[/bold green]"
        elif w.impact_bps < 5: ic = "[bold yellow]● 中[/bold yellow]"
        else:                  ic = "[bold red]● 高[/bold red]"

        buy_pct = w.buy_volume / w.total_volume if w.total_volume else 0.5
        if buy_pct > 0.6:    dc = "[green]▲ 偏买[/green]"
        elif buy_pct < 0.4:  dc = "[red]▼ 偏卖[/red]"
        else:                dc = "[white]━ 平衡[/white]"

        pl = int(w.price_levels)
        if pl <= 2:   pl_str = f"[green]{pl} 层[/green]"
        elif pl <= 5: pl_str = f"[yellow]{pl} 层[/yellow]"
        else:         pl_str = f"[red]{pl} 层[/red]"

        tbl.add_row("时间",       w.time_label,                             "")
        tbl.add_row("VWAP",      f"{w.vwap:.2f}",                          "")
        tbl.add_row("区间",      f"{w.low_price:.2f} ~ {w.high_price:.2f}", "")
        tbl.add_row("价格离散度", pl_str,                                    "")
        tbl.add_row("冲击成本",  f"{w.impact_bps:.2f} bps",                 ic)
        tbl.add_row("每手冲击",  f"${w.impact_dollar:.2f}",                 "")
        tbl.add_row("成交量",    f"{w.total_volume:.2f} 手",                  "")
        tbl.add_row("Tick 数",   f"{int(w.tick_count)}",                     "")
        tbl.add_row("Delta",     f"{w.delta:+.2f}",                          dc)
        tbl.add_row("買/賣",     f"{w.buy_volume:.2f} / {w.sell_volume:.2f}",        "")

        return Panel(tbl, title=f"[bold]{sym}[/bold] 实时指标", border_style="cyan")

    def _dist_panel(self, sym: str) -> Panel:
        fn = self._dist_fns.get(sym)
        dist = fn() if fn else None
        if not dist:
            return Panel(f"[dim]{sym} ΔP 样本积累中...[/dim]", title=f"[bold]{sym}[/bold] φ(ΔP) 密度")

        tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), expand=True)
        tbl.add_column("参数",  style="dim",        width=14)
        tbl.add_column("值",    style="bold white",  min_width=14)
        tbl.add_column("说明",  style="dim",        min_width=10)

        ks = "[green]≈ 正态[/green]"
        if dist.kurt > 2.0: ks = "[red]厚尾 ⚠[/red]"
        elif dist.kurt > 0.5: ks = "[yellow]轻厚尾[/yellow]"

        ss = "[green]对称[/green]"
        if dist.skew > 0.5: ss = "[yellow]右偏[/yellow]"
        elif dist.skew < -0.5: ss = "[yellow]左偏[/yellow]"

        tbl.add_row("样本数 n",    f"{dist.n}",            "")
        tbl.add_row("均值 μ",      f"{dist.mean:+.5f}",    "E[ΔP]")
        tbl.add_row("标准差 σ",    f"{dist.std:.5f}",      "波动幅度")
        tbl.add_row("偏度 γ₁",     f"{dist.skew:+.4f}",    ss)
        tbl.add_row("超额峰度 γ₂", f"{dist.kurt:+.4f}",    ks)
        tbl.add_row("─" * 12,     "─" * 12,               "")

        ds_fn = self._decay_stats_fns.get(sym)
        if ds_fn:
            ds = ds_fn()
            if ds is not None:
                lr = ds.liquidity_ratio
                lr_s = f"[green]{lr:.2f}x[/green]"
                if lr >= 2.0: lr_s = f"[red]{lr:.2f}x ▲[/red]"
                elif lr >= 1.2: lr_s = f"[yellow]{lr:.2f}x[/yellow]"
                tbl.add_row("衰减 k",   f"{ds.k_effective:.5f}", f"半衰={ds.half_life_sec:.0f}s")
                tbl.add_row("有效覆盖", f"{ds.coverage_sec:.0f}s", "基准300s")
                tbl.add_row("流动性倍", lr_s, "動態梯度")
                tbl.add_row("─" * 12,  "─" * 12, "")

        bar = _mini_hist(dist.hist_density, width=24)
        tbl.add_row("φ(ΔP)", bar, "经验密度")
        p_val = 0.5
        if dist.std > 0:
            z = -dist.mean / dist.std
            p_val = 0.5 * _erfc_approx(z / math.sqrt(2))
        tbl.add_row("P(ΔP>0)", f"{p_val:.1%}", "上涨概率")
        return Panel(tbl, title=f"[bold]{sym}[/bold] φ(ΔP) 密度", border_style="magenta")

    def _history_table(self, sym: str) -> Panel:
        tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim", expand=True)
        tbl.add_column("时间", width=9)
        tbl.add_column("VWAP", width=9, justify="right")
        tbl.add_column("离散度", width=5, justify="right")
        tbl.add_column("冲击bps", width=8, justify="right")
        tbl.add_column("成交量", width=7, justify="right")
        tbl.add_column("買/賣", width=11, justify="right")
        for w in reversed(self._history[sym]):
            tbl.add_row(w.time_label, f"{w.vwap:.2f}", f"{int(w.price_levels)}", f"{w.impact_bps:.2f}", f"{w.total_volume:.2f}", f"{w.buy_volume:.2f}/{w.sell_volume:.2f}")
        return Panel(tbl, title=f"[bold]{sym} 窗口历史[/bold]", border_style="dim")

    def _multi_history(self) -> Panel:
        tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim", expand=True)
        tbl.add_column("时间", width=9)
        tbl.add_column("标的", width=6, justify="center")
        tbl.add_column("VWAP", width=10, justify="right")
        tbl.add_column("冲击bps", width=8, justify="right")
        tbl.add_column("買/賣", width=11, justify="right")
        all_rows = []
        for sym in self._symbols:
            for w in self._history[sym]:
                all_rows.append((sym, w))
        all_rows.sort(key=lambda x: x[1].window_end, reverse=True)
        for sym, w in all_rows[:10]:
            tbl.add_row(w.time_label, sym, f"{w.vwap:.2f}", f"{w.impact_bps:.2f}", f"{w.buy_volume:.2f}/{w.sell_volume:.2f}")
        return Panel(tbl, title="[bold]窗口历史[/bold]", border_style="dim")

def _mini_hist(density: List[float], width: int = 24) -> str:
    if not density: return "–"
    mx = max(density) or 1.0
    bars = " ▂▃▄▅▆▇█"
    step = max(1, len(density) // width)
    res = ""
    for i in range(0, min(len(density), width * step), step):
        idx = min(int(density[i] / mx * (len(bars) - 1)), len(bars) - 1)
        res += bars[idx]
    return res

def _erfc_approx(x: float) -> float:
    if x < 0: return 2.0 - _erfc_approx(-x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    p = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    return p * math.exp(-x * x)
