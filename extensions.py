from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# Inisialisasi plugin tanpa terikat ke aplikasi (app) dulu
db = SQLAlchemy()
login_manager = LoginManager()

from flask_socketio import SocketIO
socketio = SocketIO(cors_allowed_origins="*")