#!/usr/bin/env python3
"""
Binance Futures Bot Launcher (Tek Servis)

Bu script aynı anda FastAPI backend'i (uvicorn) ve Streamlit frontend'i başlatır.
Render deployment için tek servis mimaride tüm özellikler aktif olur.
"""

import os
import subprocess
import sys

def start_backend():
    """FastAPI backend'i localhost:8000 üzerinde başlat"""
    port = os.getenv("BACKEND_PORT", "8000")
    print(f"🧠 FastAPI backend başlatılıyor - Port: {port}")
    return subprocess.Popen([
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host", "127.0.0.1",
        "--port", port,
    ])

def start_streamlit():
    """Streamlit frontend'i ana portta başlat (webhook handling dahil)"""
    port = os.getenv("PORT", "8501")  # Render'da PORT env var, lokal 8501
    print(f"🎨 Streamlit (webhook dahil) başlatılıyor - Port: {port}")
    subprocess.run([
        "streamlit", 
        "run", 
        "streamlit_app.py", 
        "--server.port", port,
        "--server.address", "0.0.0.0"
    ])

if __name__ == "__main__":
    print("🚀 Binance Futures Bot başlatılıyor (FastAPI + Streamlit)...")
    backend_proc = start_backend()
    try:
        start_streamlit()
    finally:
        # Streamlit kapandığında backend’i de sonlandır
        try:
            backend_proc.terminate()
        except Exception:
            pass