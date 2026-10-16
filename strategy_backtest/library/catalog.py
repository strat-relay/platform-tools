"""Offline research catalog. Importing this module performs no platform registration."""
from __future__ import annotations
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Definition:
    key: str
    family: str
    name: str
    mode: str
    hypothesis: str
    context: str = 'H1'
    execution: str = 'M15'
    lookback: int = 20
    fast: int = 20
    slow: int = 50
    atr_period: int = 14
    atr_multiple: float = 1.2
    tolerance_atr: float = .15
    buffer_atr: float = .1
    min_r: float = 1.
    target_r: float = 2.
    setup_bars: int = 8
    session_tz: str = 'UTC'
    range_start: int = 0
    range_end: int = 60
    trade_start: int = 60
    trade_end: int = 240
    symbols: tuple[str, ...] = ()
    target_policy: str = 'FIXED_R'
    entry_policy: str = 'NEXT_OPEN_RECONSTRUCTED'
    max_entry_gap_atr: float = .25
    lifecycle: str = 'RESEARCH'
    execution_eligible: bool = False
    version: str = 'V1'

    def __post_init__(self):
        import math
        from zoneinfo import ZoneInfo
        if self.context not in {'M15','H1','H4','D1'} or self.execution not in {'M15','H1','H4','D1'}:
            raise ValueError('unsupported timeframe')
        for name in ('lookback','fast','slow','atr_period','setup_bars'):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,int) or value<2:
                raise ValueError(f'{name} must be integer >=2')
        for name in ('atr_multiple','tolerance_atr','buffer_atr','min_r','target_r','max_entry_gap_atr'):
            value=getattr(self,name)
            if not math.isfinite(value) or value<=0:
                raise ValueError(f'{name} must be finite and positive')
        if any(not isinstance(getattr(self,n),int) or not 0<=getattr(self,n)<1440
               for n in ('range_start','range_end','trade_start','trade_end')):
            raise ValueError('session endpoints must be local minute-of-day')
        ZoneInfo(self.session_tz)
        if self.entry_policy!='NEXT_OPEN_RECONSTRUCTED' or self.execution_eligible or self.lifecycle!='RESEARCH':
            raise ValueError('library definitions must stay research-only')

    @property
    def strategy_id(self):
        return f'{self.family.upper().replace(" ", "_").replace("/", "_")}_{self.key.upper()}'

    def manifest(self):
        return asdict(self)


def _d(key, family, name, mode, hypothesis, **kw):
    return Definition(key, family, name, mode, hypothesis, **kw)


