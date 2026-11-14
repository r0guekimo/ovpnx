"""Console output helpers and secret-safe logging.

Two guarantees live here:
  * everything printed to the terminal is stripped of control characters, so a
    hostile file name cannot inject escape sequences;
  * everything written to the log file passes through redact(), so key
    material and credentials never reach the logs.
"""
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

REDACTED = "[REDACTED]"

_BLOCK_TAGS = (
    "ca", "cert", "extra-certs", "key", "dh", "tls-auth", "tls-crypt",
    "tls-crypt-v2", "pkcs12", "secret", "auth-user-pass",
)
_TAGS = "|".join(re.escape(t) for t in _BLOCK_TAGS)

# <key> ... </key>
_BLOCK_RE = re.compile(r"<(%s)>.*?</\1>" % _TAGS, re.DOTALL | re.IGNORECASE)
# <key> with no closing tag (e.g. a log line cut short): drop the rest
_OPEN_BLOCK_RE = re.compile(r"<(?:%s)>.*\Z" % _TAGS, re.DOTALL | re.IGNORECASE)
# -----BEGIN ...----- ... -----END ...----- (or truncated)
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", re.DOTALL
)
# password=..., token: ...
_KV_RE = re.compile(
    r"\b(password|passwd|token|secret|api[_-]?key|private[_-]?key)(\s*[=:]\s*)(\S+)",
    re.IGNORECASE,
)
# auth-token pushed by the server (appears in PUSH_REPLY lines of OpenVPN logs)
_AUTH_TOKEN_RE = re.compile(r"(auth-token(?:-user)?[ =:]\s*)[^,'\"\s]+", re.IGNORECASE)
# very long base64-looking runs (keys, tokens). 64+ chars avoids normal words.
_B64_RE = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


def redact(text):
    """Return text with anything that looks like a secret replaced."""
    text = str(text)
    text = _BLOCK_RE.sub(REDACTED, text)
    text = _OPEN_BLOCK_RE.sub(REDACTED, text)
    text = _PEM_RE.sub(REDACTED, text)
    text = _KV_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _AUTH_TOKEN_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _B64_RE.sub(REDACTED, text)
    return text


def clean(text):
    """Replace non-printable characters (incl. newlines/ESC) with '?'."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))


def clean_multiline(text):
    """Like clean(), but keeps newlines, carriage returns and tabs."""
    return "".join(
        ch if (ch.isprintable() or ch in "\n\r\t") else "?" for ch in str(text)
    )


class Console:
    """Tiny status printer: [+] ok, [*] info, [!] warning, [-] error."""

    _COLORS = {"+": "32", "*": "34", "!": "33", "-": "31"}

    def __init__(self, out=None, err=None, color=None):
        self.out = sys.stdout if out is None else out
        self.err = sys.stderr if err is None else err
        if color is None:
            isatty = getattr(self.out, "isatty", lambda: False)()
            color = (
                bool(isatty)
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM") != "dumb"
            )
        self.color = color

    def _emit(self, stream, tag, message):
        prefix = "[%s]" % tag
        if self.color:
            prefix = "\033[%sm%s\033[0m" % (self._COLORS[tag], prefix)
        print("%s %s" % (prefix, clean(message)), file=stream)
        self._flush(stream)

    @staticmethod
    def _flush(stream):
        flush = getattr(stream, "flush", None)
        if flush:
            flush()

    def ok(self, message):
        self._emit(self.out, "+", message)

    def info(self, message):
        self._emit(self.out, "*", message)

    def warn(self, message):
        self._emit(self.err, "!", message)

    def err_(self, message):
        self._emit(self.err, "-", message)

    def plain(self, message=""):
        print(clean(message), file=self.out)
        self._flush(self.out)


class RedactingFormatter(logging.Formatter):
    """Formats a record (including tracebacks), then redacts the result."""

    def format(self, record):
        return redact(super().format(record))


def setup_logging(state_dir, verbose=False):
    """Configure the 'ovpnx' logger. Never raises: logging is best effort."""
    logger = logging.getLogger("ovpnx")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
    try:
        log_dir = Path(state_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "ovpnx.log"
        handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=256 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        try:
            os.chmod(str(log_file), 0o600)
        except OSError:
            pass
    except OSError:
        logger.addHandler(logging.NullHandler())

    if verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(RedactingFormatter("[debug] %(message)s"))
        logger.addHandler(console)
    return logger
