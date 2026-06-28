import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN environment variable is not set. "
        "Set it in your .env file for local testing, or in Railway's "
        "environment variables for deployment."
    )