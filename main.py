import os
import json
import random
from urllib.parse import quote_plus
from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TechnoBolt API")

# Configuração MongoDB
PORT = int(os.environ.get("PORT", 10000))
mongo_user = quote_plus(os.getenv('MONGO_USER', ''))
mongo_pass = quote_plus(os.getenv('MONGO_PASS', ''))
mongo_host = os.getenv('MONGO_HOST', '')
MONGO_URI = f"mongodb+srv://{mongo_user}:{mongo_pass}@{mongo_host}/?retryWrites=true&w=majority"

GEMINI_KEYS = [os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

try:
    client = MongoClient(MONGO_URI)
    db = client.technobolt_db
    parts_collection = db.parts
    users_collection = db.users
    print("✅ MongoDB Conectado")
except Exception as e:
    print(f"❌ Erro Mongo: {e}")

# --- SEED DE DADOS E USUÁRIOS ---
def seed_data():
    # 1. Popular Peças
    if parts_collection.count_documents({}) == 0 and os.path.exists("dataset_enterprise.json"):
        with open("dataset_enterprise.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            items = data if isinstance(data, list) else data.get("parts", [])
            parts_collection.insert_many(items)
            print("✅ Peças inseridas.")

    # 2. Criar Usuário Admin Padrão
    if users_collection.count_documents({}) == 0:
        users_collection.insert_one({
            "username": "admin",
            "password": "123", # Em produção, use hash!
            "name": "Gerente Carlos",
            "role": "admin"
        })
        print("✅ Usuário Admin criado (User: admin / Pass: 123)")

if 'parts_collection' in locals():
    seed_data()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELOS ---
class LoginRequest(BaseModel):
    username: str
    password: str

# --- ROTAS ---
@app.get("/")
def read_root():
    return {"status": "Online"}

@app.post("/api/login")
def login(data: LoginRequest):
    user = users_collection.find_one({"username": data.username, "password": data.password})
    if not user:
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    
    return {
        "name": user["name"],
        "role": user["role"],
        "token": "fake-jwt-token-for-mvp" # Simplificado para o MVP
    }

@app.get("/api/parts")
def get_parts(q: str = None):
    query = {}
    if q:
        query = {"$or": [{"name": {"$regex": q, "$options": "i"}}, {"code": {"$regex": q, "$options": "i"}}]}
    
    parts = list(parts_collection.find(query).limit(50))
    for p in parts: p["id"] = str(p.pop("_id"))
    return parts

# (Mantenha a rota /api/ai/identify igual ao código anterior)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
