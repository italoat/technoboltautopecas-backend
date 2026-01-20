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
from bson.objectid import ObjectId
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

app = FastAPI(title="TechnoBolt Enterprise API")

# --- CONFIGURAÇÕES DE AMBIENTE ---
PORT = int(os.environ.get("PORT", 10000))
mongo_user = quote_plus(os.getenv('MONGO_USER', ''))
mongo_pass = quote_plus(os.getenv('MONGO_PASS', ''))
mongo_host = os.getenv('MONGO_HOST', '')

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
db = None

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.technoboltauto        
    parts_collection = db.estoque     
    users_collection = db.usuarios    
    client.admin.command('ping')
    db_status = "Conectado e Operacional"
    print("✅ MongoDB Atlas: Conexão estabelecida com sucesso!")
except Exception as e:
    db_status = f"Erro de Conexão: {str(e)}"
    print(f"❌ Falha ao conectar no MongoDB: {e}")

# --- MODELS (PYDANTIC) ---
class LoginRequest(BaseModel):
    username: str
    password: str

class SaleItem(BaseModel):
    part_id: str
    quantity: int
    unit_price: float

class SaleRequest(BaseModel):
    store_id: int
    items: List[SaleItem]
    payment_method: str
    total: float

class TransferRequest(BaseModel):
    part_id: str
    from_store_id: int
    to_store_id: int
    quantity: int
    user_id: str

# --- ROTAS ---

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
    if parts_collection is None: return []
    
    query = {}
    if q:
        query = {
            "$or": [
                {"PRODUTO_NOME": {"$regex": q, "$options": "i"}},
                {"COD_FABRICANTE": {"$regex": q, "$options": "i"}},
                {"SKU_ID": {"$regex": q, "$options": "i"}},
                {"MARCA": {"$regex": q, "$options": "i"}},
                {"COD_EQUIVALENTES": {"$regex": q, "$options": "i"}}
            ]
        }
    
    try:
        cursor = parts_collection.find(query).limit(50)
        parts = []
        for p in cursor:
            # Cálculo de Estoque Total
            estoque_rede = p.get("ESTOQUE_REDE", [])
            total_qtd = 0
            if isinstance(estoque_rede, list):
                for loja in estoque_rede:
                    qtd = loja.get("qtd", 0)
                    if isinstance(qtd, (int, float)):
                        total_qtd += int(qtd)
            
            parts.append({
                "id": str(p.get("_id")),
                "name": p.get("PRODUTO_NOME", "Nome Indisponível"),
                "code": p.get("COD_FABRICANTE", p.get("SKU_ID", "")),
                "brand": p.get("MARCA", "Genérica"),
                "image": p.get("IMAGEM_URL", ""),
                "price": p.get("PRECO_VENDA", 0.0),
                "price_retail": p.get("PRECO_VENDA", 0.0),
                "quantity": total_qtd,
                "total_stock": total_qtd,
                "stock_locations": estoque_rede,
                "application": p.get("APLICACAO_VEICULOS", "Aplicação não informada"),
                "category": p.get("CATEGORIA", "Geral")
            })
        return parts
    except Exception as e:
        print(f"Erro busca: {e}")
        return []

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    if not VALID_GEMINI_KEYS:
        print("❌ Erro: Nenhuma chave Gemini configurada.")
        raise HTTPException(status_code=500, detail="Serviço de IA não configurado no servidor.")
    
    try:
        # 1. Configura a chave
        api_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=api_key)
        
        # 2. Prepara o Modelo
        model = genai.GenerativeModel("models/gemini-3-flash-preview", "models/gemini-2.5-flash", "models/gemini-2.0-flash", "models/gemini-flash-latest")
        image_content = await file.read()
        
        # 3. Prompt Refinado
        prompt = """
        Atue como um especialista em autopeças. Analise a imagem técnica.
        Retorne APENAS um JSON válido (sem markdown, sem crases) com este formato:
        {
            "name": "Nome técnico curto da peça",
            "part_number": "Código se visível ou vazio",
            "possible_vehicles": ["Lista de 2 ou 3 carros compatíveis"],
            "category": "Categoria da peça",
            "confidence": "Alta"
        }
        """
        
        # 4. Chama a IA
        response = model.generate_content([
            prompt,
            {"mime_type": file.content_type, "data": image_content}
        ])
        
        # 5. Limpeza da Resposta (Crucial para evitar erros de JSON)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        
        return json.loads(clean_text)

    except Exception as e:
        print(f"❌ Erro Crítico na IA: {e}")
        # Retorna um erro 500 detalhado para o frontend entender
        raise HTTPException(status_code=500, detail=f"Erro ao processar imagem: {str(e)}")

@app.post("/api/sales/checkout")
def checkout(sale: SaleRequest):
    if parts_collection is None:
        raise HTTPException(status_code=503, detail="Banco offline")
    try:
        db.vendas.insert_one(sale.dict())
        for item in sale.items:
            parts_collection.update_one(
                {"_id": ObjectId(item.part_id), "ESTOQUE_REDE.loja_id": sale.store_id},
                {"$inc": {"ESTOQUE_REDE.$.qtd": -item.quantity}}
            )
        return {"status": "success"}
    except Exception as e:
        print(f"Erro PDV: {e}")
        raise HTTPException(status_code=500, detail="Erro ao processar venda")

@app.post("/api/logistics/transfer")
def transfer_stock(req: TransferRequest):
    if parts_collection is None:
        raise HTTPException(status_code=503, detail="Banco offline")
    try:
        part_oid = ObjectId(req.part_id)
        
        # Verifica Origem
        part = parts_collection.find_one(
            {"_id": part_oid, "ESTOQUE_REDE.loja_id": req.from_store_id},
            {"ESTOQUE_REDE.$": 1}
        )
        if not part: raise HTTPException(400, "Origem sem registro deste produto")
        
        curr_qtd = part["ESTOQUE_REDE"][0].get("qtd", 0)
        if curr_qtd < req.quantity:
            raise HTTPException(400, f"Saldo insuficiente. Disponível: {curr_qtd}")

        # Debita Origem
        parts_collection.update_one(
            {"_id": part_oid, "ESTOQUE_REDE.loja_id": req.from_store_id},
            {"$inc": {"ESTOQUE_REDE.$.qtd": -req.quantity}}
        )

        # Credita Destino
        dest_exists = parts_collection.find_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": req.to_store_id})
        if dest_exists:
            parts_collection.update_one(
                {"_id": part_oid, "ESTOQUE_REDE.loja_id": req.to_store_id},
                {"$inc": {"ESTOQUE_REDE.$.qtd": req.quantity}}
            )
        else:
            # Cria entrada na loja destino se não existir
            new_entry = {"loja_id": req.to_store_id, "nome": f"Loja {req.to_store_id}", "qtd": req.quantity, "local": "Recebimento"}
            parts_collection.update_one({"_id": part_oid}, {"$push": {"ESTOQUE_REDE": new_entry}})

        return {"status": "success"}
    except Exception as e:
        print(f"Erro Transferência: {e}")
        raise HTTPException(500, str(e))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
