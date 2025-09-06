import json
from datetime import datetime, timezone
import os
import shutil
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Form, UploadFile, File
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Table, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session, selectinload, subqueryload
from sqlalchemy.sql import func
from typing import List, Dict

from passlib.context import CryptContext
import firebase_admin
from firebase_admin import credentials, messaging

# --- Настройка ---
DATABASE_URL = "sqlite:///./messenger.db" 
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Настройка Firebase Admin SDK ---
try:
    cred = credentials.Certificate("firebase-credentials.json")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    print(">>> Firebase Admin SDK инициализирован успешно.")
except Exception as e:
    print(f"!!! ОШИБКА ИНИЦИАЛИЗАЦИИ FIREBASE: {e}")

# --- Настройка папок для загрузки файлов ---
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
    firebase_token = Column(String, nullable=True)
    # --- ПРАВИЛЬНАЯ СВЯЗЬ ---
    chats = relationship("Chat", secondary=chat_user_association, back_populates="users")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String) 
    messages = relationship("Message", back_populates="chat", cascade="all, delete-orphan", order_by="Message.timestamp.desc()")
    # --- ПРАВИЛЬНАЯ СВЯЗЬ ---
    users = relationship("User", secondary=chat_user_association, back_populates="chats")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    text = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
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
    try: yield db
    finally: db.close()

app = FastAPI()
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[int, List[WebSocket]] = {}
    async def connect(self, websocket: WebSocket, chat_id: int):
        await websocket.accept()
        if chat_id not in self.rooms: self.rooms[chat_id] = []
        self.rooms[chat_id].append(websocket)
    def disconnect(self, websocket: WebSocket, chat_id: int):
        if chat_id in self.rooms and websocket in self.rooms[chat_id]:
            self.rooms[chat_id].remove(websocket)
    async def broadcast_to_room(self, chat_id: int, message: str):
        if chat_id in self.rooms:
            for connection in list(self.rooms[chat_id]):
                await connection.send_text(message)
manager = ConnectionManager()

# --- Вспомогательная функция для отправки Push ---
async def send_push_notification(chat_id: int, sender: User, message_text: str, db: Session):
    chat = db.query(Chat).options(selectinload(Chat.users)).get(chat_id)
    if not chat: return
    for recipient in chat.users:
        if recipient.id != sender.id and recipient.firebase_token:
            print(f">>> Попытка отправить PUSH пользователю {recipient.username}...")
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=sender.username, body=message_text),
                    token=recipient.firebase_token,
                    android=messaging.AndroidConfig(priority='high'),
                    data={'chat_id': str(chat_id)} 
                )
                response = messaging.send(message)
                print(f'>>> Уведомление для {recipient.username} успешно отправлено:', response)
            except Exception as e:
                print(f'!!! Ошибка при отправке PUSH пользователю {recipient.id}:', e)

# --- API эндпоинты ---
@app.post("/register/")
def register_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Имя пользователя уже занято")
    hashed_password = pwd_context.hash(password)
    new_user = User(username=username, hashed_password=hashed_password)
    db.add(new_user); db.commit(); db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username, "echo_id": new_user.echo_id, "avatar_url": new_user.avatar_url}

@app.post("/login/")
def login_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверное имя или пароль")
    user.last_seen = datetime.now(timezone.utc)
    db.commit()
    return {"id": user.id, "username": user.username, "echo_id": user.echo_id, "avatar_url": user.avatar_url}

@app.post("/users/update_fcm_token/")
def update_fcm_token(user_id: int = Form(...), token: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).get(user_id)
    if user:
        user.firebase_token = token
        db.commit()
        print(f">>> Обновлен FCM токен для пользователя {user.username}")
        return {"message": "Token updated successfully"}
    raise HTTPException(status_code=404, detail="User not found")

@app.get("/users/search/")
def search_users(query: str = "", db: Session = Depends(get_db)):
    if query:
        search_term = query.replace("@echo:", "").strip().lower()
        users = db.query(User).filter(
            (User.username.ilike(f"%{search_term}%")) | (User.echo_id.ilike(f"%{search_term}%"))
        ).limit(20).all()
    else:
        users = db.query(User).all()
    return [{"id": user.id, "username": user.username, "echo_id": user.echo_id, "avatar_url": user.avatar_url} for user in users]

@app.post("/users/update_echo_id/")
def update_echo_id(user_id: int = Form(...), echo_id: str = Form(...), db: Session = Depends(get_db)):
    if len(echo_id) < 4 or not echo_id.isalnum():
        raise HTTPException(status_code=400, detail="Echo ID должен быть не менее 4 символов и содержать только буквы и цифры.")
    if db.query(User).filter(User.echo_id == echo_id, User.id != user_id).first():
        raise HTTPException(status_code=400, detail="Этот Echo ID уже занят.")
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    user.echo_id = echo_id
    db.commit()
    return {"message": "Echo ID успешно обновлен."}

