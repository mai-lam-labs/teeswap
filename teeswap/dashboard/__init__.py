"""Operator dashboard — session-based web UI for monitoring the TEE deployment."""

from .routes import create_dashboard_router

__all__ = ["create_dashboard_router"]