DEFINITIONS = (
    # ICT names identify these exact mechanical research interpretations.
    _d('2022_model', 'ICT', 'ICT 2022 Model', 'ict_fvg',
       'H1 structural bias; prior-channel liquidity sweep/reclaim; displacement and micro break; FVG forms then later retraces with directional rejection.'),
    _d('silver_bullet', 'ICT', 'Silver Bullet', 'ict_fvg',
       'ICT sweep/displacement/FVG composition restricted to 10:00–11:00 America/New_York; confirmation must complete strictly before window end.',
       session_tz='America/New_York', trade_start=600, trade_end=660),
    _d('judas_swing', 'ICT', 'Judas Swing', 'range_fade',
       'Complete 00:00–05:00 London range; 07:00–10:00 false break and close back inside; reject in reclaim direction; target opposite range edge.',
       session_tz='Europe/London', range_start=0, range_end=300, trade_start=420, trade_end=600, target_policy='OPPOSITE_RANGE'),
    _d('ote_retracement', 'ICT', 'OTE Retracement', 'ote',
       'H1 confirmed trend and latest confirmed impulse; enter on rejection of its 62–79 percent retracement after anchors become knowable.'),
    _d('killzone_liquidity_sweep', 'ICT', 'Killzone Liquidity Sweep', 'sweep',
       'Prior-channel sweep and same-candle reclaim with rejection during 07:00–10:00 Europe/London.',
       session_tz='Europe/London', trade_start=420, trade_end=600),
    # Liquidity Displacement remains bound to the existing evaluator below.
    _d('bos_retest', 'SMC', 'BOS Retest', 'bos_retest',
       'H1 confirmed trend; close beyond latest same-direction confirmed M15 swing; subsequent retest and directional confirmation.'),
    _d('choch_reversal', 'SMC', 'CHOCH Reversal', 'choch',
       'Established M15 confirmed structural trend; opposing close beyond latest confirmed swing; later retest and reversal confirmation.'),
    _d('order_block_retracement', 'SMC', 'Order Block Retracement', 'order_block',
       'Displacement closes beyond prior channel; last opposing candle within five bars defines a wick-range block; later rejection on its retest.'),
    _d('fvg_retracement', 'SMC', 'FVG Retracement', 'fvg',
       'H1 structural bias and displacement; three-bar wick FVG becomes knowable at third close; later uninvalidated-zone rejection.'),
    _d('opening_range_break', 'Breakout', 'Opening Range Break', 'range_break',
       '09:30–10:00 New York opening-range proxy; first completed close crossing outside during 10:00–12:00; fixed-R target. OTC/crypto session proxy, not official exchange open.',
       session_tz='America/New_York', range_start=570, range_end=600, trade_start=600, trade_end=720),
    _d('tokyo_range_break', 'Breakout', 'Tokyo Range Break', 'range_break',
       'Complete 09:00–12:00 Asia/Tokyo range; first close crossing outside during 12:00–15:00 Tokyo.',
       session_tz='Asia/Tokyo', range_start=540, range_end=720, trade_start=720, trade_end=900),
    _d('london_breakout', 'Breakout', 'London Breakout', 'range_break',
       'Complete 00:00–07:00 London range; first close crossing outside during 08:00–11:00 London.',
       session_tz='Europe/London', range_start=0, range_end=420, trade_start=480, trade_end=660),
    _d('ny_open_breakout', 'Breakout', 'NY Open Breakout', 'range_break',
       'Complete 08:00–09:30 New York pre-open range; first outside close during 09:30–11:00. Distinct pre-open hypothesis from Opening Range Break.',
       session_tz='America/New_York', range_start=480, range_end=570, trade_start=570, trade_end=660),
    _d('donchian_breakout', 'Breakout', 'Donchian Breakout', 'channel_break',
       'Completed close crosses prior 20-bar channel; signal candle excluded from its own range.'),
    _d('eur_mean_reversion', 'Mean Reversion', 'EUR Mean Reversion', 'bollinger',
       'EURUSD only: close re-enters prior 20-bar two-standard-deviation band after an outside close; target frozen mean.',
       symbols=('EURUSD',), target_policy='MEAN'),
    _d('session_vwap_fade', 'Mean Reversion', 'Session VWAP Fade', 'vwap_fade',
       'UTC-day tick-volume VWAP from completed prior bars; prior close exceeds 1.5 ATR deviation then current closes inward; target frozen VWAP.', target_policy='VWAP'),
    _d('bollinger_reversion', 'Mean Reversion', 'Bollinger Reversion', 'bollinger',
       'Close returns inside prior 20-bar two-standard-deviation envelope after an outside close; target frozen mean.', target_policy='MEAN'),
    _d('rsi_reversion', 'Mean Reversion', 'RSI Reversion', 'rsi_reversion',
       'Cutler RSI14 crosses back above 30/below 70; target prior 20-bar mean.', target_policy='MEAN'),
    _d('session_reversal', 'Mean Reversion', 'Session Reversal', 'range_fade',
       'Complete 00:00–06:00 UTC range; 06:00–10:00 false break with rejection back inside; target opposite edge.',
       range_start=0, range_end=360, trade_start=360, trade_end=600, target_policy='OPPOSITE_RANGE'),
    _d('ema_pullback', 'Trend Following', 'EMA Pullback', 'ema_pullback',
       'EMA20 above/below EMA50 on completed prior M15; current candle touches EMA20 and confirms in trend direction.'),
    _d('ema_20_50', 'Trend Following', 'EMA 20/50 Trend', 'ema_cross',
       'EMA20 crosses EMA50 on completed M15 close; structural recent-extreme stop.'),
    _d('ema_50_200', 'Trend Following', '50/200 Trend', 'ema_cross',
       'EMA50 crosses EMA200 on completed M15 close; structural recent-extreme stop.', fast=50, slow=200),
    _d('structure_pullback', 'Trend Following', 'Structure Pullback', 'structure_pullback',
       'H1 confirmed HH/HL or LH/LL; M15 retest of latest same-direction H1 swing with rejection.'),
    _d('trendline_break_retest', 'Trend Following', 'Trendline Break + Retest', 'trendline_retest',
       'Two confirmed M15 lows/highs form an existing sloping line; close breaks it; subsequent retest confirms in break direction.'),
    _d('btc_momentum_flow', 'Momentum', 'BTC Momentum Flow', 'momentum',
       'BTCUSD only: prior EMA20/50 alignment and close beyond prior 20-bar range with 1.5 ATR directional body.', symbols=('BTCUSD',), atr_multiple=1.5),
    _d('atr_expansion', 'Momentum', 'ATR Expansion', 'expansion',
       'Current range >=2 prior ATR and directional body >=65 percent range; close breaks prior five-bar extreme.', atr_multiple=2., lookback=5),
    _d('breakout_momentum', 'Momentum', 'Breakout Momentum', 'momentum',
       'Prior EMA20/50 alignment; close breaks prior 20-bar range with >=1.2 prior ATR directional body.'),
    _d('ema_momentum', 'Momentum', 'EMA Momentum', 'ema_momentum',
       'EMA20/50 direction agrees with three strictly directional closes; current directional body crosses above 1.2 prior ATR.'),
    _d('volatility_continuation', 'Momentum', 'Volatility Continuation', 'volatility_continuation',
       'Prior displacement creates a range; within eight bars a later close breaks its extreme in the same direction.'),
    _d('equal_level_sweep', 'Liquidity', 'Equal High/Low Sweep', 'equal_sweep',
       'Latest two confirmed M15 highs/lows within 0.15 prior ATR form liquidity; wick sweeps their outer extreme then closes inside with rejection.'),
    _d('previous_day_sweep', 'Liquidity', 'Previous Day High/Low Sweep', 'day_sweep',
       'Complete prior UTC day high/low; sweep and close back inside with directional rejection.'),
    _d('previous_week_sweep', 'Liquidity', 'Previous Week High/Low Sweep', 'week_sweep',
       'Complete prior UTC Monday–Monday range; sweep/reclaim with rejection. Missing bars fail closed; no inferred holiday calendar.'),
    _d('session_liquidity_reclaim', 'Liquidity', 'Session Liquidity Reclaim', 'range_fade',
       '00:00–06:00 London range; 07:00–11:00 sweep/reclaim with rejection; target opposite edge.',
       session_tz='Europe/London', range_start=0, range_end=360, trade_start=420, trade_end=660, target_policy='OPPOSITE_RANGE'),
    _d('liquidity_run_continuation', 'Liquidity', 'Liquidity Run Continuation', 'liquidity_run',
       'Sweep/reclaim in H1 trend direction; displacement and local 5-bar close break within eight bars; next-open continuation.'),
    _d('asian_range_fade', 'Session-Based', 'Asian Range Fade', 'range_fade',
       '09:00–12:00 Tokyo range; 12:00–15:00 sweep/reclaim rejection; opposite edge target.',
       session_tz='Asia/Tokyo', range_start=540, range_end=720, trade_start=720, trade_end=900, target_policy='OPPOSITE_RANGE'),
    _d('london_open_drive', 'Session-Based', 'London Open Drive', 'session_drive',
       '08:00–09:00 London: >=1.2 ATR displacement breaks prior 20-bar channel; one episode per direction/session.',
       session_tz='Europe/London', trade_start=480, trade_end=540),
    _d('ny_overlap_continuation', 'Session-Based', 'New York Overlap Continuation', 'session_pullback',
       '08:00–11:00 New York: EMA20/50 trend pullback and rejection, one episode per direction/session.',
       session_tz='America/New_York', trade_start=480, trade_end=660),
    _d('utc_vwap_reclaim', 'Session-Based', 'UTC VWAP Reclaim', 'vwap_reclaim',
       'Current close crosses frozen prior UTC-day tick-volume VWAP in H1 trend direction.'),
    _d('opening_range_retest', 'Session-Based', 'Opening Range Retest', 'range_retest',
       '09:30–10:00 New York range freezes; outside close followed by later boundary retest with confirmation before 12:00.',
       session_tz='America/New_York', range_start=570, range_end=600, trade_start=600, trade_end=720),
    _d('h4_structure_pullback', 'Swing Position', 'H4 Structure Pullback', 'structure_pullback',
       'Completed H4 HH/HL or LH/LL; H1 pullback to latest directional H4 structural pivot with rejection.', context='H4', execution='H1'),
    _d('daily_channel_breakout', 'Swing Position', 'Daily Channel Breakout', 'channel_break',
       'Completed D1 close outside prior 20-day channel; next daily open reference entry.', context='D1', execution='D1'),
    _d('daily_ema_pullback', 'Swing Position', 'Daily EMA Pullback', 'ema_pullback',
       'Daily EMA20/50 trend; daily EMA20 pullback and confirmation; next daily open.', context='D1', execution='D1'),
    _d('weekly_range_break_retest', 'Swing Position', 'Weekly Range Break + Retest', 'weekly_retest',
       'Complete prior UTC Monday–Monday H4 range; H4 close outside followed by later retest within eight H4 bars.', context='D1', execution='H4'),
    _d('h4_momentum_continuation', 'Swing Position', 'H4 Momentum Continuation', 'momentum',
       'H4 EMA20/50 alignment and displacement close beyond prior 20-bar H4 channel.', context='D1', execution='H4'),
)

