import os
import uvicorn
from app.main import app
if __name__=='__main__':
 port=int(os.getenv('HUB_PORT','8088'))
 print(f'\nRoom Hub 0.1.1\nManager: http://localhost:{port}/manager\nAdmin key: {app.state.admin_token}\nKeep this key private. Use your LAN IP on iPad.\n',flush=True)
 uvicorn.run(app,host=os.getenv('HUB_HOST','0.0.0.0'),port=port,workers=1,ws_max_size=16384)
