# intraday/data/ibkr_feed.py
"""
IBKR 实时 Tick 数据接口 (ib_insync)
"""
import copy
import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


# ── 合约配置 ─────────────────────────────────────────────────────

@dataclass
class IBKRConfig:
    host: str            = "127.0.0.1"
    port: int            = 7497          # paper=7497 / live=7496
    client_id: int       = 1
    timeout: float       = 15.0
    symbol: str          = "MGC"
    sec_type: str        = "FUT"
    exchange: str        = "COMEX"
    currency: str        = "USD"
    last_trade_date: str = ""            # 如 "202506"，空=自动最近月
    multiplier: str      = "10"


_PRESETS: dict = {
    "MGC": IBKRConfig(symbol="MGC", exchange="COMEX", multiplier="10"),
    "GC":  IBKRConfig(symbol="GC",  exchange="COMEX", multiplier="100"),
    "MES": IBKRConfig(symbol="MES", exchange="CME",   multiplier="5"),
    "MNQ": IBKRConfig(symbol="MNQ", exchange="CME",   multiplier="2"),
    "ES":  IBKRConfig(symbol="ES",  exchange="CME",   multiplier="50"),
    "NQ":  IBKRConfig(symbol="NQ",  exchange="CME",   multiplier="20"),
    "CL":  IBKRConfig(symbol="CL",  exchange="NYMEX", multiplier="1000"),
    "SI":  IBKRConfig(symbol="SI",  exchange="COMEX", multiplier="5000"),
    "ZB":  IBKRConfig(symbol="ZB",  exchange="CBOT",  multiplier="1000"),
}


# ── 主类 ─────────────────────────────────────────────────────────

