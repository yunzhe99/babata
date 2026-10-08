import asyncio
import socket
import threading
import time

import pytest

from babata import public_web as web


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.2.3.4",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "2002:7f00:1::1",
    ],
)
def test_private_and_special_addresses_are_blocked(address):
    assert not web._public_ip(address)


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_allowed(address):
    assert web._public_ip(address)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "https://u:p@example.com/",
        "https://example.com:8443/",
        "https://example.com:0/",
        "http://example.com:443/",
        "https://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
        "https://[fe80::1%25en0]/",
        "https://example.com/\r\nX: y",
        "https://example.com\\@127.0.0.1/",
        "https://exa mple.com/",
        "https://%31%32%37.0.0.1/",
    ],
)
def test_unsafe_urls_fail_before_dns(url, monkeypatch):
    def unexpected(*args):
        raise AssertionError("Rejected URL reached DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    with pytest.raises(web.PublicWebError):
        web._fetch(url)


def test_url_normalization_preserves_query_and_encodes_unicode():
    endpoint = web._parse_url("https://EXAMPLE.com:443/你好?q=一&n=2#ignored")
    assert endpoint.host == "example.com" and endpoint.port == 443
    assert endpoint.url == "https://example.com/%E4%BD%A0%E5%A5%BD?q=%E4%B8%80&n=2"
    assert endpoint.target.endswith("?q=%E4%B8%80&n=2")


def answer(address):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))


def test_mixed_public_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args: [answer("8.8.8.8"), answer("10.0.0.1")]
    )
    with pytest.raises(web.PublicWebError, match="private DNS"):
        web._resolve(web._parse_url("https://mixed.example/"), time.monotonic() + 5)


def test_redirect_to_private_dns_is_not_requested(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args: [answer("8.8.8.8" if host == "public.example" else "169.254.169.254")],
    )
    requests = []

    def get(endpoint, deadline):
        requests.append(endpoint.url)
        return 302, {"location": "https://metadata.example/secret"}, b""

    monkeypatch.setattr(web, "_get", get)
    with pytest.raises(web.PublicWebError, match="private DNS"):
        web._fetch("https://public.example/")
    assert requests == ["https://public.example/"]


def test_redirect_limit_is_bounded(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [answer("8.8.8.8")])
    calls = []

    def get(endpoint, deadline):
        calls.append(endpoint.url)
        return 302, {"location": "/again"}, b""

    monkeypatch.setattr(web, "_get", get)
    with pytest.raises(web.PublicWebError, match="redirect limit"):
        web._fetch("https://public.example/")
    assert len(calls) == web.MAX_REDIRECTS + 1


def test_pinned_connection_uses_literal_socket_and_hostname_tls(monkeypatch):
    connected, tls_names = [], []

    class Sock:
        def settimeout(self, seconds):
            assert 0 < seconds <= 5

        def connect(self, sockaddr):
            connected.append(sockaddr)

        def close(self):
            pass

    def wrap(sock, server_hostname):
        tls_names.append(server_hostname)
        return sock

    monkeypatch.setattr(socket, "socket", lambda *args: Sock())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("Second DNS lookup"))
    monkeypatch.setattr(web._TLS, "wrap_socket", wrap)
    endpoint = web._parse_url("https://public.example/")
    connection = web._PinnedConnection(endpoint, "8.8.8.8", time.monotonic() + 5)
    connection.connect()
    assert connected == [("8.8.8.8", 443)]
    assert tls_names == ["public.example"]


def test_only_get_with_fixed_headers_and_no_credentials(monkeypatch):
    recorded = []

    class FakeConnection:
        public_socket = None

        def __init__(self, *args):
            pass

        def request(self, method, target, headers):
            recorded.append((method, target, headers))

        def getresponse(self):
            class FakeResponse:
                status = 200

                def getheaders(self):
                    return ["Content-Type", "text/plain"], ["Set-Cookie", "private=ignored"]

                def read1(self, size):
                    return b""

            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(web, "_PinnedConnection", FakeConnection)
    endpoint = web.Endpoint(
        "https://public.example/", "https", "public.example", 443, "/", ("8.8.8.8",)
    )
    status, _, body = web._get(endpoint, time.monotonic() + 5)
    assert status == 200 and body == b""
    method, target, headers = recorded[0]
    assert method == "GET" and target == "/"
    assert {key.lower() for key in headers} == {
        "user-agent",
        "accept",
        "accept-encoding",
        "connection",
    }


