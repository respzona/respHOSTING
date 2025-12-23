from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
import docker
import os
import secrets
import string
from datetime import datetime
import subprocess

# ====================================================================
# КОНФИГУРАЦИЯ
# ====================================================================

DATABASE_URL = "sqlite:///./bothost.db"
DOCKER_IMAGE = "python:3.11-slim"
BASE_DOMAIN = "bothost.local"
PORT_START = 5001

app = FastAPI(title="BotHost API")

# CORS для фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================================================================
# БАЗА ДАННЫХ
# ====================================================================

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ====================================================================
# МОДЕЛИ БД
# ====================================================================

class BotModel(Base):
    __tablename__ = "bots"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    token = Column(String)
    user_id = Column(String)
    port = Column(Integer)
    container_id = Column(String)
    webhook_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_running = Column(Integer, default=0)

Base.metadata.create_all(bind=engine)

# ====================================================================
# PYDANTIC МОДЕЛИ
# ====================================================================

class CreateBotRequest(BaseModel):
    name: str
    token: str
    user_id: str

class BotResponse(BaseModel):
    id: int
    name: str
    token: str
    webhook_url: str
    is_running: int
    created_at: datetime

# ====================================================================
# УТИЛИТЫ
# ====================================================================

def generate_random_string(length=8):
    return ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))

def get_next_available_port(db: Session):
    last_bot = db.query(BotModel).order_by(BotModel.port.desc()).first()
    if last_bot:
        return last_bot.port + 1
    return PORT_START

def create_bot_code(token: str, webhook_url: str):
    """Генерирует bot_server.py с твоими параметрами"""
    return f'''#!/usr/bin/env python3
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask, request
import json
import os

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "{token}"
WEBHOOK_URL = "{webhook_url}"

application = None

@app.route('/webhook', methods=['POST'])
async def webhook():
    update_data = request.get_json()
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)
    return {{"ok": True}}

@app.route('/health', methods=['GET'])
def health():
    return {{"status": "БОТ РАБОТАЕТ 24/7 ✅"}}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Я работаю 24/7 на BotHost!")

async def main():
    global application
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    
    async with application:
        await application.bot.set_webhook(WEBHOOK_URL)
        await application.start()
        
if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
    app.run(host='0.0.0.0', port=5000)
'''

# ====================================================================
# API ENDPOINTS
# ====================================================================

@app.post("/api/bots/create")
async def create_bot(
    req: CreateBotRequest,
    db: Session = Depends(get_db)
):
    """Создаёт новый бот и запускает его в Docker"""
    
    # Проверяем, не существует ли уже такой бот
    existing = db.query(BotModel).filter(BotModel.name == req.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Бот с таким именем уже существует")
    
    # Получаем свободный порт
    port = get_next_available_port(db)
    webhook_url = f"https://{req.name}.{BASE_DOMAIN}/webhook"
    
    # Генерируем код бота
    bot_code = create_bot_code(req.token, webhook_url)
    
    # Создаём папку для бота
    bot_dir = f"/tmp/bots/{req.name}"
    os.makedirs(bot_dir, exist_ok=True)
    
    # Пишем bot_server.py
    with open(f"{bot_dir}/bot_server.py", "w") as f:
        f.write(bot_code)
    
    # Пишем requirements.txt
    with open(f"{bot_dir}/requirements.txt", "w") as f:
        f.write("python-telegram-bot==20.7\nflask==3.0.0\nrequests==2.31.0\nsqlalchemy==2.0.0\n")
    
    # Запускаем Docker контейнер
    try:
        client = docker.from_env()
        container = client.containers.run(
            DOCKER_IMAGE,
            f"cd /app && pip install -r requirements.txt && python bot_server.py",
            volumes={bot_dir: {"bind": "/app", "mode": "rw"}},
            ports={'5000/tcp': port},
            detach=True,
            name=f"bot-{req.name}"
        )
        container_id = container.id
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Docker ошибка: {str(e)}")
    
    # Сохраняем в БД
    db_bot = BotModel(
        name=req.name,
        token=req.token,
        user_id=req.user_id,
        port=port,
        container_id=container_id,
        webhook_url=webhook_url,
        is_running=1
    )
    db.add(db_bot)
    db.commit()
    db.refresh(db_bot)
    
    return {
        "id": db_bot.id,
        "name": db_bot.name,
        "webhook_url": db_bot.webhook_url,
        "message": "✅ Бот создан и запущен!"
    }

@app.get("/api/bots")
async def list_bots(user_id: str, db: Session = Depends(get_db)):
    """Список ботов пользователя"""
    bots = db.query(BotModel).filter(BotModel.user_id == user_id).all()
    return bots

@app.get("/api/bots/{bot_id}")
async def get_bot(bot_id: int, db: Session = Depends(get_db)):
    """Информация о боте"""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Бот не найден")
    return bot

@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int, db: Session = Depends(get_db)):
    """Удаляет бота"""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Бот не найден")
    
    # Останавливаем контейнер
    try:
        client = docker.from_env()
        container = client.containers.get(bot.container_id)
        container.stop()
        container.remove()
    except Exception as e:
        logger.error(f"Ошибка удаления контейнера: {str(e)}")
    
    # Удаляем из БД
    db.delete(bot)
    db.commit()
    
    return {"message": "✅ Бот удален"}

@app.post("/api/bots/{bot_id}/restart")
async def restart_bot(bot_id: int, db: Session = Depends(get_db)):
    """Перезагружает бота"""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Бот не найден")
    
    try:
        client = docker.from_env()
        container = client.containers.get(bot.container_id)
        container.restart()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")
    
    return {"message": "✅ Бот перезагружен"}

@app.get("/health")
async def health():
    """Проверка здоровья API"""
    return {
        "status": "🚀 BotHost API работает!",
        "timestamp": datetime.utcnow()
    }

# ====================================================================
# ЗАПУСК
# ====================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
