import base64
import urllib.parse

import pytest

from test_publish import load_cli


@pytest.fixture
def transport(monkeypatch):
    cli = load_cli()
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PAGE_DROP_TOKEN", raising=False)
    monkeypatch.setenv("EDGE_PUBLISH_TOKEN", "origin-test-token")
    connections = []

    class Reply:
        status = 200

        def read(self):
            return b"{}"

        def getheaders(self):
            return []

    class Connection:
        def __init__(self, host, port, **kwargs):
            self.host, self.port = host, port
            self.tunnel_headers, self.origin_headers = {}, {}
            connections.append(self)

        def set_tunnel(self, host, port, headers=None):
            self.tunnel_target = (host, port)
            self.tunnel_headers = dict(headers or {})

        def putrequest(self, method, target):
            self.method, self.target = method, target

        def putheader(self, key, value):
            self.origin_headers[key] = value

        def endheaders(self):
            pass

        def getresponse(self):
            return Reply()

        def close(self):
            self.closed = True

    monkeypatch.setattr(cli.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(cli.http.client, "HTTPSConnection", Connection)
    return cli, connections


def test_https_connect_keeps_authentication_layers_separate(transport, monkeypatch):
    cli, connections = transport
    monkeypatch.setenv("HTTPS_PROXY", "http://user%2Bname:p%40ss%3Aword+plus@proxy.test:7897")
    cli.request({"base_url": "https://origin.test"}, "GET", "/api/v1/publishes")
    conn = connections[0]
    expected = "Basic " + base64.b64encode(b"user+name:p@ss:word+plus").decode()
    assert conn.tunnel_headers == {"Proxy-Authorization": expected}
    assert conn.tunnel_target == ("origin.test", 443)
    assert conn.origin_headers == {"Authorization": "Bearer origin-test-token"}
    assert conn.target == "/api/v1/publishes"
    assert conn.closed


def test_http_forward_proxy_has_proxy_header(transport, monkeypatch):
    cli, connections = transport
    monkeypatch.setenv("HTTP_PROXY", "http://u:p@proxy.test:8080")
    cli.request({"base_url": "http://127.0.0.1:8000"}, "GET", "/api/v1/publishes")
    conn = connections[0]
    assert conn.tunnel_headers == {}
    assert conn.origin_headers == {"Authorization": "Bearer origin-test-token",
                                   "Proxy-Authorization": "Basic dTpw"}
    assert conn.target == "http://127.0.0.1:8000/api/v1/publishes"


def test_direct_request_and_no_proxy_send_no_proxy_credentials(transport, monkeypatch):
    cli, connections = transport
    monkeypatch.setenv("HTTPS_PROXY", "http://u:p@proxy.test:8080")
    monkeypatch.setenv("NO_PROXY", "origin.test")
    cli.request({"base_url": "https://origin.test"}, "GET", "/x",
                headers={"Proxy-Authorization": "must-not-pass", "Authorization": "must-not-override"})
    conn = connections[0]
    assert conn.host == "origin.test"
    assert conn.tunnel_headers == {}
    assert conn.origin_headers == {"Authorization": "Bearer origin-test-token"}


@pytest.mark.parametrize("scheme", ["https", "socks5", "socks5h", "ftp"])
def test_unsupported_proxy_protocol_is_explicit_without_credentials(transport, monkeypatch, scheme):
    cli, connections = transport
    monkeypatch.setenv("HTTPS_PROXY", f"{scheme}://private-user:private-password@proxy.test:8080")
    with pytest.raises(cli.CliError) as error:
        cli.request({"base_url": "https://origin.test"}, "GET", "/x")
    assert "仅支持" in str(error.value)
    assert "private-user" not in str(error.value)
    assert "private-password" not in str(error.value)
    assert connections == []


def test_https_proxy_environment_can_select_http_proxy(transport, monkeypatch):
    cli, _ = transport
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    proxy = cli.proxy_for(urllib.parse.urlsplit("https://origin.test"))
    assert proxy.scheme == "http"
