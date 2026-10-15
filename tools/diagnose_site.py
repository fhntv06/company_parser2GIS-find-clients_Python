"""Почему сайт не открывается из Python, хотя в браузере открывается.
Запуск (в активированном .venv):  python diagnose_site.py etalon142.ru"""
import socket
import sys
import time

import httpx

domain = sys.argv[1].replace("https://", "").replace("http://", "").strip("/")
BOT = "Mozilla/5.0 (compatible; SiteCheck/1.0)"
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

try:
    ips = sorted({a[4][0] for a in socket.getaddrinfo(domain, None)})
    print("DNS:", ", ".join(ips))
except Exception as e:
    print("DNS: ОШИБКА", type(e).__name__, e)

for port in (443, 80):
    t = time.time()
    try:
        socket.create_connection((domain, port), 5).close()
        print(f"TCP {port}: ok, {time.time() - t:.1f} с")
    except Exception as e:
        print(f"TCP {port}: {type(e).__name__}: {e}")

print()
for scheme in ("https", "http"):
    for ua_name, ua in (("бот", BOT), ("браузер", BROWSER)):
        for timeout in (5, 20):
            t = time.time()
            headers = {"User-Agent": ua, "Accept": "text/html,*/*", "Accept-Language": "ru-RU,ru;q=0.9"}
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=10,
                                  verify=False, headers=headers) as c:
                    r = c.get(f"{scheme}://{domain}/")
                print(f"{scheme:5} UA={ua_name:8} таймаут={timeout:2}с -> {r.status_code}, "
                      f"редиректов {len(r.history)}, {r.url}, {time.time() - t:.1f} с")
            except Exception as e:
                print(f"{scheme:5} UA={ua_name:8} таймаут={timeout:2}с -> ОШИБКА {type(e).__name__}: "
                      f"{str(e)[:110]} ({time.time() - t:.1f} с)")