@app.post("/users/upload_avatar/")
def upload_avatar(user_id: int = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    file_extension = os.path.splitext(file.filename)[1]
    filename = f"{user_id}_{int(datetime.now().timestamp())}{file_extension}"
    file_path = os.path.join(AVATAR_DIR, filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    avatar_url = f"/uploads/avatars/{filename}"
    user.avatar_url = avatar_url
    db.commit()
    return {"avatar_url": avatar_url}

@app.post("/chats/create/")
def create_chat(user1_id: int = Form(...), user2_id: int = Form(...), db: Session = Depends(get_db)):
    user1 = db.query(User).get(user1_id)
    user2 = db.query(User).get(user2_id)
    if not user1 or not user2:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    for chat in user1.chats:
        if user2 in chat.users:
            return {"id": chat.id, "name": "Уже существует", "message": "Чат уже существует"}
    technical_name = f"Chat between {user1.id} and {user2.id}"
    new_chat = Chat(name=technical_name, users=[user1, user2])
    db.add(new_chat); db.commit(); db.refresh(new_chat)
    return {"id": new_chat.id, "name": user2.username}

@app.get("/chats/{user_id}/")
def get_user_chats(user_id: int, db: Session = Depends(get_db)):
    try:
        user = db.query(User).options(
            selectinload(User.chats).subqueryload(Chat.users),
            selectinload(User.chats).subqueryload(Chat.messages)
        ).get(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        chat_previews = []
        for chat in user.chats:
            partner = next((u for u in chat.users if u.id != user_id), None)
            if not partner: continue
            last_message = chat.messages[0] if chat.messages else None
            preview = { "chat_id": chat.id, "partner": { "id": partner.id, "username": partner.username, "avatar_url": partner.avatar_url, "last_seen_iso": partner.last_seen.isoformat() if partner.last_seen else None, }, "last_message": { "text": last_message.text if last_message else "Нет сообщений", "timestamp_iso": last_message.timestamp.isoformat() if last_message else datetime.now(timezone.utc).isoformat(), "sender_id": last_message.sender_id if last_message else None, } }
            chat_previews.append(preview)
        chat_previews.sort(key=lambda x: x["last_message"]["timestamp_iso"], reverse=True)
        return chat_previews
    except Exception as e:
        print(f"!!! Ошибка в get_user_chats для user_id={user_id}: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера при загрузке чатов")

@app.get("/chats/{chat_id}/messages/")
def get_chat_messages(chat_id: int, db: Session = Depends(get_db)):
    messages = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.timestamp).limit(100).all()
    return [{"id": msg.id, "text": msg.text, "sender_username": msg.sender_username, "chat_id": msg.chat_id, "timestamp": msg.timestamp.isoformat(), "image_url": msg.image_url, "reply_to_text": None, "reply_to_sender": None} for msg in messages]
             
@app.post("/chats/{chat_id}/clear_history/")
def clear_chat_history(chat_id: int, db: Session = Depends(get_db)):
    chat = db.query(Chat).get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    db.query(Message).filter(Message.chat_id == chat_id).delete()
    db.commit()
    return {"message": "История чата успешно очищена"}

@app.post("/chats/{chat_id}/send_image/")
async def send_image_message(chat_id: int, user_id: int = Form(...), sender_username: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    file_extension = os.path.splitext(file.filename)[1]
    filename = f"{user_id}_{chat_id}_{int(datetime.now().timestamp())}{file_extension}"
    file_path = os.path.join(MESSAGES_DIR, filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    image_url = f"/uploads/messages/{filename}"
    new_message = Message(text="[Фотография]", image_url=image_url, sender_id=user_id, sender_username=sender_username, chat_id=chat_id)
    db.add(new_message); db.commit(); db.refresh(new_message)
    
    message_to_broadcast = {"action": "new_message", "message": {"id": new_message.id, "text": new_message.text, "sender_username": new_message.sender_username, "chat_id": new_message.chat_id, "timestamp": new_message.timestamp.isoformat(), "image_url": new_message.image_url, "reply_to_text": None, "reply_to_sender": None}}
    await manager.broadcast_to_room(chat_id, json.dumps(message_to_broadcast))
    sender = db.query(User).get(user_id)
    if sender:
        await send_push_notification(chat_id, sender, new_message.text, db)
    return message_to_broadcast

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
            
            new_message = Message(text=payload.get("text"), sender_id=user_id, sender_username=sender.username, chat_id=chat_id)
            db.add(new_message); db.commit(); db.refresh(new_message)

            message_to_broadcast = {"action": "new_message", "message": {"id": new_message.id, "text": new_message.text, "sender_username": new_message.sender_username, "chat_id": new_message.chat_id, "timestamp": new_message.timestamp.isoformat(), "image_url": new_message.image_url, "reply_to_text": None, "reply_to_sender": None}}
            await manager.broadcast_to_room(chat_id, json.dumps(message_to_broadcast))
            
            await send_push_notification(chat_id, sender, new_message.text, db)
    except WebSocketDisconnect:
        manager.disconnect(websocket, chat_id)
    except Exception as e:
        print(f"Ошибка WebSocket: {e}"); manager.disconnect(websocket, chat_id)
    finally:
        db.close()

