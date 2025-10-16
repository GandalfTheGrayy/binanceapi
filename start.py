#!/usr/bin/env python3
"""
Binance Futures Bot Launcher

Bu script sadece Streamlit frontend'i başlatır.
Webhook handling dahil tüm işlemler Streamlit içinde yapılır.

Render deployment için optimize edilmiştir.
"""

import os
import subprocess

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
    print("🚀 Binance Futures Bot başlatılıyor...")
    print("📡 Webhook handling Streamlit içinde yapılacak")
    start_streamlit()