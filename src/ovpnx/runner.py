"""Run a command while optionally echoing its output live.

Used for 'openvpn3 session-start', which may print login prompts that the
user must see and answer. stdin is inherited so typing works; output is both
collected (for error analysis) and, when asked, shown immediately.
"""
import codecs
import os
import select
import subprocess
import sys
import tempfile
import time

from .log import clean_multiline

TIMED_OUT = 124


def run_streaming(cmd, echo=False, timeout=None, out=None):
    """Return (returncode, output) or None if the command cannot be started.

    On timeout the process is killed and returncode is 124.
    """
    out = sys.stdout if out is None else out
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        return None

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunks = []
    fd = proc.stdout.fileno()
    deadline = None if timeout is None else time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            wait = 0.5
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                wait = min(wait, remaining)
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                continue
            data = os.read(fd, 4096)
            if not data:
                break
            text = decoder.decode(data)
            chunks.append(text)
            if echo and text:
                out.write(clean_multiline(text))
                out.flush()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass

    if timed_out:
        proc.kill()
        proc.wait()
        return TIMED_OUT, "".join(chunks)
    return proc.wait(), "".join(chunks)


def run_detached(cmd, timeout=30):
    """Run a command that may fork into the background (e.g. openvpn --daemon).

    Output goes to a temporary file rather than a pipe, so a daemonized child
    can never keep us waiting. Returns (returncode, output); returncode is 124
    on timeout, and None is returned if the command cannot be started.
    """
    try:
        with tempfile.TemporaryFile() as sink:
            try:
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=sink,
                                      stderr=subprocess.STDOUT, timeout=timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = TIMED_OUT
            sink.seek(0)
            out = sink.read().decode("utf-8", "replace")
    except OSError:
        return None
    return rc, out
