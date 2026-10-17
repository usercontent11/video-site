import os
from flask import Flask
from .routes import bp

def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SITE_NAME=os.getenv("SITE_NAME", "Video Atlas"),
        SITE_URL=os.getenv("SITE_URL", "http://localhost:5000").rstrip("/"),
        SOURCE_URL=os.getenv("SOURCE_URL", "https://mobilexh.live").rstrip("/"),
        THUMBNAIL_ORIGIN=os.getenv("THUMBNAIL_ORIGIN", "").rstrip("/"),
        PAGE_SIZE=24,
    )
    app.register_blueprint(bp)
    return app
