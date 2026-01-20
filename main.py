from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import google.generativeai as genai
import urllib.parse
from pymongo import MongoClient
import os
import uvicorn

# --- INICIALIZAÇÃO ---
app = FastAPI(title="TechnoBolt Enterprise API")

# Configurar CORS (Permite que o Frontend React acesse este Backend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Em produção, troque '*' pela URL do seu Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONEXÃO MONGODB ---
# Nota: Em produção, use os.environ.get("MONGO_URI")
username = urllib.parse.quote_plus("technobolt")
password = urllib.parse.quote_plus("tech@132")
uri = f"mongodb+srv://{username}:{password}@cluster0.zbjsvk6.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
client = MongoClient(uri)
db = client.technoboltauto

# --- CONFIGURAÇÃO IA ---
api_key = os.environ.get("GEMINI_API_KEY")
if api_key: genai.configure(api_key=api_key)

# Lista de Failover (Seus modelos preferidos)
MODEL_FAILOVER_LIST = [
    "models/gemini-1.5-flash",
    "models/gemini-2.0-flash-lite-preview-02-05", 
    "models/gemini-pro"
]

# --- MODELOS DE DADOS (Pydantic) ---
class EstoqueItem(BaseModel):
    loja_id: int
    nome: str
    qtd: int
    local: str

class Produto(BaseModel):
    SKU_ID: str
    PRODUTO_NOME: str
    MARCA: str
    COD_FABRICANTE: str
    PRECO_VENDA: float
    ESTOQUE_REDE: List[EstoqueItem]
    TAGS_IA: Optional[str] = None

# --- LÓGICA DE IA ---
def call_gemini(prompt: str, image_bytes=None, system_instr=""):
    """Função de Failover para garantir resposta"""
    for model_name in MODEL_FAILOVER_LIST:
        try:
            model = genai.GenerativeModel(model_name, system_instruction=system_instr)
            payload = [prompt, {"mime_type": "image/jpeg", "data": image_bytes}] if image_bytes else [prompt]
            response = model.generate_content(payload)
            return response.text, model_name
        except:
            continue
    return "Erro: Motores de IA indisponíveis.", "OFFLINE"

# --- ROTAS DA API ---

@app.get("/")
def health_check():
    return {"status": "online", "message": "TechnoBolt Backend Operacional"}

@app.get("/produtos/busca", response_model=List[Produto])
def buscar_produtos(termo: str):
    """Busca inteligente no MongoDB"""
    if not termo: return []
    
    query = {
        "$or": [
            {"PRODUTO_NOME": {"$regex": termo, "$options": "i"}},
            {"COD_FABRICANTE": {"$regex": termo, "$options": "i"}},
            {"TAGS_IA": {"$regex": termo, "$options": "i"}},
            {"SKU_ID": {"$regex": termo, "$options": "i"}}
        ]
    }
    # Retorna top 10 produtos e limpa o _id
    produtos = list(db.estoque.find(query).limit(10))
    for p in produtos:
        if "_id" in p: del p["_id"]
    return produtos

@app.post("/ia/vision")
async def analisar_imagem(file: UploadFile = File(...)):
    """Vision AI: Recebe foto e devolve análise"""
    content = await file.read()
    prompt = "Identifique esta peça automotiva. Retorne: Nome Técnico, Código provável e Aplicação."
    res, mod = call_gemini(prompt, image_bytes=content, system_instr="Especialista em Autopeças Visuais")
    return {"analise": res, "motor": mod}

@app.post("/ia/chat")
def chat_tecnico(pergunta: str, carro: Optional[str] = None):
    """Chatbot Técnico"""
    prompt = f"Veículo: {carro}. Pergunta: {pergunta}" if carro else pergunta
    res, mod = call_gemini(prompt, system_instr="Consultor Técnico de Oficina Mecânica. Respostas breves.")
    return {"resposta": res, "motor": mod}

@app.post("/email/gerar")
def gerar_email(assunto: str, instrucoes: str, tabela: str):
    """Gerador de Email para Fornecedores"""
    prompt = f"Assunto: {assunto}\nInstruções: {instrucoes}\nDados da Tabela: {tabela}"
    res, mod = call_gemini(prompt, system_instr="Escreva um email formal para cotação/compra de autopeças.")
    return {"email": res, "motor": mod}

# Ponto de entrada para debug local
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
