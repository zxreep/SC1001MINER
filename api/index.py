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
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # e.g., "@my_channel_username" or "-100..."
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Your numeric Telegram ID
DB_NAME = "filestorebot"
COLLECTION_NAME = "files"

# --- DATABASE CONNECTION ---
# We initialize the client outside the handler to take advantage of container reuse in Vercel
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
        # 'left' means they are not in the channel. 'kicked' means banned.
        if member.status in ['left', 'kicked']:
            return False
        return True
    except Exception as e:
        print(f"Error checking subscription: {e}")
        # If bot isn't admin in channel or channel is invalid, fail gracefully (allow access or log error)
        return False

# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # 1. Check Force Subscribe
    if CHANNEL_ID:
        is_subbed = await is_user_subscribed(context.bot, user.id, CHANNEL_ID)
        if not is_subbed:
            # Generate the same start link for the "Try Again" button
            start_param = args[0] if args else ""
            deep_link = f"https://t.me/{context.bot.username}?start={start_param}"
            
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")],
                [InlineKeyboardButton("🔄 Try Again", url=deep_link)]
            ]
            await update.message.reply_text(
                f"⚠️ **Access Denied**\n\nYou must join our channel to access this file.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # 2. If no args, just welcome
    if not args:
        await update.message.reply_text(f"Hello {user.first_name}! I am a File Store Bot.")
        return

    # 3. Retrieve File
    short_code = args[0]
    file_doc = await collection.find_one({"code": short_code})

    if not file_doc:
        await update.message.reply_text("❌ **File not found.** It may have been deleted.")
        return

    # Send the file based on type
    file_id = file_doc['file_id']
    file_type = file_doc.get('type', 'document')
    caption = f"Here is your file!\n\n📂 Code: `{short_code}`"

    try:
        if file_type == 'video':
            await update.message.reply_video(video=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        elif file_type == 'photo':
            await update.message.reply_photo(photo=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_document(document=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text("❌ Failed to send file. It might be restricted.")


async def admin_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles file uploads from Admin."""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        return # Ignore non-admins silently

    # Determine file type and ID
    message = update.message
    file_id = None
    file_type = 'document'

    if message.document:
        file_id = message.document.file_id
        file_type = 'document'
    elif message.video:
        file_id = message.video.file_id
        file_type = 'video'
    elif message.photo:
        file_id = message.photo[-1].file_id # Get highest resolution
        file_type = 'photo'

    if not file_id:
        await update.message.reply_text("Please send a Document, Video, or Photo.")
        return

    # Generate Code and Save
    short_code = generate_short_code()
    
    # Ensure code uniqueness (simple check)
    while await collection.find_one({"code": short_code}):
        short_code = generate_short_code()

    await collection.insert_one({
        "code": short_code,
        "file_id": file_id,
        "type": file_type,
        "uploader": user_id
    })

    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={short_code}"

    await update.message.reply_text(
        f"✅ **File Saved!**\n\n🔗 **Link:**\n`{link}`",
        parse_mode=ParseMode.MARKDOWN
    )

# --- VERCEL WEBHOOK HANDLER ---
# ... inside api/index.py ...

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle GET requests (e.g., browser visits) to check status."""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running! Send POST requests via Telegram.")

    def do_POST(self):
        """Handle the webhook request from Telegram."""
        # ... (rest of your existing do_POST code) ...

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        """Handle the webhook request from Telegram."""
        content_len = int(self.headers.get('Content-Length', 0))
        post_body = self.rfile.read(content_len)
        data = json.loads(post_body.decode('utf-8'))

        # Run the async process
        try:
            asyncio.run(self.process_update(data))
        except Exception as e:
            print(f"Error processing update: {e}")
            
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    async def process_update(self, data):
        """Initialize bot and process the update."""
        # Initialize Application
        app = Application.builder().token(TOKEN).build()

        # Add Handlers
        app.add_handler(CommandHandler("start", start))
        
        # Admin File Filter: Only accept files from the specific ADMIN_ID
        admin_filter = filters.User(user_id=ADMIN_ID) & (filters.Document.ALL | filters.VIDEO | filters.PHOTO)
        app.add_handler(MessageHandler(admin_filter, admin_upload))

        # Initialize the bot (v20+ requirement)
        await app.initialize()
        
        # Process the update
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        
        # Shutdown cleanly
        await app.shutdown()

