"""Flask-App-Factory für den Doorbell-Dienst."""

from flask import Flask

from .routes import register_routes


def create_app(service) -> Flask:
  app = Flask(__name__)
  register_routes(app, service)
  return app
