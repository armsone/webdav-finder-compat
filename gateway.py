#!/usr/bin/env python3
"""
WebDAV Compatibility Reverse Proxy for macOS WebDAVFS Clients.

Solves the macOS WebDAVFS Unicode normalization issue (NFC vs NFD) when
accessing WebDAV servers such as Synology NAS, without modifying on-disk
names or breaking incoming request paths.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import ssl
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Mapping, Optional, Set, Tuple

import yarl
from aiohttp import (
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
    TCPConnector,
    web,
)
from multidict import CIMultiDict

DAV_NAMESPACE = "DAV:"
DAV_MULTISTATUS = f"{{{DAV_NAMESPACE}}}multistatus"
DAV_RESPONSE = f"{{{DAV_NAMESPACE}}}response"
DAV_HREF = f"{{{DAV_NAMESPACE}}}href"

# Statically register standard WebDAV namespace prefix
ET.register_namespace("D", DAV_NAMESPACE)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# RFC 3986 path segment safe characters:
# unreserved (ALPHA, DIGIT, '-', '.', '_', '~') and sub-delims ('!', '$', '&', "'", '(', ')', '*', '+', ',', ';', '=')
# plus ':' and '@'.
# Explicitly excluded: '/', '?', '#', and '%'
SAFE_SEGMENT_CHARS = "-._~!$&'()*+,;=:@~"

logger = logging.getLogger("webdav_gateway")


def validate_origin(url_str: str, param_name: str) -> Tuple[str, str]:
    """
    Validates that a URL is a valid HTTP(S) origin:
    - Scheme must be 'http' or 'https'
    - Must have netloc (host:port)
    - Userinfo (credentials) forbidden
    - Query forbidden
    - Fragment forbidden
    - Path must be empty or '/' (origin only)
    Returns (scheme, netloc).
    """
    if not url_str:
        raise ValueError(f"{param_name} cannot be empty")
    parsed = urllib.parse.urlsplit(url_str.strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(
            f"Invalid {param_name} scheme '{parsed.scheme}'. Must be 'http' or 'https'."
        )
    if not parsed.netloc:
        raise ValueError(f"Invalid {param_name}: missing host/netloc.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError(f"Invalid {param_name}: user credentials/userinfo are not permitted.")
    if parsed.query:
        raise ValueError(f"Invalid {param_name}: query string is not permitted in origin.")
    if parsed.fragment:
        raise ValueError(f"Invalid {param_name}: fragment is not permitted in origin.")
    clean_path = parsed.path.rstrip("/")
    if clean_path:
        raise ValueError(
            f"Invalid {param_name}: path prefix '{parsed.path}' is not supported. Upstream and public origins must be origin-only (path empty or '/')."
        )
    return parsed.scheme.lower(), parsed.netloc


def is_native_webdavfs_client(user_agent: Optional[str]) -> bool:
    """
    Detects if the incoming request originates from macOS native WebDAVFS.
    """
    if not user_agent:
        return False
    return "webdavfs/" in user_agent.lower()


def normalize_path_segment_nfd(segment: str) -> str:
    """
    Decodes percent-encoded UTF-8 in a single URI path segment,
    normalizes Unicode to NFD, and re-encodes, preserving plus,
    percent, hash, and slash semantics.
    """
    if not segment:
        return segment

    try:
        raw_bytes = urllib.parse.unquote_to_bytes(segment)
    except Exception:
        return segment

    try:
        decoded_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Non-UTF8 binary payload in segment: leave unchanged
        return segment

    # Normalize Unicode to Normalization Form D (NFD)
    nfd_text = unicodedata.normalize("NFD", decoded_text)

    # Re-encode segment:
    # safe chars include '+' (preserving literal plus),
    # while '/', '?', '#', and '%' are encoded to preserve structure.
    return urllib.parse.quote(nfd_text, safe=SAFE_SEGMENT_CHARS)


def normalize_href_nfd(href: str) -> str:
    """
    Normalizes a WebDAV href to NFD segment-wise.
    Preserves absolute URI scheme, authority (netloc), query, and fragment.
    Preserves leading and trailing slashes.
    """
    if not href:
        return href

    parsed = urllib.parse.urlsplit(href)
    if not parsed.path:
        return href

    segments = parsed.path.split("/")
    normalized_segments = [normalize_path_segment_nfd(seg) for seg in segments]
    new_path = "/".join(normalized_segments)

    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            new_path,
            parsed.query,
            parsed.fragment,
        )
    )


def rewrite_if_header(
    if_value: str,
    allowed_authorities: Set[str],
    upstream_scheme: str,
    upstream_netloc: str,
) -> str:
    """
    Rewrites tagged HTTP/HTTPS resource URLs in WebDAV 'If' header from public/local
    origin to upstream backend origin at parenthesis depth 0.
    Preserves all angle tokens inside condition parentheses verbatim (state/lock tokens),
    bracketed URLs with parentheses, and spacing.
    """
    if not if_value:
        return if_value

    result: list[str] = []
    depth = 0
    i = 0
    n = len(if_value)

    while i < n:
        char = if_value[i]
        if char == "<":
            end_pos = if_value.find(">", i + 1)
            if end_pos != -1:
                token = if_value[i : end_pos + 1]
                if depth == 0:
                    url_inside = if_value[i + 1 : end_pos]
                    parsed = urllib.parse.urlsplit(url_inside)
                    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                        if parsed.netloc.lower() in allowed_authorities:
                            new_url = urllib.parse.urlunsplit((
                                upstream_scheme,
                                upstream_netloc,
                                parsed.path,
                                parsed.query,
                                parsed.fragment,
                            ))
                            token = f"<{new_url}>"
                result.append(token)
                i = end_pos + 1
                continue
            else:
                result.append(char)
                i += 1
        elif char == "(":
            depth += 1
            result.append(char)
            i += 1
        elif char == ")":
            if depth > 0:
                depth -= 1
            result.append(char)
            i += 1
        else:
            result.append(char)
            i += 1

    return "".join(result)


def remap_response_location(
    url_str: str,
    upstream_netloc: str,
    public_scheme: str,
    public_netloc: str,
) -> str:
    """
    Remaps upstream-origin Location and Content-Location headers to the public origin
    to prevent exposing internal backend hostnames (e.g. host.docker.internal).
    Preserves path, query, and fragment.
    """
    if not url_str:
        return url_str
    parsed = urllib.parse.urlsplit(url_str)
    if parsed.netloc and parsed.netloc.lower() == upstream_netloc.lower():
        return urllib.parse.urlunsplit((
            public_scheme,
            public_netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        ))
    return url_str


def filter_request_headers(
    headers: Mapping[str, str],
    upstream_netloc: str,
    upstream_scheme: str,
    public_netloc: str,
    listen_host: str,
    listen_port: int,
    force_identity_encoding: bool = False,
) -> CIMultiDict[str]:
    """
    Filters hop-by-hop headers from incoming request and adjusts Host, Destination,
    and If headers. Preserves multi-valued headers, authentication, locks, Range.
    """
    connection_val = headers.get("Connection", "")
    custom_hop = {c.strip().lower() for c in connection_val.split(",") if c.strip()}

    allowed_authorities = {
        public_netloc.lower(),
        upstream_netloc.lower(),
        f"{listen_host}:{listen_port}".lower(),
        f"127.0.0.1:{listen_port}",
        f"localhost:{listen_port}",
        listen_host.lower(),
        "127.0.0.1",
        "localhost",
    }

    forward_headers: CIMultiDict[str] = CIMultiDict()
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in HOP_BY_HOP_HEADERS or k_lower in custom_hop:
            continue
        if k_lower == "host":
            continue
        if force_identity_encoding and k_lower == "accept-encoding":
            continue
        if k_lower == "destination":
            parsed_dest = urllib.parse.urlsplit(v)
            if parsed_dest.netloc:
                if parsed_dest.netloc.lower() in allowed_authorities:
                    new_dest = urllib.parse.urlunsplit((
                        upstream_scheme,
                        upstream_netloc,
                        parsed_dest.path,
                        parsed_dest.query,
                        parsed_dest.fragment,
                    ))
                    forward_headers.add(k, new_dest)
                else:
                    raise web.HTTPBadRequest(
                        reason=f"Destination authority '{parsed_dest.netloc}' not permitted"
                    )
            else:
                new_dest = urllib.parse.urlunsplit((
                    upstream_scheme,
                    upstream_netloc,
                    parsed_dest.path,
                    parsed_dest.query,
                    parsed_dest.fragment,
                ))
                forward_headers.add(k, new_dest)
            continue
        if k_lower == "if":
            rewritten_if = rewrite_if_header(
                v, allowed_authorities, upstream_scheme, upstream_netloc
            )
            forward_headers.add(k, rewritten_if)
            continue
        forward_headers.add(k, v)

    # Set Host header to upstream endpoint host
    forward_headers["Host"] = upstream_netloc

    if force_identity_encoding:
        forward_headers["Accept-Encoding"] = "identity"

    return forward_headers


def filter_response_headers(
    headers: Mapping[str, str],
    upstream_netloc: str,
    public_scheme: str,
    public_netloc: str,
) -> CIMultiDict[str]:
    """
    Filters hop-by-hop headers from upstream response.
    Remaps upstream-origin Location and Content-Location headers to public origin.
    Preserves multi-valued headers such as WWW-Authenticate and Set-Cookie.
    """
    connection_val = headers.get("Connection", "")
    custom_hop = {c.strip().lower() for c in connection_val.split(",") if c.strip()}

    resp_headers: CIMultiDict[str] = CIMultiDict()
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in HOP_BY_HOP_HEADERS or k_lower in custom_hop:
            continue
        if k_lower in ("location", "content-location"):
            remapped_url = remap_response_location(
                v, upstream_netloc, public_scheme, public_netloc
            )
            resp_headers.add(k, remapped_url)
            continue
        resp_headers.add(k, v)

    return resp_headers


class WebDAVGateway:
    def __init__(
        self,
        upstream_url: str,
        public_origin: str = "https://nas.example.com:5006",
        listen_host: str = "127.0.0.1",
        listen_port: int = 8080,
        tls_verify: bool = True,
        max_xml_size: int = 10 * 1024 * 1024,
    ):
        self.upstream_scheme, self.upstream_netloc = validate_origin(
            upstream_url, "Upstream URL"
        )
        self.upstream_url = f"{self.upstream_scheme}://{self.upstream_netloc}"

        self.public_scheme, self.public_netloc = validate_origin(
            public_origin, "Public origin"
        )
        self.public_origin = f"{self.public_scheme}://{self.public_netloc}"

        self.listen_host = listen_host
        self.listen_port = listen_port
        self.tls_verify = tls_verify
        self.max_xml_size = max_xml_size

        self.session: Optional[ClientSession] = None

    async def init_session(self) -> None:
        """Initializes aiohttp ClientSession with proper TLS and isolation configuration."""
        if self.upstream_scheme == "https":
            ssl_context = ssl.create_default_context()
            if self.tls_verify:
                ssl_context.check_hostname = True
                ssl_context.verify_mode = ssl.CERT_REQUIRED
            else:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            connector = TCPConnector(ssl=ssl_context)
        else:
            connector = TCPConnector()

        # No total timeout to allow arbitrary streaming file transfers
        # DummyCookieJar prevents cross-user cookie leakage
        # trust_env=False avoids environment proxy leak
        # auto_decompress=False avoids silently decompressing/corrupting wire bytes
        # skip_auto_headers prevents aiohttp from injecting unintended headers
        timeout = ClientTimeout(total=None, connect=60.0, sock_read=None)
        self.session = ClientSession(
            connector=connector,
            timeout=timeout,
            cookie_jar=DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            skip_auto_headers={"Accept-Encoding", "Accept", "User-Agent", "Content-Type"},
        )

    async def close_session(self) -> None:
        """Closes the client session."""
        if self.session and not self.session.closed:
            await self.session.close()

    def rewrite_propfind_xml(self, body_bytes: bytes) -> Tuple[bool, bytes, Optional[str]]:
        """
        Parses 207 Multi-Status XML and normalizes direct DAV:multistatus/DAV:response/DAV:href
        elements to NFD.
        If backend emits absolute hrefs matching upstream origin, remaps authority to public origin.

        Returns (modified, result_bytes, error_message).
        If an error occurred (malformed XML, DOCTYPE/ENTITY, collision), returns error_message.
        """
        if b"\x00" in body_bytes:
            return False, body_bytes, "NUL bytes in XML response not permitted"

        try:
            if body_bytes.startswith(b"\xef\xbb\xbf"):
                decoded_xml = body_bytes.decode("utf-8-sig")
            else:
                decoded_xml = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return False, body_bytes, "Non-UTF-8 XML response not permitted"

        upper_text = decoded_xml.upper()
        if (
            "<!DOCTYPE" in upper_text
            or "<!ENTITY" in upper_text
            or re.search(r"<!\s*DOCTYPE", upper_text)
            or re.search(r"<!\s*ENTITY", upper_text)
        ):
            return False, body_bytes, "XML DOCTYPE and ENTITY declarations are not permitted"

        xml_decl_match = re.match(
            r"<\?xml\s+[^>]*encoding=['\"]([^'\"]+)['\"]", decoded_xml, re.IGNORECASE
        )
        if xml_decl_match:
            declared_encoding = xml_decl_match.group(1).strip().lower()
            if declared_encoding not in ("utf-8", "utf8"):
                return False, body_bytes, f"Unsupported XML encoding declaration '{declared_encoding}'; UTF-8 required"

        try:
            root = ET.fromstring(decoded_xml)
        except ET.ParseError:
            return False, body_bytes, "Malformed XML received from upstream"

        if root.tag != DAV_MULTISTATUS:
            return False, body_bytes, None

        modified = False
        seen_normalized_hrefs: Set[str] = set()
        seen_normalized_paths: Set[str] = set()
        seen_canonical_paths: Set[str] = set()

        for resp in root:
            if resp.tag != DAV_RESPONSE:
                continue

            for child in resp:
                if child.tag != DAV_HREF:
                    continue

                raw_href = (child.text or "").strip()
                if not raw_href:
                    continue

                # Document property absolutehrefauthorities:
                # If backend emits absolute hrefs with backend authority, remap to public origin
                parsed_href = urllib.parse.urlsplit(raw_href)
                target_authority_href = raw_href
                if parsed_href.netloc and parsed_href.netloc.lower() == self.upstream_netloc.lower():
                    target_authority_href = urllib.parse.urlunsplit((
                        self.public_scheme,
                        self.public_netloc,
                        parsed_href.path,
                        parsed_href.query,
                        parsed_href.fragment,
                    ))

                norm_href = normalize_href_nfd(target_authority_href)
                parsed_norm = urllib.parse.urlsplit(norm_href)
                norm_path = parsed_norm.path
                norm_canonical = norm_path.rstrip("/") or "/"

                # Detect conflicting aliases/colliding normalized hrefs and fail explicitly
                if (
                    norm_href in seen_normalized_hrefs
                    or norm_path in seen_normalized_paths
                    or norm_canonical in seen_canonical_paths
                ):
                    return False, body_bytes, "Conflicting href alias detected after normalization"

                seen_normalized_hrefs.add(norm_href)
                seen_normalized_paths.add(norm_path)
                seen_canonical_paths.add(norm_canonical)

                if norm_href != raw_href:
                    child.text = norm_href
                    modified = True

        if not modified:
            return False, body_bytes, None

        new_body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        return True, new_body, None

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """Main request handler forwarding to upstream and selectively rewriting 207 PROPFIND."""
        assert self.session is not None, "ClientSession is not initialized"

        start_time = asyncio.get_event_loop().time()
        method = request.method

        # Reject duplicate Authorization and Destination headers per RFC security requirements
        if len(request.headers.getall("Authorization", [])) > 1:
            return web.Response(
                status=400,
                text="Bad Request: Duplicate Authorization headers not permitted",
            )
        if len(request.headers.getall("Destination", [])) > 1:
            return web.Response(
                status=400,
                text="Bad Request: Ambiguous duplicate Destination headers not permitted",
            )

        raw_path = request.rel_url.raw_path
        if not raw_path.startswith("/"):
            return web.Response(
                status=400,
                text="Bad Request: Absolute path starting with '/' required",
            )
        if any(ord(c) < 32 or ord(c) == 127 for c in raw_path):
            return web.Response(
                status=400,
                text="Bad Request: Control characters not permitted in path",
            )

        target_url_str = f"{self.upstream_scheme}://{self.upstream_netloc}{raw_path}"
        if request.rel_url.raw_query_string:
            target_url_str = f"{target_url_str}?{request.rel_url.raw_query_string}"

        target_url = yarl.URL(target_url_str, encoded=True)

        user_agent = request.headers.get("User-Agent")
        is_native = is_native_webdavfs_client(user_agent)
        is_propfind = (method.upper() == "PROPFIND")

        forward_headers = filter_request_headers(
            headers=request.headers,
            upstream_netloc=self.upstream_netloc,
            upstream_scheme=self.upstream_scheme,
            public_netloc=self.public_netloc,
            listen_host=self.listen_host,
            listen_port=self.listen_port,
            force_identity_encoding=(is_native and is_propfind),
        )

        req_data = request.content if request.can_read_body else None

        try:
            upstream_resp = await self.session.request(
                method=method,
                url=target_url,
                headers=forward_headers,
                data=req_data,
                allow_redirects=False,
                skip_auto_headers={"Accept-Encoding", "Accept", "User-Agent", "Content-Type"},
            )
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.error("Upstream connection error: %s", exc.__class__.__name__)
            return web.Response(status=502, text="Bad Gateway: Unable to reach upstream WebDAV server")

        is_propfind_207 = (is_propfind and upstream_resp.status == 207)

        # For native WebDAVFS clients only, inspect and correct 207 PROPFIND responses
        if is_native and is_propfind_207:
            resp_encoding = upstream_resp.headers.get("Content-Encoding", "").strip().lower()
            if resp_encoding and resp_encoding != "identity":
                upstream_resp.close()
                logger.error("Upstream returned unsupported Content-Encoding for PROPFIND: %s", resp_encoding)
                return web.Response(
                    status=502,
                    text=f"Bad Gateway: Unsupported upstream Content-Encoding: {resp_encoding}",
                )

            declared_len = upstream_resp.headers.get("Content-Length")
            if declared_len:
                try:
                    if int(declared_len) > self.max_xml_size:
                        upstream_resp.close()
                        logger.error("Upstream XML response exceeded configured max buffer size")
                        return web.Response(status=502, text="Bad Gateway: XML response exceeded buffer limit")
                except ValueError:
                    pass

            chunks = []
            total_read = 0
            async for chunk in upstream_resp.content.iter_chunked(65536):
                total_read += len(chunk)
                if total_read > self.max_xml_size:
                    upstream_resp.close()
                    logger.error("Upstream XML stream exceeded configured max buffer size")
                    return web.Response(status=502, text="Bad Gateway: XML response exceeded buffer limit")
                chunks.append(chunk)

            body_bytes = b"".join(chunks)
            upstream_resp.close()

            modified, result_bytes, error_msg = self.rewrite_propfind_xml(body_bytes)
            if error_msg:
                logger.error("XML processing failure: %s", error_msg)
                return web.Response(status=502, text=f"Bad Gateway: {error_msg}")

            resp_headers = filter_response_headers(
                headers=upstream_resp.headers,
                upstream_netloc=self.upstream_netloc,
                public_scheme=self.public_scheme,
                public_netloc=self.public_netloc,
            )
            if modified:
                resp_headers.popall("Content-Length", None)
                resp_headers.popall("Content-Encoding", None)
                resp_headers.popall("Transfer-Encoding", None)
                resp_headers.popall("ETag", None)
                resp_headers.popall("Digest", None)
                resp_headers.popall("Content-MD5", None)

                resp_headers["Content-Length"] = str(len(result_bytes))
                resp_headers["Content-Type"] = "text/xml; charset=utf-8"
                final_body = result_bytes
            else:
                final_body = body_bytes

            elapsed = (asyncio.get_event_loop().time() - start_time) * 1000
            # Log only method, status, peer, and elapsed time - NO filenames or auth headers
            logger.info("Method: %s | Status: %d | Client: %s | Elapsed: %.1fms", method, 207, request.remote, elapsed)
            return web.Response(status=207, body=final_body, headers=resp_headers)

        # Unmodified pass-through: stream binary payloads and large transfers directly
        resp_headers = filter_response_headers(
            headers=upstream_resp.headers,
            upstream_netloc=self.upstream_netloc,
            public_scheme=self.public_scheme,
            public_netloc=self.public_netloc,
        )
        response = web.StreamResponse(
            status=upstream_resp.status,
            reason=upstream_resp.reason,
            headers=resp_headers,
        )

        try:
            await response.prepare(request)
            async for chunk in upstream_resp.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
            upstream_resp.release()
        finally:
            upstream_resp.close()

        elapsed = (asyncio.get_event_loop().time() - start_time) * 1000
        logger.info(
            "Method: %s | Status: %d | Client: %s | Elapsed: %.1fms",
            method,
            upstream_resp.status,
            request.remote,
            elapsed,
        )
        return response


def create_app(gateway: WebDAVGateway) -> web.Application:
    """Builds the aiohttp Application with unlimited upload body size and configured handler arguments."""
    app = web.Application(
        client_max_size=0,
        handler_args={
            "auto_decompress": False,
            "max_line_size": 65536,
            "max_field_size": 65536,
        },
    )

    webdav_methods = [
        "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS",
        "PROPFIND", "PROPPATCH", "MKCOL", "COPY", "MOVE", "LOCK", "UNLOCK",
        "REPORT", "CHECKIN", "CHECKOUT", "ORDERPATCH", "SEARCH",
    ]

    for meth in webdav_methods:
        app.router.add_route(meth, "/{path:.*}", gateway.handle_request)

    async def on_startup(app: web.Application) -> None:
        await gateway.init_session()

    async def on_cleanup(app: web.Application) -> None:
        await gateway.close_session()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Production-suitable WebDAV compatibility reverse proxy for macOS WebDAVFS."
    )
    parser.add_argument(
        "--upstream",
        dest="upstream",
        default=os.environ.get("WEBDAV_UPSTREAM_URL", ""),
        help="Target upstream WebDAV URL (e.g. http://host.docker.internal:5005)",
    )
    parser.add_argument(
        "--public-origin",
        dest="public_origin",
        default=os.environ.get("WEBDAV_PUBLIC_ORIGIN", "https://nas.example.com:5006"),
        help="Public origin of WebDAV service (default: https://nas.example.com:5006)",
    )
    parser.add_argument(
        "--listen-host",
        dest="listen_host",
        default=os.environ.get("WEBDAV_LISTEN_HOST", "127.0.0.1"),
        help="Host interface to listen on (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--listen-port",
        dest="listen_port",
        type=int,
        default=int(os.environ.get("WEBDAV_LISTEN_PORT", "8080")),
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--tls-verify",
        dest="tls_verify",
        action="store_true",
        default=os.environ.get("WEBDAV_TLS_VERIFY", "true").lower() in ("true", "1", "yes"),
        help="Enable upstream TLS certificate and hostname verification (default: ON)",
    )
    parser.add_argument(
        "--no-tls-verify",
        dest="tls_verify",
        action="store_false",
        help="Disable upstream TLS verification",
    )
    parser.add_argument(
        "--max-xml-size",
        dest="max_xml_size",
        type=int,
        default=int(os.environ.get("WEBDAV_MAX_XML_SIZE", str(10 * 1024 * 1024))),
        help="Maximum XML response size in bytes to buffer for PROPFIND (default: 10485760)",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_arguments()
    if not args.upstream:
        logger.error("Upstream URL is required (--upstream or WEBDAV_UPSTREAM_URL)")
        sys.exit(1)

    try:
        gateway = WebDAVGateway(
            upstream_url=args.upstream,
            public_origin=args.public_origin,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            tls_verify=args.tls_verify,
            max_xml_size=args.max_xml_size,
        )
    except Exception as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    app = create_app(gateway)
    logger.info("Starting WebDAV Compatibility Gateway on %s:%d", args.listen_host, args.listen_port)
    logger.info("Configured public origin: %s", gateway.public_origin)
    logger.info("Upstream target endpoint configured with TLS verification: %s", args.tls_verify)

    # Disable default access_log to prevent logging filenames/URIs
    web.run_app(app, host=args.listen_host, port=args.listen_port, access_log=None)


if __name__ == "__main__":
    main()
