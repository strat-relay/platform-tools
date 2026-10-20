"""Pure causal breakout/retest episode transitions; no I/O or outcome access."""
from copy import deepcopy

VERSION = 'breakout-retest-episode.v1'


def directional_close(bar, direction, *, body_fraction, close_location):
    span = bar.high-bar.low
    location = (bar.close-bar.low if direction == 1 else bar.high-bar.close)
    return (span > 0 and direction*(bar.close-bar.open) > 0
            and abs(bar.close-bar.open) >= body_fraction*span
            and location >= close_location*span)


def transition(state, bars, *, bias, atr, tick, policy):
    """One completed bar. All thresholds/levels refer to prior known observations.

    Caller owns validation, tick identity, context availability, persistence and
    next-open observation. Returned state never aliases supplied mutable state.
    """
    s = deepcopy(state)
    bar, previous = bars[-1], bars[-2]
    at = bar.close_timestamp
    if not bias:
        return {}, 'NO_STRUCTURAL_BIAS', None
    if s and s['direction'] != bias:
        return {}, 'SEQUENCE_INVALIDATED', None
    sign = bias
    tol = policy.tolerance_atr*(s.get('atr', atr))
    if s and sign*(bar.close-s['boundary']) < -tol:
        return {}, 'SEQUENCE_INVALIDATED', None
    phase = s.get('phase', 'IDLE')
    if phase == 'WAIT_RESET':
        if sign*(bar.close-s['boundary']) <= 0:
            return {}, 'SEQUENCE_INVALIDATED', None
        return s, 'AWAITING_SEQUENCE_CONFIRMATION', None
    if phase == 'IDLE':
        prior = bars[:-1][-policy.lookback:]
        boundary = max(x.high for x in prior) if sign == 1 else min(x.low for x in prior)
        if sign*(bar.close-boundary) < policy.min_breakout_atr*atr:
            return {}, 'NO_SETUP', None
        if not directional_close(bar, sign, body_fraction=policy.breakout_body_fraction,
                                 close_location=policy.breakout_close_location):
            return {}, 'BREAKOUT_QUALITY_REJECTED', None
        return {'phase':'WAIT_RETEST','direction':sign,'boundary':boundary,'atr':atr,
                'break_at':at,'extreme':bar.low if sign == 1 else bar.high}, 'AWAITING_SEQUENCE_CONFIRMATION', None
    if phase == 'WAIT_RETEST':
        if at > s['break_at']+policy.setup_bars*900:
            s['phase']='WAIT_RESET'
            return s, 'SEQUENCE_EXPIRED', None
        s['extreme'] = min(s['extreme'],bar.low) if sign == 1 else max(s['extreme'],bar.high)
        touched = bar.low <= s['boundary']+tol and bar.high >= s['boundary']-tol
        if not touched:
            return s, 'AWAITING_SEQUENCE_CONFIRMATION', None
        if not (sign*(bar.close-s['boundary']) > tol and
                directional_close(bar, sign, body_fraction=policy.retest_body_fraction,
                                  close_location=policy.retest_close_location)):
            s['phase']='WAIT_RESET'
            return s, 'FIRST_RETEST_REJECTED', None
        proposal = {'kind':'INITIAL','stop_extreme':s['extreme'],'break_at':s['break_at'],
                    'boundary':s['boundary'],'confirmation_at':at,'episode_atr':s['atr']}
        s['phase']='ENTRY_PENDING'
        return s, 'PROPOSAL', proposal
    if phase == 'ENTRY_PENDING':
        return s, 'AWAITING_SEQUENCE_CONFIRMATION', None
    if phase == 'ACTIVE':
        if bar.open_timestamp <= s['last_entry_time']:
            s['favorable_extreme'] = max(s['favorable_extreme'],bar.high) if sign == 1 else min(s['favorable_extreme'],bar.low)
            return s, 'AWAITING_SEQUENCE_CONFIRMATION', None
        if sign*(bar.close-previous.close) < 0:
            s.update(phase='PULLBACK',pullback_at=at,
                     resume_level=s['favorable_extreme'],
                     extreme=bar.low if sign == 1 else bar.high)
        else:
            s['favorable_extreme'] = max(s['favorable_extreme'],bar.high) if sign == 1 else min(s['favorable_extreme'],bar.low)
        return s, 'AWAITING_SEQUENCE_CONFIRMATION', None
    if phase == 'PULLBACK':
        if at > s['pullback_at']+policy.setup_bars*900:
            s['phase']='WAIT_RESET'
            return s, 'SEQUENCE_EXPIRED', None
        s['extreme'] = min(s['extreme'],bar.low) if sign == 1 else max(s['extreme'],bar.high)
        if sign*(bar.close-s['resume_level']) <= tick:
            return s, 'AWAITING_SEQUENCE_CONFIRMATION', None
        if (sign*(bar.close-s['last_entry_price']) < policy.min_layer_progress_atr*s['atr'] or
                not directional_close(bar, sign, body_fraction=policy.breakout_body_fraction,
                                      close_location=policy.breakout_close_location)):
            return s, 'LAYER_PROGRESS_REJECTED', None
        proposal = {'kind':'LAYER','stop_extreme':s['extreme'],'break_at':s['break_at'],
                    'boundary':s['boundary'],'confirmation_at':at,'episode_atr':s['atr'],
                    'previous_entry_price':s['last_entry_price'],'pullback_at':s['pullback_at'],
                    'resume_level':s['resume_level']}
        s['phase']='ENTRY_PENDING'
        return s, 'PROPOSAL', proposal
    raise ValueError('unknown breakout episode phase')
