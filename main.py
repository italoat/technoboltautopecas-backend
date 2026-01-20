import os
import json
import random
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
from dotenv import load_dotenv

# Carrega variáveis de ambiente (para teste local)
load_dotenv()

app = FastAPI(title="TechnoBolt API")

# --- CONFIGURAÇÃO DE AMBIENTE ---
PORT = int(os.environ.get("PORT", 10000))
MONGO_URI = f"mongodb+srv://{os.getenv('MONGO_USER')}:{os.getenv('MONGO_PASS')}@{os.getenv('MONGO_HOST')}/?retryWrites=true&w=majority"

# Lista de Chaves para Rodízio (Round Robin)
GEMINI_KEYS = [
    os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)
]
# Remove chaves vazias caso alguma env não esteja setada
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

# --- BANCO DE DADOS ---
try:
    client = MongoClient(MONGO_URI)
    db = client.technobolt_db
    parts_collection = db.parts
    print("✅ Conectado ao MongoDB Atlas")
except Exception as e:
    print(f"❌ Erro ao conectar no MongoDB: {e}")

# --- POPULAR BANCO (DATASET SEED) ---
def seed_database():
    """Lê o dataset_enterprise.json e salva no Mongo se a coleção estiver vazia"""
    if parts_collection.count_documents({}) == 0:
        if os.path.exists("dataset_enterprise.json"):
            try:
                with open("dataset_enterprise.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Assume que o JSON é uma lista ou tem uma chave 'parts'
                    items = data if isinstance(data, list) else data.get("parts", [])
                    if items:
                        parts_collection.insert_many(items)
                        print(f"✅ Banco populado com {len(items)} itens do dataset.")
            except Exception as e:
                print(f"⚠️ Erro ao ler dataset: {e}")
        else:
            print("⚠️ Arquivo dataset_enterprise.json não encontrado.")

# Executa o seed ao iniciar
seed_database()

# --- CORS (Permitir Frontend) ---
origins = [
    "http://localhost:5173",
    "https://technobolt-frontend.vercel.app", # Ajuste conforme sua URL final
    os.getenv("FRONTEND_URL")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Em produção, restrinja para a lista 'origins'
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELOS ---
class Part(BaseModel):
    name: str
    code: str
    price: float
    description: Optional[str] = None

# --- ROTAS ---

@app.get("/")
def read_root():
    return {"status": "TechnoBolt Backend Online", "keys_loaded": len(VALID_GEMINI_KEYS)}

@app.get("/api/parts")
def get_parts(q: Optional[str] = None):
    """Busca peças por nome ou código"""
    query = {}
    if q:
        query = {
            "$or": [
                {"name": {"$regex": q, "$options": "i"}},
                {"code": {"$regex": q, "$options": "i"}},
                {"brand": {"$regex": q, "$options": "i"}}
            ]
        }
    
    parts = list(parts_collection.find(query).limit(50))
    
    # Serializar ObjectId para string
    for part in parts:
        part["id"] = str(part.pop("_id"))
        
    return parts

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    """Vision AI: Identifica peça pela foto usando Gemini"""
    if not VALID_GEMINI_KEYS:
        raise HTTPException(status_code=500, detail="Nenhuma chave Gemini configurada.")
    
    try:
        # Seleciona chave aleatória
        api_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        content = await file.read()
        
        prompt = """
        Atue como um especialista sênior em autopeças. Analise esta imagem.
        Retorne APENAS um JSON (sem markdown) com:
        {
            "name": "Nome técnico da peça",
            "possible_vehicles": ["Lista de carros compatíveis"],
            "category": "Categoria (Ex: Freio, Suspensão)",
            "confidence": "Nível de certeza (Alto/Médio/Baixo)"
        }
        """
        
        response = model.generate_content([
            prompt,
            {"mime_type": file.content_type, "data": content}
        ])
        
        # Limpeza básica para garantir JSON válido
        text_resp = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text_resp)
        
    except Exception as e:
        print(f"Erro AI: {e}")
        raise HTTPException(status_code=500, detail="Falha na análise de IA")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
