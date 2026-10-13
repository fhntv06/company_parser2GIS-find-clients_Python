from __future__ import annotations

import logging
from collections import deque
from pathlib import Path


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, ()
        return True


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 200):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def tail(self, n: int = 50) -> list[str]:
        return list(self.lines)[-n:]


def setup_logging(secrets: list[str], logfile: str = "logs/parser.log") -> LogBuffer:
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    buf = LogBuffer()
    buf.setFormatter(fmt)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log = logging.getLogger("dgis")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for h in (buf, fh, sh):
        h.addFilter(RedactFilter(secrets))
        log.addHandler(h)
    # httpx пишет полные URL (с ключом) на уровне INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return buf
