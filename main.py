import os
import json
import random
import re
from typing import List, Optional
from urllib.parse import quote_plus
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
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

# SEUS MOTORES (Mantidos)
MY_ENGINES = [
    "models/gemini-3-flash-preview", 
    "models/gemini-2.5-flash", 
    "models/gemini-2.0-flash", 
    "models/gemini-flash-latest",
    "gemini-1.5-flash"
]

# --- INICIALIZAÇÃO DO BANCO DE DADOS ---
db_status = "Desconectado"
parts_collection = None
users_collection = None
transfers_collection = None
sales_collection = None
db = None

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.technoboltauto        
    parts_collection = db.estoque     
    users_collection = db.usuarios
    transfers_collection = db.transferencias
    sales_collection = db.vendas
    
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
    name: str # Adicionado para visualização
    quantity: int
    unit_price: float

class SaleCreateRequest(BaseModel):
    store_id: int
    seller_name: str
    client_name: str
    discount_percent: float
    items: List[SaleItem]
    subtotal: float
    total: float

class SaleFinalizeRequest(BaseModel):
    sale_id: str
    payment_method: str

class TransferRequest(BaseModel):
    part_id: str
    from_store_id: int
    to_store_id: int
    quantity: int
    type: str # 'ENTREGA' ou 'RETIRADA'
    user_id: str

class TransferStatusUpdate(BaseModel):
    transfer_id: str
    new_status: str # APROVADO, REJEITADO, TRANSITO, CONCLUIDO
    user_id: str

# --- ROTAS GERAIS ---

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
        "token": "bolt_session_active",
        "currentStore": {"id": user.get("allowed_stores", [1])[0], "name": "Loja Padrão"}
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
            # --- CORREÇÃO DE ESTOQUE (Força Conversão Texto -> Int) ---
            estoque_rede = p.get("ESTOQUE_REDE", [])
            total_qtd = 0
            
            if isinstance(estoque_rede, list):
                for loja in estoque_rede:
                    raw_qtd = loja.get("qtd", 0)
                    try:
                        # Converte string para int (ex: "150" -> 150) e ignora erros
                        total_qtd += int(raw_qtd)
                    except (ValueError, TypeError):
                        pass 
            
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
        raise HTTPException(status_code=500, detail="Serviço de IA não configurado.")
    
    image_content = await file.read()
    last_error = ""
    success = False
    result_json = {}

    # Tenta seus motores em ordem
    for engine in MY_ENGINES:
        try:
            api_key = random.choice(VALID_GEMINI_KEYS)
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(engine)
            
            prompt = """
            Você é um especialista em autopeças. Analise esta imagem técnica.
            Retorne APENAS um JSON válido.
            Estrutura: {"name": "Peça", "part_number": "COD", "possible_vehicles": ["Carro A"], "category": "Cat", "confidence": "Alta"}
            """
            
            response = model.generate_content([prompt, {"mime_type": file.content_type, "data": image_content}])
            
            text = response.text
            start = text.find('{')
            end = text.rfind('}') + 1
            if start != -1 and end != 0:
                result_json = json.loads(text[start:end])
                success = True
                break
        except Exception as e:
            last_error = str(e)
            continue 

    if not success:
        # Fallback de JSON vazio se falhar tudo
        raise HTTPException(status_code=500, detail=f"Falha na IA: {last_error}")
    
    return result_json

# --- ROTAS DE VENDAS (PDV -> CAIXA) ---

# 1. PDV: Cria pré-venda (PENDENTE)
@app.post("/api/sales/create")
def create_sale(sale: SaleCreateRequest):
    if sales_collection is None: raise HTTPException(503, "DB Offline")
    
    doc = sale.dict()
    doc["status"] = "PENDENTE"
    doc["created_at"] = datetime.now()
    doc["payment_method"] = None
    
    result = sales_collection.insert_one(doc)
    return {"status": "success", "sale_id": str(result.inserted_id)}

# 2. CAIXA: Lista pendentes
@app.get("/api/sales/pending")
def list_pending_sales(store_id: int):
    if sales_collection is None: return []
    # Busca vendas pendentes desta loja
    cursor = sales_collection.find({"store_id": store_id, "status": "PENDENTE"}).sort("created_at", -1)
    sales = []
    for s in cursor:
        s["id"] = str(s["_id"])
        del s["_id"]
        sales.append(s)
    return sales

