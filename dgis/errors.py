class RetryLater(Exception):
    """Источник просит подождать (429/403). Очередь ставит паузу."""

    def __init__(self, delay: float, msg: str = ""):
        super().__init__(msg or f"retry in {delay}s")
        self.delay = delay