class IBKRTickFeed:
    """
    IBKR tickByTick 逐笔成交订阅
    """

    def __init__(self, config: IBKRConfig) -> None:
        self._cfg = config
        self._callbacks: List[Callable] = []
        self._ib = None
        self._contract = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._last_price: Optional[float] = None
        self._last_tick_time: float = time.time()
        self.tick_count: int = 0
        self.error_count: int = 0
        self.resubscribe_count: int = 0
        self._consecutive_resubscribes: int = 0  # 连续重订阅次数

    # watchdog：无 tick 超过此秒数则重新订阅
    WATCHDOG_TIMEOUT: int = 600   # 10 分钟
    # 连续重订阅超过此次数则全量断线重连
    MAX_RESUBSCRIBES: int = 3

    # ── 工厂方法 ──────────────────────────────────────────────────

    @classmethod
    def from_preset(
        cls,
        symbol: str,
        last_trade_date: str = "",
        port: int = 7497,
        client_id: int = 1,
    ) -> "IBKRTickFeed":
        if symbol not in _PRESETS:
            # 兼容模式：如果不在预设中，创建一个通用的 FUT 配置
            cfg = IBKRConfig(symbol=symbol, exchange="CME", last_trade_date=last_trade_date)
        else:
            cfg = copy.copy(_PRESETS[symbol])
            
        cfg.port = port
        cfg.client_id = client_id
        cfg.last_trade_date = last_trade_date
        return cls(cfg)

    # ── 公共接口 ──────────────────────────────────────────────────

    def subscribe(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    def start_async(self) -> threading.Thread:
        """非阻塞：IBKR 在后台守护线程运行"""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"ibkr-{self._cfg.symbol}",
        )
        self._thread.start()
        return self._thread

    def wait_connected(self, timeout: float = 20.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._ib:
            try:
                self._ib.disconnect()
            except Exception:
                pass
        logger.info("[IBKR] 已断开，共收 %d ticks", self.tick_count)

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    @property
    def local_symbol(self) -> str:
        if self._contract:
            return self._contract.localSymbol
        return self._cfg.symbol

    # ── 内部：连接 & 订阅 ──────────────────────────────────────────

    def _run(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            from ib_insync import IB, Future
        except ImportError:
            raise RuntimeError("请先安装: pip install ib_insync")

        reconnect_delay = 30
        while not self._stop_event.is_set():
            self._run_once(IB, Future)
            if self._stop_event.is_set():
                break
            logger.warning(
                "[IBKR][%s] 连接断开，%d 秒后重连...",
                self._cfg.symbol, reconnect_delay,
            )
            self._stop_event.wait(timeout=reconnect_delay)

        loop.close()

    def _run_once(self, IB, Future) -> None:
        cfg = self._cfg
        ib = IB()

        connected = False
        client_id = cfg.client_id
        for attempt in range(5):
            try:
                ib.connect(
                    cfg.host, cfg.port,
                    clientId=client_id,
                    timeout=cfg.timeout,
                    readonly=False,
                )
                connected = True
                break
            except Exception as e:
                err = str(e)
                if "already in use" in err or "326" in err:
                    client_id += 1
                    ib = IB()
                else:
                    self.error_count += 1
                    return

        if not connected:
            self.error_count += 1
            return

        self._ib = ib

        # ── 解析合约 ──
        import datetime as _dt
        today_str = _dt.date.today().strftime("%Y%m%d")

        raw = Future(
            symbol=cfg.symbol,
            exchange=cfg.exchange,
            currency=cfg.currency,
            lastTradeDateOrContractMonth=cfg.last_trade_date,
            multiplier=cfg.multiplier,
        )
        try:
            details = ib.reqContractDetails(raw)
        except Exception:
            ib.disconnect()
            return

        if not details:
            ib.disconnect()
            return

        def _expiry_key(d) -> str:
            s = d.contract.lastTradeDateOrContractMonth
            return s if len(s) == 8 else s + "01"

        active = [d for d in details if _expiry_key(d) >= today_str]
        if not active:
            active = details

        active.sort(key=_expiry_key)
        self._contract = active[0].contract
        
        # ── 订阅 ──
        ib.reqTickByTickData(
            self._contract,
            tickType="Last",
            numberOfTicks=0,
            ignoreSize=False,
        )
        ib.pendingTickersEvent += self._on_pending_tickers

        self._connected.set()
        self._last_tick_time = time.time()
        self._consecutive_resubscribes = 0

        # ── 事件循环 ──
        try:
            while not self._stop_event.is_set():
                ib.waitOnUpdate(timeout=1.0)
                elapsed = time.time() - self._last_tick_time
                if elapsed > self.WATCHDOG_TIMEOUT:
                    try:
                        ib.cancelTickByTickData(self._contract, "Last")
                        time.sleep(2)
                        ib.reqTickByTickData(self._contract, tickType="Last", numberOfTicks=0, ignoreSize=False)
                        self._consecutive_resubscribes += 1
                        if self._consecutive_resubscribes >= self.MAX_RESUBSCRIBES:
                            break
                    except Exception:
                        self.error_count += 1
        except Exception:
            self.error_count += 1
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
            self._ib = None

    def _on_pending_tickers(self, tickers) -> None:
        for ticker in tickers:
            for t in ticker.tickByTicks:
                try:
                    price = float(t.price)
                    volume = int(t.size)
                    ts = t.time.timestamp() if hasattr(t.time, "timestamp") else time.time()
                    self._dispatch(price, volume, ts)
                except Exception:
                    pass

    def _dispatch(self, price: float, volume: int, ts: float) -> None:
        if price <= 0 or volume <= 0:
            return

        if self._last_price is None or price > self._last_price:
            side = "buy"
        elif price < self._last_price:
            side = "sell"
        else:
            side = "buy"

        self._last_price = price
        self._last_tick_time = time.time()
        self._consecutive_resubscribes = 0
        self.tick_count += 1

        for cb in self._callbacks:
            try:
                cb(price=price, volume=volume, timestamp=ts, side=side)
            except Exception:
                self.error_count += 1