# 3. CAIXA: Finaliza e Baixa Estoque
@app.post("/api/sales/finalize")
def finalize_sale(req: SaleFinalizeRequest):
    if sales_collection is None: raise HTTPException(503, "DB Offline")
    
    sale = sales_collection.find_one({"_id": ObjectId(req.sale_id)})
    if not sale: raise HTTPException(404, "Venda não encontrada")
    if sale["status"] == "FINALIZADA": raise HTTPException(400, "Venda já finalizada")

    try:
        # Baixa Estoque
        for item in sale["items"]:
            parts_collection.update_one(
                {"_id": ObjectId(item["part_id"]), "ESTOQUE_REDE.loja_id": sale["store_id"]},
                {"$inc": {"ESTOQUE_REDE.$.qtd": -item["quantity"]}}
            )
        
        # Atualiza Venda
        sales_collection.update_one(
            {"_id": ObjectId(req.sale_id)},
            {"$set": {
                "status": "FINALIZADA", 
                "payment_method": req.payment_method,
                "finalized_at": datetime.now()
            }}
        )
        return {"status": "success"}
    except Exception as e:
        print(f"Erro finalizar: {e}")
        raise HTTPException(500, "Erro ao baixar estoque")

# --- ROTAS DE LOGÍSTICA ---

@app.post("/api/logistics/request")
def request_transfer(req: TransferRequest):
    if transfers_collection is None: raise HTTPException(503, "DB Offline")
    
    part = parts_collection.find_one({"_id": ObjectId(req.part_id)})
    part_name = part.get("PRODUTO_NOME") if part else "Desconhecido"
    part_image = part.get("IMAGEM_URL") if part else ""

    doc = {
        "part_id": req.part_id,
        "part_name": part_name,
        "part_image": part_image,
        "from_store_id": req.from_store_id,
        "to_store_id": req.to_store_id,
        "quantity": req.quantity,
        "type": req.type,
        "status": "PENDENTE",
        "created_at": datetime.now(),
        "history": [{"status": "PENDENTE", "user": req.user_id, "time": datetime.now()}]
    }
    transfers_collection.insert_one(doc)
    return {"status": "success"}

@app.get("/api/logistics/list")
def list_transfers(store_id: int):
    if transfers_collection is None: return []
    cursor = transfers_collection.find({
        "$or": [{"from_store_id": int(store_id)}, {"to_store_id": int(store_id)}]
    }).sort("created_at", -1)
    
    results = []
    for doc in cursor:
        doc["id"] = str(doc["_id"])
        del doc["_id"]
        results.append(doc)
    return results

@app.post("/api/logistics/update-status")
def update_status(data: TransferStatusUpdate):
    if transfers_collection is None: raise HTTPException(503, "DB Offline")
    
    transfer = transfers_collection.find_one({"_id": ObjectId(data.transfer_id)})
    if not transfer: raise HTTPException(404, "Pedido não encontrado")
    
    new_status = data.new_status
    qty = transfer["quantity"]
    from_id = transfer["from_store_id"]
    to_id = transfer["to_store_id"]
    part_oid = ObjectId(transfer["part_id"])

    if new_status == "APROVADO": 
        # Verifica Saldo
        part = parts_collection.find_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": from_id}, {"ESTOQUE_REDE.$": 1})
        if not part: raise HTTPException(400, "Origem sem estoque")
        
        # Converte para int seguro
        try:
            curr_qtd = int(part["ESTOQUE_REDE"][0].get("qtd", 0))
        except:
            curr_qtd = 0

        if curr_qtd < qty: raise HTTPException(400, f"Saldo insuficiente. Disp: {curr_qtd}")

        if transfer["type"] == "RETIRADA":
            # Baixa Origem
            parts_collection.update_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": from_id}, {"$inc": {"ESTOQUE_REDE.$.qtd": -qty}})
            # Credita Destino
            _credit_dest(part_oid, to_id, qty)
            new_status = "CONCLUIDO"
        else:
            # Baixa Origem (Reserva)
            parts_collection.update_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": from_id}, {"$inc": {"ESTOQUE_REDE.$.qtd": -qty}})
            new_status = "SEPARACAO"
    
    elif new_status == "CONCLUIDO" and transfer["type"] == "ENTREGA":
        _credit_dest(part_oid, to_id, qty)

    transfers_collection.update_one(
        {"_id": ObjectId(data.transfer_id)},
        {"$set": {"status": new_status}, "$push": {"history": {"status": new_status, "user": data.user_id}}}
    )
    return {"status": "success"}

def _credit_dest(part_oid, store_id, qty):
    exists = parts_collection.find_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": store_id})
    if exists:
        parts_collection.update_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": store_id}, {"$inc": {"ESTOQUE_REDE.$.qtd": qty}})
    else:
        new_entry = {"loja_id": store_id, "nome": f"Loja {store_id}", "qtd": qty, "local": "Receb."}
        parts_collection.update_one({"_id": part_oid}, {"$push": {"ESTOQUE_REDE": new_entry}})

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