@pytest.mark.parametrize("declared_length", [5, 10])
def test_real_connection_close_body_keeps_live_socket(monkeypatch, declared_length):
    client_socket, peer_socket = socket.socketpair()

    def connect(connection):
        connection.sock = connection.public_socket = client_socket

    def serve():
        try:
            peer_socket.recv(4096)
            peer_socket.sendall(
                (
                    "HTTP/1.1 200 OK\r\nConnection: close\r\n"
                    f"Content-Length: {declared_length}\r\n\r\n"
                ).encode()
                + b"hello"
            )
        finally:
            peer_socket.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(web._PinnedConnection, "connect", connect)
    endpoint = web.Endpoint(
        "http://public.example/", "http", "public.example", 80, "/", ("8.8.8.8",)
    )
    try:
        if declared_length == 5:
            assert web._get(endpoint, time.monotonic() + 2)[2] == b"hello"
        else:
            with pytest.raises(web.PublicWebError, match="complete"):
                web._get(endpoint, time.monotonic() + 2)
    finally:
        client_socket.close()
        thread.join(timeout=1)


def test_whole_request_deadline_interrupts_slow_body(monkeypatch):
    client_socket, peer_socket = socket.socketpair()
    finished = threading.Event()

    def connect(connection):
        connection.sock = connection.public_socket = client_socket

    def serve():
        try:
            peer_socket.recv(4096)
            peer_socket.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx")
            finished.wait(2)
        finally:
            peer_socket.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(web._PinnedConnection, "connect", connect)
    endpoint = web.Endpoint(
        "http://public.example/", "http", "public.example", 80, "/", ("8.8.8.8",)
    )
    started = time.monotonic()
    try:
        with pytest.raises(web.PublicWebError):
            web._get(endpoint, started + 0.15)
        assert time.monotonic() - started < 1
    finally:
        finished.set()
        client_socket.close()
        thread.join(timeout=1)


@pytest.mark.parametrize("header", [str(web.MAX_BYTES + 1), "invalid"])
def test_declared_size_limit_blocks_read(monkeypatch, header):
    class FakeConnection:
        public_socket = None

        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            class FakeResponse:
                status = 200

                def getheaders(self):
                    return [("Content-Length", header)]

                def read1(self, size):
                    pytest.fail("Oversize response was read")

            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(web, "_PinnedConnection", FakeConnection)
    endpoint = web.Endpoint(
        "https://public.example/", "https", "public.example", 443, "/", ("8.8.8.8",)
    )
    with pytest.raises(web.PublicWebError, match="size limit"):
        web._get(endpoint, time.monotonic() + 5)


def test_html_is_readable_and_active_content_is_removed(monkeypatch):
    html = (
        b"<html><title>A &amp; B</title><script>evil()</script><style>bad css</style>"
        b"<h1>Hello</h1><p>A &lt; B</p><p>Next</p></html>"
    )
    monkeypatch.setattr(
        web, "_fetch", lambda url: web.Response(url, 200, "text/html;charset=utf-8", html)
    )
    result = web.fetch_public_web("https://public.example/")
    assert result["title"] == "A & B"
    assert result["text"] == "Hello\nA < B\nNext"
    assert result["source_url"] == "https://public.example/"
    assert result["source_is_untrusted"] is True


