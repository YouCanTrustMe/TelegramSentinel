"""Dummy required-settings values so importing src.config (and everything that
imports it) works without a real .env — e.g. in CI. Set before any test module
imports a src package. Nothing in the suite connects to a network."""
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummyhash")
os.environ.setdefault("TELEGRAM_PHONE", "+10000000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:dummy")
os.environ.setdefault("TELEGRAM_SUPERGROUP_ID", "-1000000000000")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")
os.environ.setdefault("GROQ_API_KEY", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "")
