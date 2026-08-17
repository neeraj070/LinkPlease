import os
from dotenv import load_dotenv

load_dotenv()

PSEUDOGRAM_API_KEY = os.getenv("PSEUDOGRAM_API_KEY", "").strip().strip("'\"")
PSEUDOGRAM_BASE_URL = os.getenv("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com").strip().strip("'\"")
DB_PATH = os.getenv("DB_PATH", "linkplease.db").strip().strip("'\"")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

