"""Redis-backed broker view: the read-only broker state the API serves (account, positions,
orders, history, symbols), refreshed by one populator instead of read from the MT5 bridge on
every request.

    populator (python -m broker_view.service) -> read bridge 22347, every 15 s -> Redis
    API / Trade Manager live projection        -> Redis first; bridge only when missing/stale
"""
