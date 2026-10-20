#!/usr/bin/env python3
"""
crawler.py
Canonical Enacted Crawler with Robots.txt Compliance & Host Rate Limiting.
Swapp Technologies LLC (Virginia)

Rules:
1. User-Agent: EnactedBot/1.0 (+https://enacted.app/bot/)
2. Every fetch honours robots.txt: a disallowed path is skipped and reported, never fetched.
3. One request at a time per host, with at least 5 s between requests.
"""

import os
import sys
import time
import ssl
import logging
import urllib.request
import urllib.parse
import urllib.error
from urllib.robotparser import RobotFileParser
from typing import Tuple, Dict, Optional, List

logger = logging.getLogger("enacted.crawler")

ENACTED_USER_AGENT = "EnactedBot/1.0 (+https://enacted.app/bot/)"


class RobotsDisallowedError(Exception):
    """Raised when an attempt is made to fetch a URL prohibited by robots.txt."""
    pass


class EnactedCrawler:
    """
    Deterministic crawler respecting robots.txt and host rate limits.
    """
    DEFAULT_USER_AGENT = ENACTED_USER_AGENT
    DEFAULT_MIN_INTERVAL = 5.0  # seconds

    def __init__(
        self,
        user_agent: Optional[str] = None,
        min_interval: float = 5.0,
        obey_robots: bool = True,
        opener: Optional[urllib.request.OpenerDirector] = None
    ):
        self.user_agent = user_agent or os.environ.get("ENACTED_USER_AGENT", self.DEFAULT_USER_AGENT)
        self.min_interval = min_interval
        self.obey_robots = obey_robots
        self.opener = opener
        self.last_request_time: Dict[str, float] = {}
        self.robot_parsers: Dict[str, RobotFileParser] = {}
        self.disallowed_log: List[Dict[str, str]] = []

    def register_robots_txt(self, host: str, content: str):
        """Allows injecting robots.txt content for testing fixtures."""
        host_key = host.lower()
        parser = RobotFileParser()
        parser.parse(content.splitlines())
        self.robot_parsers[host_key] = parser

    def get_robots_parser(self, url: str) -> Optional[RobotFileParser]:
        """Retrieves or fetches the RobotFileParser for a given URL's host."""
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return None

        if host in self.robot_parsers:
            return self.robot_parsers[host]

        if not self.obey_robots:
            return None

        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)

        try:
            req = urllib.request.Request(robots_url, headers={"User-Agent": self.user_agent})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            if self.opener is not None:
                resp = self.opener.open(req, timeout=10)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=10)

            if resp.getcode() == 200:
                raw_lines = resp.read().decode("utf-8", errors="replace").splitlines()
                parser.parse(raw_lines)
            elif resp.getcode() in (401, 403):
                parser.disallow_all = True
            else:
                parser.allow_all = True
        except urllib.error.HTTPError as he:
            if he.code in (401, 403):
                parser.disallow_all = True
            else:
                parser.allow_all = True
        except Exception:
            parser.allow_all = True

        self.robot_parsers[host] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        """
        Evaluates whether the URL is permitted under robots.txt.
        If disallowed, records the incident in disallowed_log and returns False.
        """
        if not self.obey_robots:
            return True

        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        parser = self.get_robots_parser(url)

        if parser is None:
            return True

        allowed = parser.can_fetch(self.user_agent, url)
        if not allowed:
            entry = {
                "url": url,
                "host": host,
                "user_agent": self.user_agent,
                "reason": "disallowed_by_robots_txt",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            self.disallowed_log.append(entry)
            print(f"[Crawler] Disallowed by robots.txt: {url}", file=sys.stderr)
            return False

        return True

    def wait_if_needed(self, host: str):
        """Enforces at least min_interval seconds between consecutive requests to the same host."""
        if self.min_interval <= 0:
            return

        host_key = host.lower()
        now = time.time()
        last = self.last_request_time.get(host_key, 0.0)
        elapsed = now - last

        if elapsed < self.min_interval:
            sleep_duration = self.min_interval - elapsed
            time.sleep(sleep_duration)

        self.last_request_time[host_key] = time.time()

    def fetch(self, url: str, timeout: int = 15) -> Tuple[bool, int, bytes, str]:
        """
        Fetches the URL respecting robots.txt and host rate limits.
        Returns (is_allowed, status_code, content_bytes, error_message).
        """
        if not self.can_fetch(url):
            return False, 403, b"", "disallowed_by_robots_txt"

        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()

        self.wait_if_needed(host)

        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            if self.opener is not None:
                resp = self.opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)

            status_code = resp.getcode()
            body = resp.read()
            return True, status_code, body, ""
        except urllib.error.HTTPError as he:
            return True, he.code, b"", f"HTTPError {he.code}: {he.reason}"
        except urllib.error.URLError as ue:
            return True, 0, b"", f"URLError: {ue.reason}"
        except Exception as exc:
            return True, 0, b"", f"Fetch error: {exc}"
