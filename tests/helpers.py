from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "openvpn3"


def fixture(name):
    return (FIXTURES / name).read_text()


NET_FIXTURES = Path(__file__).parent / "fixtures" / "net"


def net_fixture(name):
    return (NET_FIXTURES / name).read_text()


OV2_FIXTURES = Path(__file__).parent / "fixtures" / "openvpn2"


def ov2_fixture(name):
    return (OV2_FIXTURES / name).read_text()
