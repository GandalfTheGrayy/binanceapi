#!/usr/bin/env python3
"""
Binance Futures Bot Launcher (Tek Servis)

Bu script aynı anda FastAPI backend'i (uvicorn) ve Streamlit frontend'i başlatır.
Render deployment için tek servis mimaride tüm özellikler aktif olur.
"""

import os
import subprocess
import sys

def start_streamlit_bg():
    """Streamlit frontend'i iç portta arka planda başlat"""
    s_port = os.getenv("STREAMLIT_PORT", "8501")
    # FastAPI uygulamasına iç URL'yi bildir
    os.environ["STREAMLIT_INTERNAL_URL"] = f"http://127.0.0.1:{s_port}"
    print(f"🎨 Streamlit (internal) başlatılıyor - Port: {s_port}")
    return subprocess.Popen([
        "streamlit",
        "run",
        "streamlit_app.py",
        "--server.port", s_port,
        "--server.address", "127.0.0.1",
        "--server.headless", "true",
    ])

def start_backend_foreground():
    """FastAPI backend'i public PORT üzerinde başlat (uvicorn)"""
    port = os.getenv("PORT", "8000")
    print(f"🧠 FastAPI backend (public) başlatılıyor - Port: {port}")
    subprocess.run([
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", port,
    ])

if __name__ == "__main__":
    print("🚀 Binance Futures Bot başlatılıyor (FastAPI public + Streamlit internal)...")
    # Streamlit'in backend'i doğru bulabilmesi için, PORT değerinden BACKEND_URL'i üret
    try:
        _port = os.getenv("PORT", "8000")
        os.environ.setdefault("BACKEND_URL", f"http://127.0.0.1:{_port}")
    except Exception:
        pass
    streamlit_proc = start_streamlit_bg()
    try:
        start_backend_foreground()
    finally:
        # Backend kapandığında Streamlit’i de sonlandır
        try:
            streamlit_proc.terminate()
        except Exception:
            pass