# intraday/app/main_engine.py
import time
from typing import Optional, List, TYPE_CHECKING

from intraday.core.types import Tick, WindowResult, PhysicsStatsResult
from intraday.core.liquidity_engine import LiquidityEngine
from intraday.core.physics_stats import EconophysicsStats
from intraday.config.sessions import TimeFunctionSwitch, MarketSession
from intraday.config.products import ProductConfig
from intraday.analytics.signal_engine import SignalEngine
from intraday.analytics.session_adapter import SessionAwareAdapter, SessionParams

if TYPE_CHECKING:
    from intraday.display.bridge import DisplayBridge
    from intraday.core.price_distribution import DeltaPStats
    from intraday.core.signals import SignalEvent
    from intraday.core.persistence import Persistence

class MainQuantEngine:
    """
    微观流动性与经济物理学主引擎 (Central Brain)
    """
    def __init__(self, product_config: ProductConfig,
                 history_size: int = 1000,
                 min_samples: int = 30,
                 clt_n_agg: int = 30,
                 persistence: Optional["Persistence"] = None):
        self.config = product_config
        self.time_switch = TimeFunctionSwitch()

        initial_session = self.time_switch.get_current_session()
        self.current_session_name = initial_session.session_name

        self.liquidity_engine = LiquidityEngine(
            product_config=self.config,
            initial_window_sec=initial_session.window_size_sec
        )

        self.physics_stats = EconophysicsStats(
            history_size=history_size,
            clt_n_agg=clt_n_agg,
            min_samples=min_samples,
        )

        self._bridge: Optional["DisplayBridge"] = None
        self._persistence: Optional["Persistence"] = persistence
        self.signal_engine = SignalEngine()
        self.signal_engine.register(product_config)
        self._session_adapter = SessionAwareAdapter()
        self._session_adapter.on_change(self._on_session_params_change)

    def _on_session_params_change(self, old: SessionParams, new: SessionParams) -> None:
        self.liquidity_engine.set_window_size(new.window_sec)
        self.physics_stats._tracker.resize(new.history_size)
        self.physics_stats.min_samples = new.min_samples

    def set_bridge(self, bridge: "DisplayBridge") -> None:
        self._bridge = bridge

    def get_current_session(self) -> str:
        return self.current_session_name.value

    def get_price_distribution(self) -> Optional["DeltaPStats"]:
        return self.physics_stats._tracker.get_stats()

    def get_recent_signals(self, n: int = 20) -> List["SignalEvent"]:
        return self.signal_engine.recent(n)

    def on_signal(self, callback) -> None:
        self.signal_engine.on_signal(callback)

    def flush_window(self, now: float) -> None:
        result = self.liquidity_engine.flush(now)
        if result is not None:
            result.symbol  = self.config.symbol
            result.session = self.current_session_name.value
            if self._bridge:
                self._bridge.emit(result)
            active_config = self.time_switch.get_current_session(now)
            self._evaluate_market_state(result, active_config)

    def on_tick_received(self, price: float, volume: int, side: str, timestamp: float = None):
        if timestamp is None:
            timestamp = time.time()
        active_config = self.time_switch.get_current_session(timestamp)
        if active_config.session_name == MarketSession.MAINTENANCE:
            return
        if self.current_session_name != active_config.session_name:
            self.current_session_name = active_config.session_name
        self._session_adapter.tick(self.current_session_name.value)
        tick = Tick(price=price, volume=volume, timestamp=timestamp, side=side)
        window_result = self.liquidity_engine.process_tick(tick)
        if window_result is not None:
            window_result.symbol  = self.config.symbol
            window_result.session = self.current_session_name.value
            if self._bridge:
                self._bridge.emit(window_result)
            self._evaluate_market_state(window_result, active_config)

    def _evaluate_market_state(self, window_result: WindowResult, session_config):
        adjusted_impact = session_config.impact_factor * (
            window_result.impact_bps / session_config.adv_multiplier
        )
        physics_result = self.physics_stats.update(
            window_end=window_result.window_end,
            current_vwap=window_result.vwap,
            volume=window_result.total_volume,
        )
        if self._persistence is not None:
            decay = self.physics_stats.get_decay_stats(window_result.window_end)
            self._persistence.write_window(window_result, decay)
            if physics_result is not None:
                self._persistence.write_physics(
                    physics_result,
                    decay=decay,
                    symbol=getattr(window_result, "symbol", ""),
                )
        if physics_result is None:
            self.signal_engine.evaluate(window_result, None)
            return
        self.signal_engine.evaluate(window_result, physics_result)
