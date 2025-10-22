# test_your_channel.py
# Apne test channel ki ID nikalne ke liye

from telethon.sync import TelegramClient
from dotenv import load_dotenv
import os

load_dotenv()

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")

print("🔍 Finding your channels...\n")

with TelegramClient('session_name', api_id, api_hash) as client:
    print("=" * 70)
    print("YOUR CHANNELS (where you are admin/creator)")
    print("=" * 70)
    
    for dialog in client.iter_dialogs():
        # Only show channels where you are admin
        if hasattr(dialog.entity, 'broadcast') and dialog.entity.broadcast:
            print(f"\n📢 Name: {dialog.name}")
            print(f"   ID: {dialog.id}")
            print(f"   Type: Channel")
            
            # Check if you're admin
            try:
                if dialog.entity.creator or (hasattr(dialog.entity, 'admin_rights') and dialog.entity.admin_rights):
                    print(f"   ✅ You are ADMIN/CREATOR")
            except:
                pass
            
            print("-" * 70)

print("\n✅ Done! Copy your channel ID and paste in .env file")
print("Example: TG_CHANNEL=-1001234567890")