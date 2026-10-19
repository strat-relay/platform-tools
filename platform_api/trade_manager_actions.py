"""HTTP boundary for operator recommendations; no broker client is used here."""
import json
from trade_management.operator_actions import ActionConflict, request_action, validate_request
from .trade_manager_mode import TradeManagerModeApi


def save_action(body, connect_fn):
    try:
        request = validate_request(json.loads((body or b'').decode()))
    except (ValueError, UnicodeDecodeError) as exc:
        return 400, TradeManagerModeApi._error('INVALID_REQUEST', str(exc))
    try:
        conn = connect_fn(readonly=False)
        try:
            result = request_action(conn, request)
        finally:
            conn.close()
    except ActionConflict as exc:
        return 409, TradeManagerModeApi._error('ACTION_CONFLICT', str(exc))
    except Exception:
        return 503, TradeManagerModeApi._error('SOURCE_UNAVAILABLE', 'Action result is uncertain; retry with the same requestId')
    return 200, TradeManagerModeApi._ok(result)