def test_fetch_truncates_text_and_rejects_binary(monkeypatch):
    monkeypatch.setattr(
        web, "_fetch", lambda url: web.Response(url, 200, "text/plain", b"x" * (web.MAX_TEXT + 1))
    )
    result = web.fetch_public_web("https://public.example/")
    assert len(result["text"]) == web.MAX_TEXT and result["truncated"]
    monkeypatch.setattr(web, "_fetch", lambda url: web.Response(url, 200, "image/png", b"pixels"))
    with pytest.raises(web.PublicWebError, match="not readable text"):
        web.fetch_public_web("https://public.example/")


def test_search_parses_sources_filters_unsafe_and_deduplicates(monkeypatch):
    xml = (
        b"<rss><channel><item><title>Title</title><link>https://public.example/path</link>"
        b"<description>&lt;b&gt;Useful&lt;/b&gt; snippet</description></item>"
        b"<item><link>file:///etc/passwd</link></item><item><link>http://127.0.0.1/</link></item>"
        b"<item><link>https://public.example/path</link></item></channel></rss>"
    )
    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        return web.Response(url, 200, "application/rss+xml", xml)

    monkeypatch.setattr(web, "_fetch", fetch)
    result = web.search_public_web("test & query")
    assert "q=test+%26+query" in seen[0]
    assert "format=rss" in seen[1]
    assert len(result["results"]) == 1
    assert result["results"][0]["summary"] == "Useful snippet"
    assert result["results"][0]["source_url"] == "https://public.example/path"
    assert result["fallback_used"] is True
    assert result["backend"] == "Bing RSS"


@pytest.mark.parametrize("body", [b"<html>captcha</html>", b"<!DOCTYPE rss><rss/>", b"not XML"])
def test_search_backend_failures_are_not_fabricated_results(monkeypatch, body):
    monkeypatch.setattr(
        web, "_fetch", lambda url, **kwargs: web.Response(url, 200, "text/html", body)
    )
    with pytest.raises(web.PublicWebError, match="search backend"):
        web.search_public_web("test")


def test_ddg_search_parses_original_sources_and_matching_snippets(monkeypatch):
    body = (
        b'<a class="result-link" href="//duckduckgo.com/l/?'
        b'uddg=https%3A%2F%2Fpublic.example%2Fstory&amp;rut=tracking">'
        b'Good <b>source</b></a><td class="result-snippet">Relevant <b>summary</b></td>'
        b'<a class="result-link" href="http://127.0.0.1/">Invalid</a>'
        b'<td class="result-snippet">Do not attach to previous result</td>'
    )
    monkeypatch.setattr(
        web, "_fetch", lambda url, **kwargs: web.Response(url, 200, "text/html", body)
    )
    result = web.search_public_web("a query")
    assert result["backend"] == "DuckDuckGo Lite" and not result["fallback_used"]
    assert result["results"] == [
        {
            "title": "Good source",
            "source_url": "https://public.example/story",
            "summary": "Relevant summary",
            "published": "",
        }
    ]


def test_meta_charset_decodes_chinese_page():
    html = '<meta charset="gb2312"><p>话剧</p>'.encode("gb2312")
    decoded = web._decode(web.Response("https://public.example", 200, "text/html", html))
    assert "话剧" in decoded


def test_malformed_search_link_does_not_discard_later_valid_results(monkeypatch):
    body = (
        b'<a class="result-link" href="https://[">Malformed</a>'
        b'<td class="result-snippet">Bad link snippet</td>'
        b'<a class="result-link" href="https://public.example/story">Valid</a>'
        b'<td class="result-snippet">Good snippet</td>'
    )
    monkeypatch.setattr(
        web, "_fetch", lambda url, **kwargs: web.Response(url, 200, "text/html", body)
    )
    result = web.search_public_web("query")
    assert len(result["results"]) == 1 and not result["fallback_used"]
    assert result["results"][0]["source_url"] == "https://public.example/story"
    assert result["results"][0]["summary"] == "Good snippet"


def test_mcp_exposes_only_read_only_tools():
    from babata.public_web_mcp import server

    async def check():
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == {"search_public_web", "fetch_public_web"}
        for tool in tools:
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False

    asyncio.run(check())
