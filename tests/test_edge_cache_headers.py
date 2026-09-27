"""Edge (Cloudflare) cache headers: only successful strategy/signal read models are cacheable,
with stale-if-error so the edge can answer while the tunnel to the origin stalls. Everything else,
and every error or write, stays no-store. The router sends constant CORS allow headers so a cached
response is valid for the Console regardless of which request populated the cache."""
from __future__ import annotations

import re
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from platform_api import signals as module
from platform_api.signals import EDGE_CACHE_CONTROL, edge_cache_control

ROOT = Path(__file__).resolve().parents[1]


class StubApi:
    def execute(self, method, target, body=None):
        if "missing" in target:
            return 503, {"error": "SOURCE_UNAVAILABLE"}
        return 200, {"data": {"method": method, "target": target}}


class EdgeCacheHeaderTests(unittest.TestCase):
    def test_policy(self):
        cacheable = ["/api/v1/strategies", "/api/v1/strategies/", "/api/v1/strategies/S/instances/i",
                     "/api/v1/signals?limit=25&offset=0", "/api/v1/signals/SIG_1"]
        for target in cacheable:
            self.assertEqual(edge_cache_control("GET", target, 200), EDGE_CACHE_CONTROL, target)
        never = [("GET", "/api/v1/broker/account", 200), ("GET", "/api/v1/trade-manager/live", 200),
                 ("GET", "/api/v1/v2-execution/risk-policy", 200), ("GET", "/api/v1/strategiesX", 200),
                 ("GET", "/api/v1/strategies", 503), ("GET", "/api/v1/signals/SIG_1", 404),
                 ("POST", "/api/v1/strategies/S/lifecycle", 200)]
        for method, target, status in never:
            self.assertEqual(edge_cache_control(method, target, status), "no-store", (method, target, status))
        self.assertIn("stale-if-error=", EDGE_CACHE_CONTROL)

    def test_http_server_sends_the_policy(self):
        server = module.create_server("127.0.0.1", 0, api=StubApi(), allowed_origins={"https://console.stratrelay.app"})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base}/api/v1/strategies") as r:
                self.assertEqual(r.headers["Cache-Control"], EDGE_CACHE_CONTROL)
            with urlopen(f"{base}/api/v1/broker/account") as r:
                self.assertEqual(r.headers["Cache-Control"], "no-store")
            with self.assertRaises(HTTPError) as err:
                urlopen(f"{base}/api/v1/strategies/missing")
            self.assertEqual(err.exception.headers["Cache-Control"], "no-store")
            err.exception.close()
            with urlopen(Request(f"{base}/api/v1/strategies/S/lifecycle", data=b"{}", method="POST")) as r:
                self.assertEqual(r.headers["Cache-Control"], "no-store")
        finally:
            server.shutdown()
            server.server_close()

    def test_router_cors_allow_headers_are_constant(self):
        conf = (ROOT / "deploy/platform_api_router/nginx.conf").read_text()
        origin_map = re.search(r"map \$http_origin \$cors_allow_origin \{(.*?)\}", conf, re.S).group(1)
        self.assertEqual(origin_map.split(), ["default", '"https://console.stratrelay.app";'])
        credentials_map = re.search(r"map \$cors_allow_origin \$cors_allow_credentials \{(.*?)\}", conf, re.S).group(1)
        self.assertEqual(credentials_map.split(), ["default", '"true";'])


if __name__ == "__main__":
    unittest.main()
