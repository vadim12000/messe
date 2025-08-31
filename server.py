import json
from datetime import datetime, timezone
import os
import shutil
import asyncio # <-- НОВЫЙ ИМПОРТ
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Form, UploadFile, File
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Table, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session, selectinload, subqueryload
from sqlalchemy.sql import func
from typing import List, Dict

from passlib.context import CryptContext

# --- Настройка ---
DATABASE_URL = "sqlite:///./messenger.db" 
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Создаем папки для загрузки файлов ---
UPLOAD_DIR = "uploads"
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
MESSAGES_DIR = os.path.join(UPLOAD_DIR, "messages")
os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(MESSAGES_DIR, exist_ok=True)


# --- Модели SQLAlchemy ---
chat_user_association = Table('chat_user_association', Base.metadata,
    Column('user_id', Integer, ForeignKey('users.id', ondelete="CASCADE"), primary_key=True),
    Column('chat_id', Integer, ForeignKey('chats.id', ondelete="CASCADE"), primary_key=True))

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    echo_id = Column(String, unique=True, index=True, nullable=True) 
    hashed_password = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    chats = relationship("Chat", secondary=chat_user_association, back_populates="users")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String) 
    users = relationship("User", secondary=chat_user_association, back_populates="chats")
    messages = relationship("Message", back_populates="chat", cascade="all, delete-orphan", order_by="Message.timestamp.desc()")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    text = Column(String, nullable=True) # Текст теперь может быть пустым
    image_url = Column(String, nullable=True) # <-- НОВОЕ ПОЛЕ
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False) 
    sender_username = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    chat_id = Column(Integer, ForeignKey("chats.id"), nullable=False)
    chat = relationship("Chat", back_populates="messages")
    is_read = Column(Integer, default=0)
    reply_to_id = Column(Integer, ForeignKey("messages.id"), nullable=True)

# --- Настройка Базы Данных ---
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI()

# --- Раздаем статичные файлы ---
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

class ConnectionManager:
    # ... (код ConnectionManager без изменений)
manager = ConnectionManager()

# --- API эндпоинты ---
# ... (register, login, search, update_echo_id, upload_avatar, create_chat, get_user_chats - без изменений)

# --- ИЗМЕНЕНИЕ: get_chat_messages теперь возвращает и image_url ---
@app.get("/chats/{chat_id}/messages/")
def get_chat_messages(chat_id: int, db: Session = Depends(get_db)):
    messages = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.timestamp).limit(100).all()
    return [{
        "id": msg.id, "text": msg.text, "sender_username": msg.sender_username,
        "chat_id": msg.chat_id, "timestamp": msg.timestamp.isoformat(),
        "image_url": msg.image_url, # <-- Добавлено
        "reply_to_text": None, "reply_to_sender": None
    } for msg in messages]

@app.post("/chats/{chat_id}/clear_history/")
def clear_chat_history(chat_id: int, db: Session = Depends(get_db)):
    # ... (код без изменений)

# --- НОВЫЙ ЭНДПОИНТ ---
@app.post("/chats/{chat_id}/send_image/")
async def send_image_message(chat_id: int, user_id: int = Form(...), sender_username: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    file_extension = os.path.splitext(file.filename)[1]
    filename = f"{user_id}_{chat_id}_{int(datetime.now().timestamp())}{file_extension}"
    file_path = os.path.join(MESSAGES_DIR, filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    image_url = f"/uploads/messages/{filename}"

    new_message = Message(
        text="[Фотография]", image_url=image_url,
        sender_id=user_id, sender_username=sender_username,
        chat_id=chat_id
    )
    db.add(new_message); db.commit(); db.refresh(new_message)
    
    message_to_broadcast = {
        "action": "new_message",
        "message": {
            "id": new_message.id, "text": new_message.text, "sender_username": new_message.sender_username,
            "chat_id": new_message.chat_id, "timestamp": new_message.timestamp.isoformat(),
            "image_url": new_message.image_url,
            "reply_to_text": None, "reply_to_sender": None,
        }
    }
    
    # Отправляем уведомление по WebSocket
    await manager.broadcast_to_room(chat_id, json.dumps(message_to_broadcast))
    
    return message_to_broadcast

# --- ИЗМЕНЕНИЕ: websocket_endpoint теперь отдает и image_url ---
@app.websocket("/ws/{chat_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: int, user_id: int):
    await manager.connect(websocket, chat_id)
    db = SessionLocal()
    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            payload = data.get("payload", {})
            sender = db.query(User).get(user_id)
            if not sender: continue
            
            # Сообщение с текстом (без картинки)
            new_message = Message(text=payload.get("text"), sender_id=user_id,
                                  sender_username=sender.username, chat_id=chat_id)
            db.add(new_message); db.commit(); db.refresh(new_message)

            message_to_broadcast = {
                "action": "new_message",
                "message": {
                    "id": new_message.id, "text": new_message.text, "sender_username": new_message.sender_username,
                    "chat_id": new_message.chat_id, "timestamp": new_message.timestamp.isoformat(),
                    "image_url": new_message.image_url,
                    "reply_to_text": None, "reply_to_sender": None,
                }
            }
            await manager.broadcast_to_room(chat_id, json.dumps(message_to_broadcast))
    except WebSocketDisconnect:
        manager.disconnect(websocket, chat_id)
    except Exception as e:
        print(f"Ошибка WebSocket: {e}"); manager.disconnect(websocket, chat_id)
    finally:
        db.close()
