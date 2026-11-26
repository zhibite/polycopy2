"""Minimal JSON HTTP client with retries (stdlib only)."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpClient:
    def __init__(self, timeout: int = 20, retries: int = 4,
                 logger: logging.Logger | None = None):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.log = logger or logging.getLogger("polycopy.http")

    def get_json(self, url: str, params: dict | None = None):
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA, "Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (400, 404):
                    self.log.debug("HTTP %s %s", exc.code, url)
                    return None
                self.log.warning("HTTP %s %s", exc.code, url)
                wait = 10.0 * (attempt + 1) if exc.code == 429 else 1.5 * (attempt + 1)
                time.sleep(wait)
            except Exception as exc:  # timeout, reset, bad JSON
                last = exc
                self.log.warning("GET %s failed: %s", url, exc)
                time.sleep(1.5 * (attempt + 1))
        self.log.error("giving up on %s: %s", url, last)
        return None

    def post_json(self, url: str, body):
        """POST a JSON body and decode the JSON response (batch CLOB reads)."""
        data = json.dumps(body).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(
                    url, data=data, method="POST",
                    headers={"User-Agent": UA, "Accept": "application/json",
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (400, 404):
                    self.log.debug("HTTP %s %s", exc.code, url)
                    return None
                self.log.warning("HTTP %s %s", exc.code, url)
                wait = 10.0 * (attempt + 1) if exc.code == 429 else 1.5 * (attempt + 1)
                time.sleep(wait)
            except Exception as exc:  # timeout, reset, bad JSON
                last = exc
                self.log.warning("POST %s failed: %s", url, exc)
                time.sleep(1.5 * (attempt + 1))
        self.log.error("giving up on %s: %s", url, last)
        return None