# Existing evaluators retain their IDs, versions, parameters and behavior. These
# bindings are catalog references, not alternate implementations or registrations.
EXISTING = (
    {'family': 'Price Action', 'name': 'Support/Resistance Retest', 'strategy_id': 'PRICE_ACTION_SUPPORT_RESISTANCE_RETEST', 'version': 'V1', 'module': 'strategy_backtest.price_action_sr_retest'},
    {'family': 'Price Action', 'name': 'Engulfing Continuation', 'strategy_id': 'PRICE_ACTION_ENGULFING_CONTINUATION', 'version': 'V1', 'module': 'strategy_backtest.price_action_engulfing_continuation'},
    {'family': 'Price Action', 'name': 'Engulfing Reversal', 'strategy_id': 'PRICE_ACTION_ENGULFING_REVERSAL', 'version': 'V1', 'module': 'strategy_backtest.price_action_engulfing_reversal'},
    {'family': 'Price Action', 'name': 'Morning/Evening Star Reversal', 'strategy_id': 'PRICE_ACTION_STAR_REVERSAL', 'version': 'V1', 'module': 'strategy_backtest.price_action_star_reversal'},
    {'family': 'Price Action', 'name': 'Rejection / Pin-Bar Reversal', 'strategy_id': 'PRICE_ACTION_PIN_BAR_REVERSAL', 'version': 'V1', 'module': 'strategy_backtest.price_action_pin_bar_reversal'},
    {'family': 'SMC', 'name': 'Liquidity Displacement Scalp', 'strategy_id': 'LIQUIDITY_DISPLACEMENT_SCALP_V1', 'version': 'V1', 'module': 'liquidity_displacement'},
)
BY_KEY = {d.key: d for d in DEFINITIONS}
assert len(BY_KEY) == len(DEFINITIONS)
