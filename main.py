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
from bson.objectid import ObjectId
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TechnoBolt Enterprise API")

# --- CONFIGURAÇÕES ---
PORT = int(os.environ.get("PORT", 10000))
mongo_user = quote_plus(os.getenv('MONGO_USER', ''))
mongo_pass = quote_plus(os.getenv('MONGO_PASS', ''))
mongo_host = os.getenv('MONGO_HOST', '')

if not mongo_host:
    MONGO_URI = "mongodb://localhost:27017"
else:
    MONGO_URI = f"mongodb+srv://{mongo_user}:{mongo_pass}@{mongo_host}/?retryWrites=true&w=majority"

GEMINI_KEYS = [os.getenv(f"GEMINI_CHAVE_{i}") for i in range(1, 8)]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

MY_ENGINES = [
    "models/gemini-3-flash-preview", 
    "models/gemini-2.5-flash", 
    "models/gemini-2.0-flash", 
    "models/gemini-flash-latest",
    "gemini-1.5-flash"
]

# --- BANCO DE DADOS ---
db_status = "Desconectado"
parts_collection = None
users_collection = None
transfers_collection = None # Nova coleção
db = None

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.technoboltauto        
    parts_collection = db.estoque     
    users_collection = db.usuarios
    transfers_collection = db.transferencias # <--- Coleção de Pedidos de Transferência
    client.admin.command('ping')
    db_status = "Conectado e Operacional"
    print("✅ MongoDB Atlas: Conexão estabelecida!")
except Exception as e:
    db_status = f"Erro de Conexão: {str(e)}"
    print(f"❌ Falha ao conectar no MongoDB: {e}")

# --- MODELS ---
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

# Modelo atualizado para o novo fluxo
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

# --- ROTAS ---

@app.get("/")
def health_check():
    return {"status": "online", "db": db_status}

@app.post("/api/login")
def login(data: LoginRequest):
    if users_collection is None: raise HTTPException(503, "DB Offline")
    user = users_collection.find_one({"username": data.username, "password": data.password})
    if not user: raise HTTPException(401, "Credenciais inválidas")
    return {
        "name": user.get("name"),
        "role": user.get("role", "vendedor"),
        "allowed_stores": user.get("allowed_stores", []),
        "token": "bolt_session_active",
        "currentStore": {"id": user.get("allowed_stores", [1])[0], "name": "Loja Padrão"} # Mock simples
    }

@app.get("/api/parts")
def get_parts(q: Optional[str] = None):
    if parts_collection is None: return []
    query = {}
    if q:
        query = {"$or": [
            {"PRODUTO_NOME": {"$regex": q, "$options": "i"}},
            {"COD_FABRICANTE": {"$regex": q, "$options": "i"}},
            {"SKU_ID": {"$regex": q, "$options": "i"}},
            {"MARCA": {"$regex": q, "$options": "i"}}
        ]}
    try:
        cursor = parts_collection.find(query).limit(50)
        parts = []
        for p in cursor:
            estoque_rede = p.get("ESTOQUE_REDE", [])
            total_qtd = sum([int(l.get("qtd", 0)) for l in estoque_rede if isinstance(l.get("qtd"), (int, float))])
            parts.append({
                "id": str(p.get("_id")),
                "name": p.get("PRODUTO_NOME", "Nome Indisponível"),
                "code": p.get("COD_FABRICANTE", ""),
                "image": p.get("IMAGEM_URL", ""),
                "price": p.get("PRECO_VENDA", 0.0),
                "total_stock": total_qtd,
                "stock_locations": estoque_rede
            })
        return parts
    except Exception as e:
        print(e)
        return []

@app.post("/api/ai/identify")
async def identify_part(file: UploadFile = File(...)):
    if not VALID_GEMINI_KEYS: raise HTTPException(500, "Sem chaves IA")
    image_content = await file.read()
    
    for engine in MY_ENGINES:
        try:
            genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
            model = genai.GenerativeModel(engine)
            prompt = """Retorne APENAS JSON: {"name": "Peça", "part_number": "COD", "possible_vehicles": ["Carro A"], "category": "Cat", "confidence": "Alta"}"""
            response = model.generate_content([prompt, {"mime_type": file.content_type, "data": image_content}])
            text = response.text
            start, end = text.find('{'), text.rfind('}') + 1
            if start != -1 and end != 0: return json.loads(text[start:end])
        except: continue
    raise HTTPException(500, "Falha na IA")

@app.post("/api/sales/checkout")
def checkout(sale: SaleRequest):
    if parts_collection is None: raise HTTPException(503, "DB Offline")
    db.vendas.insert_one(sale.dict())
    for item in sale.items:
        parts_collection.update_one(
            {"_id": ObjectId(item.part_id), "ESTOQUE_REDE.loja_id": sale.store_id},
            {"$inc": {"ESTOQUE_REDE.$.qtd": -item.quantity}}
        )
    return {"status": "success"}

# --- NOVAS ROTAS DE LOGÍSTICA (WORKFLOW) ---

