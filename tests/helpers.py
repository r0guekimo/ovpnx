from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "openvpn3"


def fixture(name):
    return (FIXTURES / name).read_text()
