import argparse

import pytest

from scripts.setup_camera import camera_server_url


@pytest.mark.parametrize("url, expected", [
    ("https://agri-sentinel.qunivex.com/", "https://agri-sentinel.qunivex.com"),
    ("http://192.168.1.20:5000", "http://192.168.1.20:5000"),
    ("http://[::1]:5000/", "http://[::1]:5000"),
])
def test_camera_setup_accepts_production_and_lan_origins(url, expected):
    assert camera_server_url(url) == expected


@pytest.mark.parametrize("url", [
    "ftp://example.com", "https://", "https://user:password@example.com",
    "https://example.com/api", "https://example.com?key=secret",
    "https://example.com#fragment", "https://example.com:invalid",
    "https://example.com:0", "https://example.com:65536", "https://bad host",
    "https://[broken-ipv6",
])
def test_camera_setup_rejects_non_origins_and_embedded_credentials(url):
    with pytest.raises(argparse.ArgumentTypeError):
        camera_server_url(url)
