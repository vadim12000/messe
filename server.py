import asyncio
import json
from datetime import datetime
# ИСПРАВЛЕНИЕ ТУТ:
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Form
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Table, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session # <-- ДОБАВЛЕНО declarative_base
from sqlalchemy.sql import func
from typing import Dict, List

# --- Настройка базы данных ---
DATABASE_URL = "sqlite:///./messenger.db"
Base = declarative_base() # Теперь эта строка будет работать

# ... остальной код сервера остается без изменений ...

# Таблица связи "пользователь-чат" (многие ко многим)
chat_user_association = Table(
    'chat_user_association', Base.metadata,
    Column('user_id', Integer, ForeignKey('users.id', ondelete="CASCADE"), primary_key=True),
    Column('chat_id', Integer, ForeignKey('chats.id', ondelete="CASCADE"), primary_key=True)
)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    chats = relationship("Chat", secondary=chat_user_association, back_populates="users")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    users = relationship("User", secondary=chat_user_association, back_populates="chats")
    messages = relationship("Message", back_populates="chat", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    text = Column(String)
    sender_username = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    chat_id = Column(Integer, ForeignKey("chats.id"))
    chat = relationship("Chat", back_populates="messages")

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

# --- Менеджер WebSocket-комнат ---
class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, chat_id: int):
        await websocket.accept()
        if chat_id not in self.rooms:
            self.rooms[chat_id] = []
        self.rooms[chat_id].append(websocket)

    def disconnect(self, websocket: WebSocket, chat_id: int):
        if chat_id in self.rooms:
            self.rooms[chat_id].remove(websocket)

    async def broadcast_to_room(self, chat_id: int, message: str):
        if chat_id in self.rooms:
            for connection in self.rooms[chat_id]:
                await connection.send_text(message)

manager = ConnectionManager()

# --- API эндпоинты ---

@app.post("/register/")
async def register_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Имя пользователя уже занято")
    new_user = User(username=username, hashed_password=password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username}

@app.post("/login/")
async def login_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or user.hashed_password != password:
        raise HTTPException(status_code=401, detail="Неверное имя или пароль")
    return {"id": user.id, "username": user.username}

@app.get("/users/search/")
async def search_users(query: str, db: Session = Depends(get_db)):
    users = db.query(User).filter(User.username.contains(query)).limit(10).all()
    return [{"id": user.id, "username": user.username} for user in users]

@app.post("/chats/create/")
async def create_chat(user1_id: int = Form(...), user2_id: int = Form(...), db: Session = Depends(get_db)):
    user1 = db.query(User).get(user1_id)
    user2 = db.query(User).get(user2_id)
    if not user1 or not user2:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    for chat in user1.chats:
        if user2 in chat.users:
            return {"id": chat.id, "name": chat.name, "message": "Чат уже существует"}

    chat_name = f"{user1.username} & {user2.username}"
    new_chat = Chat(name=chat_name, users=[user1, user2])
    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)
    return {"id": new_chat.id, "name": new_chat.name}

@app.get("/chats/{user_id}/")
async def get_user_chats(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).options(relationship(User.chats)).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return [{"id": chat.id, "name": chat.name} for chat in user.chats]

@app.get("/chats/{chat_id}/messages/")
async def get_chat_messages(chat_id: int, db: Session = Depends(get_db)):
    messages = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.timestamp.desc()).limit(50).all()
    return [{"text": msg.text, "sender_username": msg.sender_username, "chat_id": msg.chat_id} for msg in reversed(messages)]

# --- WebSocket эндпоинт ---
@app.websocket("/ws/{chat_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: int, user_id: int):
    await manager.connect(websocket, chat_id)
    db = SessionLocal()
    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            
            new_message = Message(text=data["text"], sender_username=data["sender_username"], chat_id=chat_id)
            db.add(new_message)
            db.commit()
            
            await manager.broadcast_to_room(chat_id, json.dumps({
                "text": new_message.text,
                "sender_username": new_message.sender_username,
                "chat_id": new_message.chat_id
            }))
    except WebSocketDisconnect:
        manager.disconnect(websocket, chat_id)
    except Exception as e:
        print(f"Ошибка WebSocket: {e}")
        manager.disconnect(websocket, chat_id)
    finally:
        db.close()
