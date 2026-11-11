"""qwen-relay egress: the only way out of a runtime build. CONNECT to port 443 of a public
address only; never loopback, the LAN, the WSL host, or a local service such as emet's
MCP server on 127.0.0.1."""
import importlib.machinery
import importlib.util
import pathlib
import socket
import subprocess
import sys
import time

import pytest

RELAY = pathlib.Path(__file__).resolve().parent.parent / "bin" / "qwen-relay"


def _load():
    loader = importlib.machinery.SourceFileLoader("qwen_relay", str(RELAY))
    spec = importlib.util.spec_from_loader("qwen_relay", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


relay = _load()


@pytest.mark.parametrize("ip", ["8.8.8.8", "151.101.0.223", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert relay.public_addr(ip)


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.53", "10.255.255.254", "192.168.1.5", "172.17.0.1",
                                "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1",
                                "fe80::1", "fd00::1", "::ffff:127.0.0.1", "::", "::127.0.0.1", "64:ff9b::7f00:1",
                                "2002:7f00:1::1", "64:ff9b::a00:1"])
def test_local_and_private_addresses_refused(ip):
    assert not relay.public_addr(ip)


def test_parse_connect():
    assert relay.parse_connect(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n") == ("pypi.org", 443)
    assert relay.parse_connect(b"CONNECT [2606:4700::1]:443 HTTP/1.1\r\n\r\n") == ("2606:4700::1", 443)


@pytest.mark.parametrize("head", [b"GET http://pypi.org/ HTTP/1.1\r\n\r\n", b"CONNECT pypi.org HTTP/1.1\r\n\r\n",
                                  b"CONNECT pypi.org:443\r\n\r\n", b"CONNECT bad/host:443 HTTP/1.1\r\n\r\n",
                                  b"CONNECT :443 HTTP/1.1\r\n\r\n"])
def test_parse_connect_refuses_other_requests(head):
    with pytest.raises(ValueError):
        relay.parse_connect(head)


def test_pick_public_drops_local_answers():
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    assert relay.pick_public(infos) == [(socket.AF_INET, ("8.8.8.8", 443))]


@pytest.fixture
def egress(private_dir):
    sock = private_dir / "egress.sock"
    p = subprocess.Popen([sys.executable, "-I", str(RELAY), "egress", "--sock", str(sock)],
                         stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)
    yield sock, p
    p.kill()
    p.wait()


def _ask(sock, head):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(str(sock))
    s.sendall(head)
    reply = s.recv(4096)
    s.close()
    return reply


@pytest.mark.parametrize("target", [b"127.0.0.1:443", b"localhost:443", b"10.255.255.254:443", b"[::1]:443"])
def test_egress_refuses_local_targets(egress, target):
    sock, _ = egress
    assert _ask(sock, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("host", ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"])
def test_registries_allowed(host):
    assert relay.allowed_host(host)


@pytest.mark.parametrize("host", ["github.com", "evil.example", "pypi.org.evil.example", "8.8.8.8", "xpypi.org"])
def test_other_hosts_refused(host):
    assert not relay.allowed_host(host)


def test_egress_refuses_hosts_off_the_list(egress):
    sock, p = egress
    assert _ask(sock, b"CONNECT github.com:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    p.kill()
    assert "refused github.com:443 (not a package registry)" in p.stderr.read()


def test_egress_refuses_other_ports_and_plain_http(egress):
    sock, _ = egress
    assert _ask(sock, b"CONNECT example.com:80 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _ask(sock, b"GET http://example.com/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_egress_logs_each_refusal(egress):
    sock, p = egress
    _ask(sock, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
    p.kill()
    assert "refused 127.0.0.1:443" in p.stderr.read()
