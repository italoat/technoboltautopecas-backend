import os
import json
import random
from urllib.parse import quote_plus
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TechnoBolt API")

# --- CONFIGURAÇÃO ---
PORT = int(os.environ.get("PORT", 10000))
mongo_user = quote_plus(os.getenv('MONGO_USER', ''))
mongo_pass = quote_plus(os.getenv('MONGO_PASS', ''))
mongo_host = os.getenv('MONGO_HOST', '')
MONGO_URI = f"mongodb+srv://{mongo_user}:{mongo_pass}@{mongo_host}/?retryWrites=true&w=majority"

GEMINI_KEYS = [os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

# --- CONEXÃO MONGO ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.technoboltauto 
    parts_collection = db.estoque   
    users_collection = db.usuarios  
    print("✅ Conectado ao MongoDB: technoboltauto")
except Exception as e:
    print(f"❌ Erro de Conexão: {e}")
    parts_collection = None
    users_collection = None

# --- SEED (POPULAR DADOS) ---
def seed_data():
    if parts_collection is None: return

    # Seed de Peças
    if parts_collection.count_documents({}) == 0 and os.path.exists("dataset_enterprise.json"):
        try:
            with open("dataset_enterprise.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data.get("parts", []) if isinstance(data, dict) else data
                if items: parts_collection.insert_many(items)
        except Exception: pass

    # Seed de Usuário (Atualizado com Lojas)
    if users_collection.count_documents({}) == 0:
        users_collection.insert_one({
            "username": "admin",
            "password": "123",
            "name": "Administrador",
            "role": "admin",
            "allowed_stores": [
                {"id": "loja-01", "name": "Loja 01 - Matriz"},
                {"id": "loja-02", "name": "Loja 02 - Filial Centro"}
            ]
        })
        print("✅ Usuário admin com multilojas criado.")

if client: seed_data()

# --- CORS ---
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
    return {"status": "TechnoBolt Backend Online"}

@app.post("/api/login")
def login(data: LoginRequest):
    if users_collection is None:
        raise HTTPException(status_code=503, detail="Banco desconectado")

    user = users_collection.find_one({"username": data.username, "password": data.password})
    
    if not user:
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    
    # Retorna também as lojas permitidas
    return {
        "name": user.get("name"),
        "role": user.get("role"),
        "allowed_stores": user.get("allowed_stores", [{"id": "loja-01", "name": "Loja Principal"}]),
        "token": "demo-jwt-token"
    }

@app.get("/api/parts")
def get_parts(q: str = None):
    if not parts_collection: return []
    query = {}
    if q:
        query = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"code": {"$regex": q, "$options": "i"}}
        ]}
    parts = list(parts_collection.find(query).limit(50))
    for p in parts: p["id"] = str(p.pop("_id"))
    return parts

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    if not VALID_GEMINI_KEYS: raise HTTPException(status_code=500, detail="Sem chaves IA")
    try:
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        model = genai.GenerativeModel('gemini-1.5-flash')
        content = await file.read()
        prompt = """Analise a peça. Retorne JSON: {"name": "Nome", "possible_vehicles": ["Carros"], "category": "Categoria"}"""
        response = model.generate_content([prompt, {"mime_type": file.content_type, "data": content}])
        return json.loads(response.text.replace("```json", "").replace("```", "").strip())
    except Exception:
        raise HTTPException(status_code=500, detail="Erro IA")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
