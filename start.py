#!/usr/bin/env python3
"""
Binance Futures Bot Launcher

Bu script hem FastAPI backend'i hem de Streamlit frontend'i başlatır.
- FastAPI: Port 8000'de çalışır (backend API)
- Streamlit: Ana PORT'ta çalışır (Render'da dışarıya açık)

Render deployment için optimize edilmiştir.
"""

import os
import subprocess
import threading
import time

def start_fastapi():
    """FastAPI backend'i port 8000'de başlat"""
    print("🚀 FastAPI backend başlatılıyor - Port: 8000")
    subprocess.run([
        "uvicorn", 
        "main:app", 
        "--host", "0.0.0.0", 
        "--port", "8000",
        "--reload"
    ])

def start_streamlit():
    """Streamlit frontend'i ana portta başlat (Render'da dışarıya açık)"""
    port = os.getenv("PORT", "8501")  # Render'da PORT env var, lokal 8501
    print(f"🎨 Streamlit frontend başlatılıyor - Port: {port}")
    subprocess.run([
        "streamlit", 
        "run", 
        "streamlit_app.py", 
        "--server.port", port,
        "--server.address", "0.0.0.0"
    ])

if __name__ == "__main__":
    print("🔥 Binance API Uygulaması Başlatılıyor...")
    print("📡 Backend: http://localhost:8000")
    print("🌐 Frontend: http://localhost:8501")
    
    # FastAPI'yi arka planda başlat
    fastapi_thread = Thread(target=start_fastapi, daemon=True)
    fastapi_thread.start()
    
    # Streamlit'i ana thread'de başlat
    start_streamlit()