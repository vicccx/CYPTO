# intraday/data/binance_feed.py
"""
Binance 实时 Tick 数据接口 (Websocket)
"""
import json
import logging
import threading
import time
import os
from typing import Callable, List, Optional

import websocket
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 載入 .env
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"))

class BinanceTickFeed:
    """
    Binance 實時 Tick 數據接口 (Websocket)
    """

    def __init__(self, symbol: str = "BTCUSDT", is_futures: bool = False) -> None:
        self.symbol = symbol.lower()
        # 自動判斷是否為合約
        if "paxg" in self.symbol or "nvda" in self.symbol:
            self.is_futures = False
        else:
            self.is_futures = is_futures
            
        self._callbacks: List[Callable] = []
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        
        self.api_key = os.getenv("BINANCE_API_KEY")
        
        self.tick_count: int = 0
        self.error_count: int = 0
        
        # 基礎 URL
        if self.is_futures:
            self.base_url = f"wss://fstream.binance.com/ws/{self.symbol}@aggTrade"
        else:
            self.base_url = f"wss://stream.binance.com:9443/ws/{self.symbol}@aggTrade"

    @classmethod
    def from_preset(
        cls,
        symbol: str,
        last_trade_date: str = "",
        port: int = 0,
        client_id: int = 0,
    ) -> "BinanceTickFeed":
        return cls(symbol=symbol)

    def subscribe(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    def start_async(self) -> threading.Thread:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"binance-{self.symbol}",
        )
        self._thread.start()
        return self._thread

    def wait_connected(self, timeout: float = 20.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            self._ws.close()
        logger.info("[Binance] 已停止，共收 %d ticks", self.tick_count)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            logger.info(f"[Binance][{self.symbol}] 嘗試連接: {self.base_url}")
            self._ws = websocket.WebSocketApp(
                self.base_url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
            # 設定超時防止死掛
            self._ws.run_forever(ping_interval=30, ping_timeout=10)
            if self._stop_event.is_set():
                break
            logger.warning("[Binance][%s] 連接中斷，5秒後重連...", self.symbol)
            time.sleep(5)

    def _on_open(self, ws):
        logger.warning(f"[Binance][{self.symbol}] ✅ Websocket 已連接 (API Key: {'OK' if self.api_key else 'None'})")
        self._connected.set()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            # 兼容有些接口返回的是列表或單個對象
            if isinstance(data, list): data = data[0]
            
            price = float(data.get('p', 0))
            volume = float(data.get('q', 0))
            ts = data.get('T', time.time() * 1000) / 1000.0
            side = "sell" if data.get('m', False) else "buy"
            
            if price > 0:
                self._dispatch(price, volume, ts, side)
        except Exception as e:
            logger.error(f"[Binance] 解析錯誤: {e}")
            self.error_count += 1

    def _on_error(self, ws, error):
        logger.error(f"[Binance][{self.symbol}] WS 錯誤: {error}")
        self.error_count += 1

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning(f"[Binance][{self.symbol}] ❌ WS 關閉: {close_msg}")
        self._connected.clear()

    def _dispatch(self, price: float, volume: float, ts: float, side: str) -> None:
        self.tick_count += 1
        for cb in self._callbacks:
            try:
                cb(price=price, volume=volume, timestamp=ts, side=side)
            except Exception:
                self.error_count += 1