# 1. CRIAR SOLICITAÇÃO
@app.post("/api/logistics/request")
def request_transfer(req: TransferRequest):
    if transfers_collection is None: raise HTTPException(503, "DB Offline")
    
    # Busca nome da peça para facilitar exibição
    part = parts_collection.find_one({"_id": ObjectId(req.part_id)})
    part_name = part.get("PRODUTO_NOME") if part else "Peça Desconhecida"
    part_image = part.get("IMAGEM_URL") if part else ""

    doc = {
        "part_id": req.part_id,
        "part_name": part_name,
        "part_image": part_image,
        "from_store_id": req.from_store_id, # Loja que vai CEDER a peça
        "to_store_id": req.to_store_id,     # Loja que PEDIU (Destino)
        "quantity": req.quantity,
        "type": req.type, # 'ENTREGA' ou 'RETIRADA'
        "status": "PENDENTE", # PENDENTE, SEPARACAO, TRANSITO, CONCLUIDO, REJEITADO
        "created_at": datetime.now(),
        "history": [{
            "status": "PENDENTE", 
            "user": req.user_id, 
            "time": datetime.now()
        }]
    }
    transfers_collection.insert_one(doc)
    return {"status": "success", "message": "Solicitação criada"}

# 2. LISTAR SOLICITAÇÕES
@app.get("/api/logistics/list")
def list_transfers(store_id: int):
    if transfers_collection is None: return []
    
    # Busca pedidos onde a loja é Origem (para aprovar) OU Destino (para receber)
    cursor = transfers_collection.find({
        "$or": [
            {"from_store_id": int(store_id)},
            {"to_store_id": int(store_id)}
        ]
    }).sort("created_at", -1)
    
    results = []
    for doc in cursor:
        doc["id"] = str(doc["_id"])
        del doc["_id"]
        results.append(doc)
    return results

# 3. ATUALIZAR STATUS (O CÉREBRO DO PROCESSO)
@app.post("/api/logistics/update-status")
def update_status(data: TransferStatusUpdate):
    if transfers_collection is None: raise HTTPException(503, "DB Offline")
    
    transfer = transfers_collection.find_one({"_id": ObjectId(data.transfer_id)})
    if not transfer: raise HTTPException(404, "Pedido não encontrado")
    
    current_status = transfer["status"]
    new_status = data.new_status
    transfer_type = transfer["type"]
    qty = transfer["quantity"]
    from_id = transfer["from_store_id"]
    to_id = transfer["to_store_id"]
    part_oid = ObjectId(transfer["part_id"])

    # Lógica de Movimentação de Estoque
    if new_status == "APROVADO": 
        # Verifica saldo na origem
        part = parts_collection.find_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": from_id}, {"ESTOQUE_REDE.$": 1})
        if not part: raise HTTPException(400, "Origem sem estoque")
        curr_qtd = part["ESTOQUE_REDE"][0].get("qtd", 0)
        
        if curr_qtd < qty: raise HTTPException(400, f"Saldo insuficiente. Disp: {curr_qtd}")

        if transfer_type == "RETIRADA":
            # RETIRADA: Movimenta imediatamente e finaliza
            # 1. Tira da Origem
            parts_collection.update_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": from_id}, {"$inc": {"ESTOQUE_REDE.$.qtd": -qty}})
            # 2. Põe no Destino
            _credit_dest(part_oid, to_id, qty)
            new_status = "CONCLUIDO"
        else:
            # ENTREGA: Tira da Origem (reserva) e vai para SEPARAÇÃO
            parts_collection.update_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": from_id}, {"$inc": {"ESTOQUE_REDE.$.qtd": -qty}})
            new_status = "SEPARACAO"

    elif new_status == "TRANSITO":
        if current_status != "SEPARACAO": raise HTTPException(400, "Fluxo inválido")
        # Apenas muda o status visual
    
    elif new_status == "CONCLUIDO":
        if transfer_type == "ENTREGA":
            if current_status != "TRANSITO": raise HTTPException(400, "Precisa estar em trânsito para receber")
            # Credita no Destino (só agora)
            _credit_dest(part_oid, to_id, qty)
        
    elif new_status == "REJEITADO":
        if current_status != "PENDENTE": raise HTTPException(400, "Não pode rejeitar se já iniciou")
        # Não faz nada com estoque

    # Atualiza documento
    transfers_collection.update_one(
        {"_id": ObjectId(data.transfer_id)},
        {
            "$set": {"status": new_status},
            "$push": {"history": {"status": new_status, "user": data.user_id, "time": datetime.now()}}
        }
    )
    
    return {"status": "success", "new_status": new_status}

def _credit_dest(part_oid, store_id, qty):
    # Função auxiliar para creditar estoque (cria loja se não existir)
    exists = parts_collection.find_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": store_id})
    if exists:
        parts_collection.update_one({"_id": part_oid, "ESTOQUE_REDE.loja_id": store_id}, {"$inc": {"ESTOQUE_REDE.$.qtd": qty}})
    else:
        new_entry = {"loja_id": store_id, "nome": f"Loja {store_id}", "qtd": qty, "local": "Receb."}
        parts_collection.update_one({"_id": part_oid}, {"$push": {"ESTOQUE_REDE": new_entry}})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
