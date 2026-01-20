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
from pymongo.errors import OperationFailure
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

# Construção da URI
if not mongo_host:
    MONGO_URI = "mongodb://localhost:27017"
else:
    MONGO_URI = f"mongodb+srv://{mongo_user}:{mongo_pass}@{mongo_host}/?retryWrites=true&w=majority"

# Configuração das chaves Gemini
GEMINI_KEYS = [os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

# --- INICIALIZAÇÃO DO BANCO DE DADOS ---
db_status = "Desconectado"
parts_collection = None
users_collection = None

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.technoboltauto        
    parts_collection = db.estoque     
    users_collection = db.usuarios    
    
    # Teste de conexão
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
    # CORREÇÃO 1: Verificação explícita de None
    if parts_collection is None: 
        return
        
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
    allow_origins=["*"],
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
    return {
        "service": "TechnoBolt Backend",
        "status": "online",
        "database": db_status,
        "ai_keys_active": len(VALID_GEMINI_KEYS)
    }

@app.post("/api/login")
def login(data: LoginRequest):
    # CORREÇÃO 2: Verificação explícita de None
    if users_collection is None:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    
    user = users_collection.find_one({"username": data.username, "password": data.password})
    if not user:
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    
    return {
        "name": user.get("name"),
        "role": user.get("role", "vendedor"),
        "allowed_stores": user.get("allowed_stores", []),
        "token": "bolt_session_active"
    }

@app.get("/api/parts")
def get_parts(q: Optional[str] = None):
    # Verificação de segurança
    if parts_collection is None:
        return []
    
    query = {}
    if q:
        # CORREÇÃO 1: Buscando nos campos corretos do MongoDB (Português)
        query = {
            "$or": [
                {"PRODUTO_NOME": {"$regex": q, "$options": "i"}},
                {"COD_FABRICANTE": {"$regex": q, "$options": "i"}},
                {"MARCA": {"$regex": q, "$options": "i"}},
                {"SKU_ID": {"$regex": q, "$options": "i"}}
            ]
        }
    
    try:
        cursor = parts_collection.find(query).limit(50)
        parts = []
        for p in cursor:
            # CORREÇÃO 2: Mapeamento (De-Para) do Banco (PT) para o Frontend (EN)
            # O Frontend espera: id, name, code, brand, price_retail, quantity, image
            
            mapped_part = {
                "id": str(p.get("_id")),
                "name": p.get("PRODUTO_NOME", "Nome Indisponível"),
                "code": p.get("COD_FABRICANTE", p.get("SKU_ID", "")),
                "brand": p.get("MARCA", "Genérica"),
                "category": p.get("CATEGORIA", "Geral"),
                "price_retail": p.get("PRECO_VENDA", 0.0),
                "image": p.get("IMAGEM_URL", ""),
                "compatible_vehicles": [p.get("APLICACAO_VEICULOS")] if isinstance(p.get("APLICACAO_VEICULOS"), str) else [],
                
                # Tratamento simples de estoque: Se não tiver campo de qtd, assume 10 para teste
                # Futuramente, você deve somar o array 'ESTOQUE_REDE'
                "quantity": 10 
            }
            parts.append(mapped_part)
            
        return parts
    except Exception as e:
        print(f"Erro ao buscar peças: {e}")
        return []

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    if not VALID_GEMINI_KEYS:
        raise HTTPException(status_code=500, detail="Configuração de IA ausente")
    
    try:
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
        
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_json)
        
    except Exception as e:
        print(f"❌ Erro na IA: {e}")
        raise HTTPException(status_code=500, detail="Falha ao processar imagem")

if __name__ == "__main__":
    import uvicorn
    # A porta precisa ser 0.0.0.0 para o Render acessá-la externamente
    uvicorn.run(app, host="0.0.0.0", port=PORT)
