"""
Run the FastAPI server using uvicorn.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import uvicorn
from src.api.server import create_app
from src.config import API_HOST, API_PORT

app = create_app()

if __name__ == "__main__":
    print(f"Starting Qwen Local Proxy on {API_HOST}:{API_PORT}")
    print("Press Ctrl+C to stop")
    uvicorn.run(app, host=API_HOST, port=API_PORT)
