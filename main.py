import os
import json
import random
from typing import List, Optional
from urllib.parse import quote_plus # <--- Importação necessária para corrigir o erro da senha
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

app = FastAPI(title="TechnoBolt API")

# --- CONFIGURAÇÃO DE AMBIENTE ---
PORT = int(os.environ.get("PORT", 10000))

# --- CORREÇÃO DO ERRO DE SENHA (RFC 3986) ---
# Codifica usuário e senha para permitir caracteres especiais como '@'
mongo_user = quote_plus(os.getenv('MONGO_USER', ''))
mongo_pass = quote_plus(os.getenv('MONGO_PASS', ''))
mongo_host = os.getenv('MONGO_HOST', '')

MONGO_URI = f"mongodb+srv://{mongo_user}:{mongo_pass}@{mongo_host}/?retryWrites=true&w=majority"

# Lista de Chaves para Rodízio (Round Robin)
GEMINI_KEYS = [
    os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)
]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

# --- BANCO DE DADOS ---
try:
    client = MongoClient(MONGO_URI)
    db = client.technobolt_db
    parts_collection = db.parts
    # Teste rápido de conexão
    client.admin.command('ping')
    print("✅ Conectado ao MongoDB Atlas com sucesso!")
except Exception as e:
    print(f"❌ Erro Crítico ao conectar no MongoDB: {e}")

# --- POPULAR BANCO (DATASET SEED) ---
def seed_database():
    """Lê o dataset_enterprise.json e salva no Mongo se a coleção estiver vazia"""
    try:
        if parts_collection.count_documents({}) == 0:
            if os.path.exists("dataset_enterprise.json"):
                with open("dataset_enterprise.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    items = data if isinstance(data, list) else data.get("parts", [])
                    if items:
                        parts_collection.insert_many(items)
                        print(f"✅ Banco populado com {len(items)} itens.")
            else:
                print("⚠️ Arquivo dataset_enterprise.json não encontrado (Seed ignorado).")
    except Exception as e:
        # Evita quebrar o deploy se o banco cair
        print(f"⚠️ Aviso no Seed: {e}")

# Executa o seed ao iniciar (apenas se conexão tiver sucesso)
if 'parts_collection' in locals():
    seed_database()

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ROTAS ---
@app.get("/")
def read_root():
    return {
        "status": "TechnoBolt Backend Online", 
        "keys_loaded": len(VALID_GEMINI_KEYS),
        "python_version": "OK"
    }

@app.get("/api/parts")
def get_parts(q: Optional[str] = None):
    query = {}
    if q:
        query = {
            "$or": [
                {"name": {"$regex": q, "$options": "i"}},
                {"code": {"$regex": q, "$options": "i"}},
                {"brand": {"$regex": q, "$options": "i"}}
            ]
        }
    
    try:
        parts = list(parts_collection.find(query).limit(50))
        for part in parts:
            part["id"] = str(part.pop("_id"))
        return parts
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    if not VALID_GEMINI_KEYS:
        raise HTTPException(status_code=500, detail="Nenhuma chave Gemini configurada.")
    
    try:
        api_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        content = await file.read()
        
        prompt = """
        Analise esta imagem de autopeça. Retorne APENAS JSON:
        {
            "name": "Nome técnico",
            "possible_vehicles": ["Veículos compatíveis"],
            "category": "Categoria",
            "confidence": "Alto/Médio/Baixo"
        }
        """
        
        response = model.generate_content([
            prompt,
            {"mime_type": file.content_type, "data": content}
        ])
        
        text_resp = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text_resp)
        
    except Exception as e:
        print(f"Erro AI: {e}")
        raise HTTPException(status_code=500, detail="Falha na análise de IA")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
