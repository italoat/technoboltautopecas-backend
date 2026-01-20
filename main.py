import os
import json
import random
from typing import List, Optional
from urllib.parse import quote_plus
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

app = FastAPI(title="TechnoBolt Enterprise API")

# --- CONFIGURAÇÕES DE AMBIENTE ---
PORT = int(os.environ.get("PORT", 10000))

# Tratamento seguro da String de Conexão MongoDB
mongo_user = quote_plus(os.getenv('MONGO_USER', ''))
mongo_pass = quote_plus(os.getenv('MONGO_PASS', ''))
mongo_host = os.getenv('MONGO_HOST', '')

if not mongo_host:
    MONGO_URI = "mongodb://localhost:27017" # Fallback local caso ENV falhe
else:
    MONGO_URI = f"mongodb+srv://{mongo_user}:{mongo_pass}@{mongo_host}/?retryWrites=true&w=majority"

# Configuração das 7 Chaves Gemini para Rodízio
GEMINI_KEYS = [os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

# --- INICIALIZAÇÃO DO BANCO DE DADOS ---
db_status = "Desconectado"
try:
    # Timeout de 5s para não travar o deploy se o Mongo estiver fora
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.technoboltauto        # Seu banco de dados
    parts_collection = db.estoque     # Sua coleção de peças
    users_collection = db.usuarios    # Sua coleção de usuários
    
    # Teste de conexão/permissão
    client.admin.command('ping')
    db_status = "Conectado e Operacional"
    print("✅ MongoDB Atlas: Conexão estabelecida com sucesso!")
except OperationFailure:
    db_status = "Erro de Permissão (Verifique o Atlas)"
    print("❌ Erro: Usuário sem permissão no banco 'technoboltauto'")
except Exception as e:
    db_status = f"Erro de Conexão: {str(e)}"
    print(f"❌ Falha ao conectar no MongoDB: {e}")

# --- SEED DE DADOS (POPULAÇÃO INICIAL) ---
def seed_data():
    if not parts_collection: return
    try:
        # Só popula se a coleção estiver vazia
        if parts_collection.count_documents({}) == 0:
            file_path = "dataset_enterprise.json"
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    items = data.get("parts", []) if isinstance(data, dict) else data
                    if items:
                        parts_collection.insert_many(items)
                        print(f"✅ Seed: {len(items)} peças importadas do JSON.")
    except Exception as e:
        print(f"⚠️ Erro no processo de Seed: {e}")

# Executa o seed ao subir o serviço
seed_data()

# --- MIDDLEWARE (CORS) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Em produção, substitua pela URL da Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELOS DE DADOS ---
class LoginRequest(BaseModel):
    username: str
    password: str

# --- ROTAS DA API ---

@app.get("/")
def health_check():
    """Verifica se o servidor e o banco estão respondendo"""
    return {
        "service": "TechnoBolt Backend",
        "status": "online",
        "database": db_status,
        "ai_keys_active": len(VALID_GEMINI_KEYS)
    }

@app.post("/api/login")
def login(data: LoginRequest):
    """Realiza autenticação e retorna lojas permitidas"""
    if not users_collection:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    
    user = users_collection.find_one({"username": data.username, "password": data.password})
    if not user:
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    
    return {
        "name": user.get("name"),
        "role": user.get("role", "vendedor"),
        "allowed_stores": user.get("allowed_stores", []),
        "token": "bolt_session_active" # Token simplificado para MVP
    }

@app.get("/api/parts")
def get_parts(q: Optional[str] = None):
    """Busca peças no estoque com suporte a filtros"""
    if not parts_collection:
        return []
    
    query = {}
    if q:
        # Busca insensível a maiúsculas/minúsculas em nome, código ou marca
        query = {
            "$or": [
                {"name": {"$regex": q, "$options": "i"}},
                {"code": {"$regex": q, "$options": "i"}},
                {"brand": {"$regex": q, "$options": "i"}}
            ]
        }
    
    try:
        cursor = parts_collection.find(query).limit(50)
        parts = []
        for p in cursor:
            p["id"] = str(p.pop("_id")) # Converte ID do Mongo para String
            parts.append(p)
        return parts
    except Exception:
        return []

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    """Vision AI: Identifica peça via foto usando rodízio de chaves Gemini"""
    if not VALID_GEMINI_KEYS:
        raise HTTPException(status_code=500, detail="Configuração de IA ausente")
    
    try:
        # Escolhe uma chave aleatória das 7 disponíveis para distribuir carga
        api_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        image_content = await file.read()
        
        prompt = """
        Você é um especialista em catálogo de autopeças. 
        Analise a imagem e identifique o item.
        Retorne OBRIGATORIAMENTE apenas um JSON puro:
        {
            "name": "Nome técnico da peça",
            "possible_vehicles": ["Veículo A", "Veículo B"],
            "category": "Categoria da Peça",
            "confidence": "Alta/Média/Baixa"
        }
        """
        
        response = model.generate_content([
            prompt,
            {"mime_type": file.content_type, "data": image_content}
        ])
        
        # Limpa possíveis blocos de código markdown que a IA possa enviar
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_json)
        
    except Exception as e:
        print(f"❌ Erro na IA: {e}")
        raise HTTPException(status_code=500, detail="Falha ao processar imagem")

# --- EXECUÇÃO ---
if __name__ == "__main__":
    import uvicorn
    # O Render detecta a porta automaticamente via variável PORT
    uvicorn.run(app, host="0.0.0.0", port=PORT)
