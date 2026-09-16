"""Central dashboard server. Run with: python app.py"""
import os

from flask import Flask

from api import api_bp
from db import setup
from routes import ui_bp
from datetime import datetime, timezone, timedelta


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("CSP_MONITOR_SECRET_KEY", "dev-secret-change-me")
    app.register_blueprint(ui_bp)
    app.register_blueprint(api_bp)

    IST = timezone(timedelta(hours=5, minutes=30))

    @app.template_filter("ist")
    def _ist_filter(value):
        if not value:
            return "-"
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(IST)
            return dt.strftime("%d %b %Y, %I:%M:%S %p") + " IST"
        except (ValueError, TypeError):
            return str(value)

    return app


setup()
app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    app.run(host="0.0.0.0", port=port)
