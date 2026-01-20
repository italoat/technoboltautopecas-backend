import os
import random
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TechnoBolt AutoPeças Backend")

# Configuração de CORS para a Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Em produção, substitua pela URL da Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lista de Chaves Gemini para Rodízio (Round Robin)
GEMINI_KEYS = [
    os.getenv("GEMINI_CHAVE_1"), os.getenv("GEMINI_CHAVE_2"),
    os.getenv("GEMINI_CHAVE_3"), os.getenv("GEMINI_CHAVE_4"),
    os.getenv("GEMINI_CHAVE_5"), os.getenv("GEMINI_CHAVE_6"),
    os.getenv("GEMINI_CHAVE_7")
]

# Conexão MongoDB Atlas
MONGO_URI = f"mongodb+srv://{os.getenv('MONGO_USER')}:{os.getenv('MONGO_PASS')}@{os.getenv('MONGO_HOST')}/?retryWrites=true&w=majority"
client = MongoClient(MONGO_URI)
db = client.technobolt_db

# Helper para obter uma chave aleatória ou sequencial da IA
def get_ai_model():
    key = random.choice([k for k in GEMINI_KEYS if k])
    genai.configure(api_key=key)
    return genai.GenerativeModel('gemini-1.5-flash')

# --- ROTAS ---

@app.get("/status")
async def get_status():
    return {"status": "online", "database": "connected", "keys_active": len([k for k in GEMINI_KEYS if k])}

@app.post("/api/ai/identify-part")
async def identify_part(file: UploadFile = File(...)):
    """
    TechnoBolt Vision: Identifica uma peça de carro através de foto
    """
    try:
        model = get_ai_model()
        image_data = await file.read()
        
        prompt = """
        Você é um especialista em autopeças. Analise esta imagem e retorne:
        1. Nome exato da peça.
        2. Possíveis montadoras e modelos.
        3. Código de referência (se visível).
        4. Dica técnica de instalação.
        Responda em formato JSON.
        """
        
        response = model.generate_content([
            prompt,
            {"mime_type": "image/jpeg", "data": image_data}
        ])
        
        return {"result": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/parts/search")
async def search_parts(q: str):
    """
    Busca peças no MongoDB (Dataset Enterprise)
    """
    parts = list(db.parts.find({"$or": [
        {"name": {"$regex": q, "$options": "i"}},
        {"code": {"$regex": q, "$options": "i"}}
    ]}).limit(10))
    
    # Converter ObjectID do Mongo para String
    for part in parts:
        part["_id"] = str(part["_id"])
        
    return parts

if __name__ == "__main__":
    import uvicorn
    # O Render usa a porta da variável de ambiente PORT
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
