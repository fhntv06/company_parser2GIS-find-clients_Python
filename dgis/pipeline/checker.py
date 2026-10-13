"""Проверка доступности сайта.

Принципы: любой HTTP-ответ означает, что сайт жив; причина отказа всегда сохраняется (а не просто «0»);
временные сбои (таймаут, сброс соединения, DNS) повторяются раньше, чем сайт будет признан недоступным.
"""
from __future__ import annotations

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}
TRANSIENT = {"timeout", "reset", "connect", "dns"}      # имеет смысл повторить позже


def classify_error(e: Exception) -> str:
    """Короткая причина отказа: timeout | dns | ssl | reset | connect | redirects | <имя исключения>."""
    name, msg = type(e).__name__.lower(), str(e).lower()
    if "timeout" in name:
        return "timeout"
    if "toomanyredirects" in name:
        return "redirects"
    if any(x in msg for x in ("getaddrinfo", "name or service not known", "nodename nor servname",
                              "no address associated", "temporary failure in name resolution")):
        return "dns"
    if any(x in msg for x in ("ssl", "tls", "certificate", "handshake")):
        return "ssl"
    if name in ("readerror", "remoteprotocolerror", "writeerror", "localprotocolerror") \
            or any(x in msg for x in ("reset", "disconnected", "eof")):
        return "reset"
    if "connect" in name or "refused" in msg:
        return "connect"
    return name[:20] or "error"


def is_transient(error: str | None) -> bool:
    return bool(error) and any(p in TRANSIENT for p in error.split("/"))


async def check_url(client, url: str) -> tuple[int, str | None, str | None]:
    """GET с редиректами: сначала https, затем http. Возвращает (status, final_url, error).
    Любой HTTP-ответ (даже 403) возвращается как есть. status=0 значит, что ответа не было, причина в error."""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])
    errors: list[str] = []
    for u in candidates:
        try:
            r = await client.get(u)
            return r.status_code, str(r.url), None
        except Exception as e:
            errors.append(classify_error(e))
    return 0, None, errors[0] if len(set(errors)) == 1 else "/".join(errors)


def _ssl_context():
    """Для проверки доступности сертификат не важен; допускаем и старые серверы (слабые шифры, legacy TLS)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


def make_client(timeout: float, max_connections: int = 40, proxy: str | None = None):
    import httpx
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=10, verify=_ssl_context(),
                             proxy=proxy, headers=BROWSER_HEADERS,
                             limits=httpx.Limits(max_connections=max_connections))
