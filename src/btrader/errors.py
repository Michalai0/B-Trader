class BTraderError(Exception):
    """Base error safe to show to the Telegram user."""


class ConfigurationError(BTraderError):
    pass


class AuthorizationError(BTraderError):
    pass


class ValidationError(BTraderError):
    pass


class ExchangeError(BTraderError):
    pass


class AmbiguousExchangeError(ExchangeError):
    """The exchange may have accepted a request despite an HTTP timeout/5xx."""


class RateLimitError(ExchangeError):
    pass

