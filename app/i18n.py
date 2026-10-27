"""Russian-only compatibility helpers; stored user content is never translated."""

LANGUAGES = {"ru": "Русский"}


def get_language():
    return "ru"


def t(source, **values):
    return source.format(**values) if values else source


def init_app(app):
    @app.context_processor
    def language_context():
        return {"t": t, "language": "ru", "languages": LANGUAGES}
