#!/usr/bin/env python3
"""
Hem FastAPI backend'i hem de Streamlit frontend'i aynı anda başlatan script
"""
import subprocess
import sys
import os
import time
from threading import Thread

def start_fastapi():
    """FastAPI backend'i başlat - Ana portta (Render'da dışarıya açık)"""
    print("🚀 FastAPI backend başlatılıyor...")
    # Render'da $PORT environment variable'ı ana port (dışarıya açık)
    port = os.getenv("PORT", "8000")
    cmd = [
        sys.executable, "-m", "uvicorn", 
        "app.main:app", 
        "--host", "0.0.0.0", 
        "--port", port
    ]
    subprocess.run(cmd)

def start_streamlit():
    """Streamlit frontend'i başlat - Sabit 8501 portunda (internal)"""
    print("🎨 Streamlit frontend başlatılıyor...")
    # FastAPI'nin başlaması için kısa bir bekleme
    time.sleep(3)
    
    # Streamlit'i sabit 8501 portunda çalıştır (internal)
    cmd = [
        sys.executable, "-m", "streamlit", "run", 
        "streamlit_app.py",
        "--server.port", "8501",
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false"
    ]
    subprocess.run(cmd)

if __name__ == "__main__":
    print("🔥 Binance API Uygulaması Başlatılıyor...")
    print("📡 Backend: http://localhost:8000")
    print("🌐 Frontend: http://localhost:8501")
    
    # FastAPI'yi arka planda başlat
    fastapi_thread = Thread(target=start_fastapi, daemon=True)
    fastapi_thread.start()
    
    # Streamlit'i ana thread'de başlat
    start_streamlit()