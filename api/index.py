import os
import json
import asyncio
import secrets
import string
from http.server import BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from motor.motor_asyncio import AsyncIOMotorClient

# --- CONFIGURATION ---
# Load environment variables
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # e.g., "@my_channel" or "-100123456789"
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

DB_NAME = "filestorebot"
COLLECTION_NAME = "files"

# --- DATABASE CONNECTION ---
# Initialize globally for connection pooling across warm starts
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client[DB_NAME]
collection = db[COLLECTION_NAME]

# --- HELPER FUNCTIONS ---

def generate_short_code(length=6):
    """Generates a random URL-safe string."""
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

async def is_user_subscribed(bot, user_id, channel_id):
    """Checks if the user is a member of the required channel."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        if member.status in ['left', 'kicked']:
            return False
        return True
    except Exception:
        # If bot is not admin in the channel or ID is wrong, default to True (allow access)
        # or False (deny access) depending on your preference.
        return False

# --- BOT COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user = update.effective_user
    args = context.args

    # 1. Force Subscribe Check
    if CHANNEL_ID:
        is_subbed = await is_user_subscribed(context.bot, user.id, CHANNEL_ID)
        if not is_subbed:
            # Reconstruct the start link for the "Try Again" button
            start_param = args[0] if args else ""
            deep_link = f"https://t.me/{context.bot.username}?start={start_param}"
            
            # Create Join Button
            channel_url = f"https://t.me/{CHANNEL_ID.replace('@', '')}"
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=channel_url)],
                [InlineKeyboardButton("🔄 Try Again", url=deep_link)]
            ]
            await update.message.reply_text(
                f"⚠️ **Access Denied**\n\nPlease join our channel to access this file.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # 2. If no args (just /start), welcome the user
    if not args:
        await update.message.reply_text(f"👋 Hello {user.first_name}! Send me a file to store (Admin only).")
        return

    # 3. Retrieve File from DB
    short_code = args[0]
    file_doc = await collection.find_one({"code": short_code})

    if not file_doc:
        await update.message.reply_text("❌ **File not found.** It may have been deleted.")
        return

    # 4. Send the file
    file_id = file_doc['file_id']
    file_type = file_doc.get('type', 'document')
    caption = f"Here is your file!\n📂 Code: `{short_code}`"

    try:
        if file_type == 'video':
            await update.message.reply_video(video=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        elif file_type == 'photo':
            await update.message.reply_photo(photo=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_document(document=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text("❌ Failed to send file. It might have been deleted from Telegram servers.")


async def admin_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles file uploads (Admin Only)."""
    user_id = update.effective_user.id
    
    # Security Check
    if user_id != ADMIN_ID:
        return # Ignore non-admins completely

    message = update.message
    file_id = None
    file_type = 'document'

    # Determine file type
    if message.document:
        file_id = message.document.file_id
        file_type = 'document'
    elif message.video:
        file_id = message.video.file_id
        file_type = 'video'
    elif message.photo:
        file_id = message.photo[-1].file_id # Best quality
        file_type = 'photo'

    if not file_id:
        await update.message.reply_text("Please send a Document, Video, or Photo.")
        return

    # Generate Short Code
    short_code = generate_short_code()
    # Basic collision check
    while await collection.find_one({"code": short_code}):
        short_code = generate_short_code()

    # Save to MongoDB
    await collection.insert_one({
        "code": short_code,
        "file_id": file_id,
        "type": file_type,
        "uploader": user_id,
        "created_at": message.date
    })

    # Reply with Link
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={short_code}"

    await update.message.reply_text(
        f"✅ **File Saved!**\n\n🔗 **Link:**\n`{link}`",
        parse_mode=ParseMode.MARKDOWN
    )

# --- VERCEL SERVERLESS HANDLER ---

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """
        FIX: Handles GET requests to prevent 501 errors.
        Useful for health checks via browser.
        """
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running. Send POST to this URL via Telegram Webhook.")

    def do_POST(self):
        """Handles the incoming updates from Telegram."""
        content_len = int(self.headers.get('Content-Length', 0))
        post_body = self.rfile.read(content_len)
        
        try:
            data = json.loads(post_body.decode('utf-8'))
            # Run the async process
            asyncio.run(self.process_update(data))
            
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print(f"Error in webhook: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal Server Error")

    async def process_update(self, data):
        """Builds the bot app and processes the specific update."""
        # We build the app on every request because Vercel is stateless
        app = Application.builder().token(TOKEN).build()

        # Add Handlers
        app.add_handler(CommandHandler("start", start))
        
        # Filter: Only Admin, Only Media
        admin_media_filter = filters.User(user_id=ADMIN_ID) & (filters.Document.ALL | filters.VIDEO | filters.PHOTO)
        app.add_handler(MessageHandler(admin_media_filter, admin_upload))

        # Initialize
        await app.initialize()
        
        # Process One Update
        try:
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
        except Exception as e:
            print(f"Update processing error: {e}")
        finally:
            # Clean shutdown to release resources
            await app.shutdown()
