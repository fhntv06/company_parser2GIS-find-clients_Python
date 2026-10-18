def ui_security_problem(cfg) -> str | None:
    """UI без авторизации можно запускать только на localhost."""
    if cfg.ui_host not in ("127.0.0.1", "localhost", "::1") and not (cfg.ui_user and cfg.ui_password):
        return ("UI доступен не только с этого компьютера (UI_HOST=%s), задайте UI_USER и UI_PASSWORD в .env "
                "или оставьте 127.0.0.1." % cfg.ui_host)
    return None
