"""Fetch allowlisted HTTPS pages with pinned public IPs and bounded bodies."""

import http.client
import ipaddress
import re
import socket
import ssl
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError
from app.genai.skills.reference_readers.common import (
    check_bytes, color_values, frequent, profile, reference_error,
)

_REDIRECTS = {301, 302, 303, 307, 308}
_CSS_VALUE = r"([^;}]+)"


def _host(url, allowed_hosts):
    """Validate an allowlisted HTTPS origin and return its canonical host."""
    try:
        if not isinstance(url, str):
            raise TypeError("URL must be text")
        parsed = urlsplit(url)
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (AttributeError, UnicodeError, ValueError) as exc:
        reference_error("website reference URL is malformed", exc)
    except TypeError as exc:
        reference_error("website reference URL must be text", exc)
    try:
        allowed = {str(item).strip().encode("idna").decode("ascii").lower()
                   for item in allowed_hosts if isinstance(item, str) and item.strip()}
    except UnicodeError as exc:
        reference_error("website allowlist contains an invalid hostname", exc)
    if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password:
        raise AppError("website references must use HTTPS and cannot include credentials")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise AppError("website references must use an allowlisted hostname, not an IP literal")
    if port not in (None, 443) or hostname not in allowed:
        raise AppError("website reference host is not allowlisted on HTTPS port 443")
    return parsed, hostname


def _public_address(host):
    """Resolve a host and reject any non-public result before connecting."""
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        reference_error("website reference host could not be resolved", exc)
    if not addresses:
        raise AppError("website reference host has no usable address")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address.split("%")[0]).is_global:
                raise AppError("website reference resolves to a non-public address")
        except ValueError as exc:
            reference_error("website reference host resolved to an invalid address", exc)
    return sorted(addresses)[0]


def _fetch_once(host, address, path):
    """Fetch one HTTPS response through a socket pinned to its checked IP."""
    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((address, 443), timeout=Limits.WEBSITE_TIMEOUT_SECONDS)
            self.sock = self._context.wrap_socket(raw, server_hostname=host)

    connection = PinnedHTTPSConnection(host, timeout=Limits.WEBSITE_TIMEOUT_SECONDS,
                                       context=ssl.create_default_context())
    try:
        connection.request("GET", path, headers={
            "Host": host,
            "User-Agent": "DynamicSkillReferenceReader/1.0",
            "Accept": "text/html",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        response = connection.getresponse()
        if response.status in _REDIRECTS:
            return response.status, response.getheader("Location"), b""
        if response.status != 200:
            raise AppError(f"website reference returned HTTP {response.status}")
        if "text/html" not in (response.getheader("Content-Type") or "").lower():
            raise AppError("website reference must return text/html")
        length = response.getheader("Content-Length")
        if length:
            try:
                if int(length) > Limits.MAX_WEBSITE_BYTES:
                    raise AppError("website reference exceeds the response size limit")
            except ValueError as exc:
                reference_error("website reference has an invalid Content-Length", exc)
        data = response.read(Limits.MAX_WEBSITE_BYTES + 1)
        if len(data) > Limits.MAX_WEBSITE_BYTES:
            raise AppError("website reference exceeds the response size limit")
        return response.status, None, data
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        reference_error("website reference could not be fetched safely", exc)
    finally:
        connection.close()


def _record_css(metrics, css):
    """Collect explicit typography, color, and spacing values from a CSS fragment."""
    metrics["fonts"].extend(re.findall(r"font-family\s*:\s*" + _CSS_VALUE, css, re.I))
    metrics["colors"].extend(re.findall(r"(?:color|background-color)\s*:\s*" +
                                        r"(#[0-9a-fA-F]{6})", css, re.I))
    metrics["sizes"].extend(re.findall(
        r"font-size\s*:\s*(\d+(?:\.\d+)?(?:px|pt|rem|em))", css, re.I))
    metrics["gaps"].extend(re.findall(r"(?:padding|margin)\s*:\s*([^;}]+)", css, re.I))


class _StyleParser(HTMLParser):
    """Collect only style attributes and element counts; never retain page text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {"fonts": [], "colors": [], "sizes": [], "gaps": []}
        self.elements = Counter()
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        self.elements[tag] += 1
        attributes = dict(attrs)
        if attributes.get("style"):
            _record_css(self.values, attributes["style"])
        self.in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            _record_css(self.values, data)


def _css_metrics(data):
    """Count common page elements and extract explicit style-token values."""
    parser = _StyleParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    parser.close()
    return {
        "fonts": frequent(parser.values["fonts"]),
        "colors": color_values(parser.values["colors"]),
        "font_sizes": frequent(parser.values["sizes"]),
        "spacing": frequent(parser.values["gaps"]),
        "elements": {tag: parser.elements[tag]
                     for tag in ("h1", "h2", "h3", "ul", "ol", "li", "table",
                                 "blockquote", "img", "section")},
    }


def inspect_website(url, allowed_hosts):
    """Fetch an allowlisted HTML page and return a bounded, text-free style profile."""
    if not isinstance(allowed_hosts, (list, tuple, set)) or not allowed_hosts:
        raise AppError("website references require an explicit host allowlist")
    current = url
    for redirect in range(Limits.WEBSITE_MAX_REDIRECTS + 1):
        parsed, host = _host(current, allowed_hosts)
        address = _public_address(host)
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            path += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
        status, location, data = _fetch_once(host, address, path)
        if status in _REDIRECTS:
            if redirect == Limits.WEBSITE_MAX_REDIRECTS or not location:
                raise AppError("website reference exceeded the redirect limit")
            current = urljoin(current, location)
            continue
        digest = check_bytes(data)
        metrics = _css_metrics(data)
        summary = (f"HTML page with {metrics['elements']['h1']} primary heading(s); "
                   f"common fonts: {', '.join(metrics['fonts'][:4]) or 'browser default'}.")
        return profile("website", digest, summary, metrics,
                       [{"kind": "web_layout", "elements": metrics["elements"]}])
    raise AppError("website reference exceeded the redirect limit")
