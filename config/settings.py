import os
from dotenv import load_dotenv

# Cargar desde .env
load_dotenv("/opt/beatnow-back/.env")  # <-- AJUSTA LA RUTA EXACTA

class Settings:
    SSH_USERNAME = os.getenv("SSH_USERNAME_RES")
    SSH_PASSWORD = os.getenv("SSH_PASSWORD_RES")
    SSH_HOST     = os.getenv("SSH_HOST_RES")

    SECRET_KEY = os.getenv("SECRET_KEY")
    ALGORITHM = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 8000))

    MONGO_USER = os.getenv("MONGO_USER")
    MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
    MONGO_HOST = os.getenv("MONGO_HOST")
    MONGO_DB = os.getenv("MONGO_DB", "BeatNow")

settings = Settings()
