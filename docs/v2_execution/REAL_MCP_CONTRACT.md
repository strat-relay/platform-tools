# V2 real bridge contract

The production V2 client targets the host-local execution bridge at
`POST /mcp`. It never uses the isolated `/advance_fence` or `/submit` proof-server
protocol and it never constructs `BridgeFenceSimulator`.

The request is JSON-RPC `tools/call`:

```json
{
  "jsonrpc": "2.0",
  "id": "<attempt_id>",
  "method": "tools/call",
  "params": {
    "name": "mt5_canonical_order_send",
    "arguments": {
      "schema_version": 1,
      "action": 1,
      "magic": 0,
      "symbol": "XAUUSDm",
      "volume": 0.01,
      "price": 4320.0,
      "sl": 4300.0,
      "tp": 4400.0,
      "deviation": 50,
      "type": 0,
      "type_filling": 1,
      "type_time": 0,
      "expiration": 0,
      "comment": "SRV2:<attempt_id>",
      "canonical_request_text": "<versioned MT5 request text>",
      "request_fingerprint": "<MT5 wire fingerprint>",
      "idempotency_key": "<attempt_id>"
    }
  }
}
```

The signed OD-06 authorization is carried in `X-Fence-*` headers. The HMAC
payload is the exact sorted, compact UTF-8 JSON representation of:

`resource, generation, attempt_id, tool, request_fingerprint, scope_class, exp, key_id`.

The fence fingerprint is separately computed over
`{instrument, direction, volume, stop_price, target_price}`. For the real MT5
request, `instrument` is the resolved broker symbol (`XAUUSDm`), while the
PostgreSQL `ExecutionIntent.instrument` remains canonical (`XAUUSD`). Numeric
values are normalized as floats on both sides before canonicalization.

The bridge validates the signature, account, expiry, generation, tool, request
fingerprint, and durable idempotency claim before `pending.put()` can reach the
EA. The `SRV2:<attempt_id>` comment is the deterministic broker-visible
correlation token used by reconciliation through `mt5_history`, `mt5_orders`, and
`mt5_positions`. Reconciliation requires an exact token match and never retries
an ambiguous attempt.

