import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Table
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session
from typing import List, Dict

# --- Настройка базы данных ---
DATABASE_URL = "sqlite:///./messenger.db"
Base = declarative_base()

# Таблица связи "пользователь-чат" (многие ко многим)
chat_user_association = Table(
    'chat_user_association', Base.metadata,
    Column('user_id', Integer, ForeignKey('users.id')),
    Column('chat_id', Integer, ForeignKey('chats.id'))
)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String) # Для простоты храним пароль открытым, в реальном проекте - хеш!
    chats = relationship("Chat", secondary=chat_user_association, back_populates="users")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String) # Имя чата (например, "User1 & User2")
    users = relationship("User", secondary=chat_user_association, back_populates="chats")
    messages = relationship("Message", back_populates="chat")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    text = Column(String)
    sender_username = Column(String)
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
        # Структура: {chat_id: {user_id: websocket}}
        self.rooms: Dict[int, Dict[int, WebSocket]] = {}

    async def connect(self, websocket: WebSocket, chat_id: int, user_id: int):
        await websocket.accept()
        if chat_id not in self.rooms:
            self.rooms[chat_id] = {}
        self.rooms[chat_id][user_id] = websocket

    def disconnect(self, chat_id: int, user_id: int):
        if chat_id in self.rooms and user_id in self.rooms[chat_id]:
            del self.rooms[chat_id][user_id]

    async def broadcast_to_room(self, chat_id: int, message: str):
        if chat_id in self.rooms:
            for connection in self.rooms[chat_id].values():
                await connection.send_text(message)

manager = ConnectionManager()

# --- API эндпоинты ---

@app.post("/register/")
async def register_user(username: str, password: str, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Имя пользователя уже занято")
    new_user = User(username=username, hashed_password=password) # В реальном приложении хешировать пароль!
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username}

@app.post("/login/")
async def login_user(username: str, password: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or user.hashed_password != password: # Простая проверка пароля
        raise HTTPException(status_code=401, detail="Неверное имя пользователя или пароль")
    return {"id": user.id, "username": user.username, "message": "Вход выполнен успешно"}

@app.get("/users/search/")
async def search_users(query: str, db: Session = Depends(get_db)):
    users = db.query(User).filter(User.username.contains(query)).limit(10).all()
    return [{"id": user.id, "username": user.username} for user in users]

@app.post("/chats/create/")
async def create_chat(user1_id: int, user2_id: int, db: Session = Depends(get_db)):
    user1 = db.query(User).get(user1_id)
    user2 = db.query(User).get(user2_id)
    if not user1 or not user2:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Проверка, существует ли уже чат между этими пользователями
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
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return [{"id": chat.id, "name": chat.name} for chat in user.chats]

# --- WebSocket эндпоинт ---
@app.websocket("/ws/{chat_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: int, user_id: int, db: Session = Depends(get_db)):
    await manager.connect(websocket, chat_id, user_id)
    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str) # Ожидаем: {"text": "сообщение", "sender_username": "имя"}

            # Сохранение сообщения в БД
            new_message = Message(
                text=data["text"],
                sender_username=data["sender_username"],
                chat_id=chat_id
            )
            db.add(new_message)
            db.commit()

            # Рассылка сообщения всем в комнате
            await manager.broadcast_to_room(chat_id, json.dumps({
                "text": new_message.text,
                "sender_username": new_message.sender_username,
                "chat_id": new_message.chat_id
            }))

    except WebSocketDisconnect:
        manager.disconnect(chat_id, user_id)
    except Exception as e:
        print(f"Ошибка WebSocket: {e}")
        manager.disconnect(chat_id, user_id)