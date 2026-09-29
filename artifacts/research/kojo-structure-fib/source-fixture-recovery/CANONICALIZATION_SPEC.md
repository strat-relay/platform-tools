# KOJO_STRUCTURE_FIB source-fixture canonicalization

Schema: `stratrelay.historical_bars.v1`

The canonical artifact is a single JSON object serialized as UTF-8 with
`ensure_ascii=true`, sorted object keys not used, compact separators `,` and
`:`, and exactly one trailing LF. The field order is the order in the file:

`schema`, `provider`, `canonical_symbol`, `provider_symbol`, `timeframe`,
`timezone`, `bars`.

Bars are strictly ascending by UTC candle-open timestamp. Each bar contains,
in this order, `timestamp`, `open`, `high`, `low`, and `close`. Timestamps are
UTC ISO-8601 strings with second precision and a `Z` suffix. Prices are
decimal strings rounded to exactly three fractional digits, matching the
precision present in the acquired source fixture. Volume is preserved in the
raw provider artifact but intentionally excluded from this v1 canonical OHLC
fingerprint because the KOJO source semantics require OHLC only.

Duplicate timestamps are invalid. Missing or irregular market intervals are
not filled or synthesized; the persisted bar sequence is the provider output.
The raw artifact and its byte SHA-256 remain separate from the canonical
artifact. The historical `2139eae...` fingerprint is legacy metadata only and
was not reused for the new canonical file.

