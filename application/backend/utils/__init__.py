import os
from config.settings import settings

def init_config():
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.FRAMES_DIR, exist_ok=True)
    os.makedirs(settings.RESULTS_DIR, exist_ok=True)

if __name__ == "__main__":
    init_config()
