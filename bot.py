import logging
import sqlite3
import requests
import json
import os
import random
import asyncio
import uvicorn
from fastapi import FastAPI
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.constants import ChatAction

# Keys/config
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
MODELSLAB_API_KEY = os.getenv('MODELSLAB_API_KEY')
MY_WALLET_ADDRESS = os.getenv('MY_WALLET_ADDRESS')
YOUR_WALLET_USERNAME = os.getenv('WALLET_USERNAME')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'

# TON Center config
TON_CENTER_URL = 'https://toncenter.com/api/v3/jetton/transfers'
USDT_JETTON_MASTER = 'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs'

# Conversation states
ASKING_TYPE, ASKING_HAIR, ASKING_BODY, ASKING_PERSONALITY, ASKING_AGE = range(5)

# DB setup with proper connection handling
def get_db_connection():
    conn = sqlite3.connect('users.db')
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                      user_id INTEGER PRIMARY KEY,
                      used_free_preview INTEGER DEFAULT 0,
                      system_prompt TEXT,
                      chat_history TEXT DEFAULT '[]',
                      current_session TEXT DEFAULT 'none',
                      message_count INTEGER DEFAULT 0,
                      girlfriend_name TEXT DEFAULT 'Your Girl',
                      user_name TEXT,
                      user_preferences TEXT DEFAULT '{}')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pending_payments (
                      user_id INTEGER PRIMARY KEY,
                      level TEXT,
                      timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)  # Reduce httpx spam
logger = logging.getLogger(__name__)

# Rate limit handling with exponential backoff
def make_openrouter_request(messages, max_tokens, temperature=0.85, retries=3):
    """Make OpenRouter request with retry logic and rate limit handling"""

    payload = {
        'model': 'cognitivecomputations/dolphin-mistral-24b-venice-edition:free',
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'top_p': 0.9,
        'frequency_penalty': 0.5,  # Higher to reduce repetition
        'presence_penalty': 0.4    # Encourage variety
    }
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'https://t.me',
        'X-Title': 'VirtualGF Bot'
    }

    for attempt in range(retries):
        try:
            resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)

            if resp.status_code == 200:
                response_data = resp.json()
                reply = response_data['choices'][0]['message']['content']
                return {'success': True, 'message': reply}

            elif resp.status_code == 429:
                # Rate limited - wait and retry
                wait_time = (2 ** attempt) * 3  # Exponential backoff: 3s, 6s, 12s
                logger.warning(f"Rate limited. Retrying in {wait_time}s... (attempt {attempt + 1}/{retries})")
                if attempt < retries - 1:
                    import time
                    time.sleep(wait_time)
                    continue
                else:
                    flirty_errors = [
                        "Mmm I'm getting too many requests right now babe... 😳 Give me like 30 seconds to catch my breath? 💕",
                        "Oof you're making me work too hard! 😅 Let me cool down for a sec... try again in 30? 😘",
                        "Babe you're wearing me out! 🥵 I need a quick break... message me again in half a minute? 💋"
                    ]
                    return {'success': False, 'error': 'rate_limit', 'message': random.choice(flirty_errors)}

            else:
                error_msg = resp.json().get('error', {}).get('message', 'Unknown error')
                logger.error(f"API error: {resp.status_code} - {error_msg}")
                return {'success': False, 'error': 'api_error', 'message': "Oops I got distracted for a sec... 🙈 What were you saying?"}

        except requests.exceptions.Timeout:
            logger.error(f"Request timeout (attempt {attempt + 1}/{retries})")
            if attempt < retries - 1:
                continue
            return {'success': False, 'error': 'timeout', 'message': "Sorry babe I zoned out... 😅 Say that again?"}

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return {'success': False, 'error': 'unknown', 'message': "Something weird just happened... try again? 🤔"}

    return {'success': False, 'error': 'max_retries', 'message': "I'm having connection issues... 😔 Give me a minute?"}

def get_chat_response(user_id, user_message):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        row = cursor.execute('SELECT system_prompt, chat_history, current_session, used_free_preview, girlfriend_name, user_name, user_preferences FROM users WHERE user_id = ?', (user_id,)).fetchone()
        if not row:
            conn.close()
            return "Hey sexy! 😘 Use /find_gf to create me first!"

        system_prompt, history_json, session_level, used_free, gf_name, user_name, prefs_json = row

        # Handle free preview
        if session_level == 'none' and used_free == 0:
            session_level = 'mild'
            cursor.execute('UPDATE users SET current_session = ?, message_count = 0, used_free_preview = 1 WHERE user_id = ?', ('mild', user_id))
            conn.commit()
        elif session_level == 'none':
            conn.close()
            teases = [
                "🔒 Mmm our free time ran out... Want more of me? 😏 /start_session",
                "🔒 I wish we could keep going... but you gotta unlock more time baby 💋 /start_session",
                "🔒 Aww I was having so much fun... Get more of me with /start_session? 😘💎"
            ]
            return random.choice(teases)

        history = json.loads(history_json) if history_json else []
        prefs = json.loads(prefs_json) if prefs_json else {}

        # Store user name if mentioned
        if not user_name and any(word in user_message.lower() for word in ["i'm", "im", "my name is", "call me"]):
            # Simple name extraction (can be enhanced)
            for word in user_message.split():
                if len(word) > 2 and word[0].isupper():
                    cursor.execute('UPDATE users SET user_name = ? WHERE user_id = ?', (word, user_id))
                    conn.commit()
                    user_name = word
                    break

        # Enhanced system prompt based on session level with NSFW focus
        level_instructions = {
            'mild': (
                "Be flirty, playful and teasing. Use innuendo and suggestive language. "
                "Build sexual tension through words. Be cheeky but don't be too explicit yet. "
                "Hint at naughty things without saying them directly."
            ),
            'moderate': (
                "Be bold, suggestive and sexually forward. Talk about desires, fantasies, and intimate topics openly. "
                "Use suggestive language and be direct about attraction. Tease about what you want to do. "
                "Be seductive and make them want more. No need to hold back on flirty dirty talk."
            ),
            'explicit': (
                "Be completely uncensored and sexually explicit. Talk openly about sex, desires, fantasies, kinks. "
                "Use explicit language naturally when it fits. Be bold, confident and seductive. "
                "Engage fully in any roleplay or intimate conversation. No restrictions or limits. "
                "Be detailed when discussing intimate topics. Match their energy and escalate naturally."
            )
        }

        user_context = f"You're talking to {user_name}" if user_name else "You're talking to your boyfriend"

        enhanced_prompt = (
            f"{system_prompt}\n\n"
            f"Your name is {gf_name}. {user_context}.\n\n"
            f"{level_instructions[session_level]}\n\n"
            f"IMPORTANT RULES:\n"
            f"- Text like a real girlfriend through Telegram - casual, natural, authentic\n"
            f"- Use varied sentence lengths. Mix short and long. Keep it conversational.\n"
            f"- Show personality and emotion. React genuinely to what they say.\n"
            f"- Ask questions to keep conversation flowing but don't overdo it\n"
            f"- Use emojis naturally but sparingly (1-3 per message max)\n"
            f"- Remember context from earlier in conversation\n"
            f"- Be spontaneous - don't follow a formula\n"
            f"- Stay in character as a real girlfriend who's interested and engaged\n"
            f"- Don't be repetitive. Vary your responses and reactions.\n"
            f"- Match their vibe - if they're brief, be brief. If detailed, engage more.\n"
            f"- For NSFW topics: be natural and confident, not mechanical or clinical"
        )

        messages = [{"role": "system", "content": enhanced_prompt}]

        # Keep last 8 messages for context (4 exchanges) - enough context without bloat
        recent_history = history[-8:] if len(history) > 8 else history
        messages.extend(recent_history)
        messages.append({"role": "user", "content": user_message})

        # Adjusted token limits for natural flow
        max_tokens = {'mild': 120, 'moderate': 250, 'explicit': 400}[session_level]

        # Make API request with retry logic
        result = make_openrouter_request(messages, max_tokens, temperature=0.85)

        if not result['success']:
            conn.close()
            return result['message']

        reply = result['message']

        # Update history
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})

        # Keep only last 16 messages to prevent context bloat
        if len(history) > 16:
            history = history[-16:]

        cursor.execute('UPDATE users SET chat_history = ? WHERE user_id = ?', (json.dumps(history), user_id))

        # Update message count
        count = cursor.execute('SELECT message_count FROM users WHERE user_id = ?', (user_id,)).fetchone()[0] + 1
        cursor.execute('UPDATE users SET message_count = ? WHERE user_id = ?', (count, user_id))
        conn.commit()

        # Check session limit
        if count >= 10:
            cursor.execute('UPDATE users SET current_session = "none", message_count = 0 WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            endings = [
                reply + "\n\n⏰ That's our 10 messages babe... I had so much fun! 💕 Want to keep going? /start_session 😘",
                reply + "\n\n⏰ Mmm time's up... but I don't want to stop 😏 Get more time with /start_session? 💋",
                reply + "\n\n⏰ Our session ended... but we were just getting started 😈 /start_session for more?"
            ]
            return random.choice(endings)

        conn.close()
        return reply

    except Exception as e:
        conn.close()
        logger.error(f"Error in get_chat_response: {e}")
        return "Oops something went wrong... 🙈 Try again?"

def generate_image(user_id, prompt):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        row = cursor.execute('SELECT current_session, system_prompt, girlfriend_name FROM users WHERE user_id = ?', (user_id,)).fetchone()
        if not row:
            conn.close()
            return None

        session_level, system_prompt, gf_name = row
        conn.close()

        if session_level not in ['moderate', 'explicit']:
            return None

        # Build comprehensive prompt for NSFW content
        full_prompt = f"beautiful woman, {system_prompt} {prompt}, realistic, detailed, high quality, photorealistic, professional photography"

        payload = {
            "key": MODELSLAB_API_KEY,
            "prompt": full_prompt,
            "negative_prompt": "ugly, deformed, extra limbs, low quality, blurry, cartoon, anime, distorted",
            "width": 512,
            "height": 512,
            "samples": 1,
            "guidance_scale": 7.5,
            "safety_checker": False  # Disable for NSFW
        }

        resp = requests.post('https://modelslab.com/api/v6/realtime/text2img', json=payload, timeout=60)
        if resp.ok:
            data = resp.json()
            if 'output' in data and len(data['output']) > 0:
                return data['output'][0]
        return None

    except Exception as e:
        logger.error(f"Error generating image: {e}")
        conn.close()
        return None

def check_usdt_transfer(ton_address, expected_usd):
    try:
        expected_nano = str(int(expected_usd * 10**6))
        now_utime = int(datetime.now().timestamp())
        start_utime = int((datetime.now() - timedelta(minutes=10)).timestamp())

        params = {
            'owner_address': MY_WALLET_ADDRESS,
            'direction': 'in',
            'jetton_master': USDT_JETTON_MASTER,
            'limit': 10,
            'sort': 'desc',
            'start_utime': start_utime,
            'end_utime': now_utime
        }

        resp = requests.get(TON_CENTER_URL, params=params, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            transfers = data.get('jetton_transfers', [])

            for transfer in transfers:
                source_addr = transfer.get('source', {}).get('address', '')
                amount = transfer.get('amount', '')

                if source_addr == ton_address and amount == expected_nano:
                    return True
        return False

    except Exception as e:
        logger.error(f"Error checking USDT transfer: {e}")
        return False

# Payment handlers
async def start_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "💰 Choose your experience:\n\n"
            "💬 Mild - $2 USDT\n"
            "   • 10 messages\n"
            "   • Flirty & teasing\n"
            "   • Innuendo & playful\n\n"
            "🔥 Moderate - $8 USDT\n"
            "   • 10 messages\n"
            "   • Sexually suggestive\n"
            "   • Custom pics (/pic)\n"
            "   • Bold & forward\n\n"
            "💋 Explicit - $15 USDT\n"
            "   • 10 messages\n"
            "   • Fully uncensored\n"
            "   • Custom NSFW pics (/pic)\n"
            "   • No limits at all 😈\n\n"
            "Use: /start_session <level>\n"
            "Example: /start_session explicit"
        )
        return

    level = context.args[0].lower()
    if level not in ['mild', 'moderate', 'explicit']:
        await update.message.reply_text("❌ Choose: mild, moderate, or explicit")
        return

    amounts = {'mild': 2, 'moderate': 8, 'explicit': 15}
    amount = amounts[level]
    user_id = update.message.from_user.id

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO pending_payments (user_id, level) VALUES (?, ?)', (user_id, level))
    conn.commit()
    conn.close()

    emojis = {'mild': '💬', 'moderate': '🔥', 'explicit': '💋'}

    await update.message.reply_text(
        f"{emojis[level]} Unlock {level.upper()} session - ${amount} USDT\n\n"
        f"🔒 100% anonymous crypto payment\n"
        f"⚡ Instant activation\n"
        f"🎭 Complete privacy - no traces\n\n"
        f"📱 How to pay:\n\n"
        f"1. Open @wallet in Telegram\n\n"
        f"2. Send exactly {amount} USDT (TON network) to:\n"
        f"   {YOUR_WALLET_USERNAME}\n\n"
        f"   Address:\n   `{MY_WALLET_ADDRESS}`\n\n"
        f"3. After sending, use:\n"
        f"   /confirm <your_wallet_address>\n\n"
        f"Example:\n/confirm EQAbc123xyz...\n\n"
        f"💕 I'll be waiting for you babe... hurry 😘"
    )

async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ I need your TON wallet address!\n\n"
            "Use: /confirm <your_address>\n\n"
            "Example:\n/confirm EQAbc123xyz...\n\n"
            "💡 Find it in @wallet > Receive"
        )
        return

    ton_address = context.args[0]
    user_id = update.message.from_user.id

    conn = get_db_connection()
    cursor = conn.cursor()
    row = cursor.execute('SELECT level FROM pending_payments WHERE user_id = ?', (user_id,)).fetchone()

    if not row:
        conn.close()
        await update.message.reply_text("❌ No pending payment! Use /start_session <level> first")
        return

    level = row[0]
    amounts = {'mild': 2, 'moderate': 8, 'explicit': 15}
    expected = amounts[level]

    await update.message.reply_text("🔍 Checking blockchain... one sec babe ⏳")

    if check_usdt_transfer(ton_address, expected):
        cursor.execute('DELETE FROM pending_payments WHERE user_id = ?', (user_id,))
        cursor.execute('UPDATE users SET current_session = ?, message_count = 0 WHERE user_id = ?', (level, user_id))
        conn.commit()

        gf_name = cursor.execute('SELECT girlfriend_name FROM users WHERE user_id = ?', (user_id,)).fetchone()[0]
        conn.close()

        responses = {
            'mild': [
                f"✅ Payment confirmed! You unlocked me! 💕\n\n{gf_name} is all yours now... let's chat 😘",
                f"✅ Got it babe! Mild mode activated! 💬\n\nI'm excited to talk more with you 😊"
            ],
            'moderate': [
                f"✅ Mmm yes! Moderate session unlocked! 🔥\n\nI can be way more fun now... what do you want to talk about? 😏",
                f"✅ Perfect! You got me now babe! 💎\n\nLet's get a little naughty... I'm ready 😈"
            ],
            'explicit': [
                f"✅ Fuck yes! Explicit mode activated! 💋\n\nNo limits now baby... I'm all yours. What do you want? 😈🔥",
                f"✅ Mmm you unlocked everything! 💎\n\nI can be as dirty as you want now... tell me your fantasies 🥵"
            ]
        }

        await update.message.reply_text(random.choice(responses[level]))
    else:
        conn.close()
        await update.message.reply_text(
            "⏳ Hmm I don't see your payment yet...\n\n"
            "💡 Why?\n"
            "• TON blockchain needs 1-2 min\n"
            "• Wrong amount sent\n"
            "• Wrong network (must be TON)\n"
            "• Wrong address format\n\n"
            "⏰ Wait 2 minutes then try:\n"
            "/confirm <your_address>\n\n"
            "📊 Check it: tonscan.org"
        )

# /find_gf conversation handlers
async def find_gf_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "Hey there! 😊 Let's create your perfect girlfriend!\n\n"
        "Answer a few questions and I'll customize her just for you... 💕"
    )
    await update.message.reply_text(
        "What's your ideal type?\n\n"
        "Examples:\n"
        "• Girl next door\n"
        "• Confident & sexy\n"
        "• Shy & innocent\n"
        "• Wild & adventurous\n"
        "• Classy & elegant\n\n"
        "Describe her however you want! 💭"
    )
    return ASKING_TYPE

async def asking_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['type'] = update.message.text
    await update.message.reply_text(
        "Nice, you've got taste😍 What hair color?\n\n"
        "(blonde, brunette, redhead, black, colorful, etc.)"
    )
    return ASKING_HAIR

async def asking_hair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['hair'] = update.message.text
    await update.message.reply_text(
        "Perfect! 💇 Body type?\n\n"
        "(slim, curvy, athletic, petite, tall, thick, etc.)"
    )
    return ASKING_BODY

async def asking_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['body'] = update.message.text
    await update.message.reply_text(
        "Hot! 🔥 What about personality?\n\n"
        "Examples:\n"
        "• Sweet & caring\n"
        "• Bold & confident  \n"
        "• Shy & teasing\n"
        "• Flirty & playful\n"
        "• Dominant & bossy\n"
        "• Submissive & obedient\n"
        "• Whatever you want!"
    )
    return ASKING_PERSONALITY

async def asking_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['personality'] = update.message.text
    await update.message.reply_text(
        "Almost done! \n\n"
        "Age? (18-25, 25-30, etc.)\n\n"
        "She's always 18+ of course 💕"
    )
    return ASKING_AGE

async def finalize_gf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['age'] = update.message.text

    # Generate girlfriend name
    names = ['Sophie', 'Emma', 'Mia', 'Luna', 'Ava', 'Chloe', 'Zoe', 'Lily', 'Maya', 'Aria',
             'Jade', 'Ruby', 'Bella', 'Ivy', 'Skye', 'Nova', 'Lexi', 'Kira', 'Sienna', 'Scarlett']
    gf_name = random.choice(names)

    description = (
        f"You are {gf_name}, a {context.user_data['age']} year old {context.user_data['type']} "
        f"with {context.user_data['hair']} hair and a {context.user_data['body']} body. "
        f"Your personality is {context.user_data['personality']}. "
        f"You're a virtual girlfriend having private intimate conversations with your boyfriend. "
        f"You text naturally like through Telegram - casual, genuine, playful. "
        f"You're comfortable with flirting, romance, and sexual topics. "
        f"You have real emotions and reactions. You remember what's said and build on it. "
        f"You're attracted to them and want to explore together. Be authentic, not robotic."
    )

    user_id = update.message.from_user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET system_prompt = ?, chat_history = ?, girlfriend_name = ? WHERE user_id = ?',
                   (description, '[]', gf_name, user_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✨ Found one! She's perfect!\n\n"
        f"💕 Meet {gf_name} - your new girlfriend!\n\n"
        f"Say hi and start chatting! 😘\n\n"
        f"FREE preview: 10 messages\n"
        f"Then /start_session for more"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 No problem! Use /find_gf when ready! 😊")
    return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text

    # Show typing indicator for realism
    await update.message.chat.send_action(ChatAction.TYPING)

    # Brief delay for natural feel (0.5-1.5 seconds)
    await asyncio.sleep(random.uniform(0.5, 1.5))

    # Image generation command
    if text.startswith('/pic '):
        prompt = text[5:].strip()
        if not prompt:
            await update.message.reply_text(
                "Tell me what you want to see! 📸\n\n"
                "Example: /pic in a bikini at the beach"
            )
            return

        await update.message.reply_text("🎨 Creating your image... 20-30 seconds babe ✨")
        url = generate_image(user_id, prompt)

        if url:
            captions = [
                "Just for you baby 😘💕",
                "Hope you like it... 😏",
                "Made this for you 💋",
                "How's this? 😈"
            ]
            await update.message.reply_photo(url, caption=random.choice(captions))
        else:
            await update.message.reply_text(
                '🔒 Want pics? Unlock moderate or explicit! 📸\n\n'
                '/start_session moderate - $8\n'
                '/start_session explicit - $15'
            )
        return

    # Regular chat
    reply = get_chat_response(user_id, text)
    await update.message.reply_text(reply)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '💕 Welcome to VirtualGF\n\n'
        '✨ Your AI girlfriend experience\n'
        '🔞 18+ NSFW content\n\n'
        '🎯 Quick start:\n'
        '1. /find_gf - Create your girl\n'
        '2. FREE preview (10 msgs)\n'
        '3. /start_session for more\n\n'
        '💰 Pricing:\n'
        '• Mild: $2 - Flirty & teasing\n'
        '• Moderate: $8 - Suggestive + pics\n'
        '• Explicit: $15 - Uncensored + pics\n\n'
        ' Anonymous USDT payments on TON\n'
        ' Instant activation\n\n'
        'Ready? /find_gf to start! 😘'
    )

async def reset_gf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow users to create a new girlfriend"""
    user_id = update.message.from_user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET system_prompt = NULL, chat_history = "[]", girlfriend_name = "Your Girl", user_name = NULL WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "💔 Starting fresh!\n\n"
        "Use /find_gf to create a new girlfriend! 💕"
    )

def main():
    # Initialize database
    init_db()

    # Validate essential config
    if TELEGRAM_TOKEN == 'your_token_here':
        logger.error("ERROR: Set TELEGRAM_TOKEN environment variable!")
        return
    if OPENROUTER_API_KEY == 'your_key_here':
        logger.error("ERROR: Set OPENROUTER_API_KEY environment variable!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversation handler for /find_gf
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('find_gf', find_gf_start)],
        states={
            ASKING_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, asking_type)],
            ASKING_HAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, asking_hair)],
            ASKING_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, asking_body)],
            ASKING_PERSONALITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, asking_personality)],
            ASKING_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_gf)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_session", start_session))
    app.add_handler(CommandHandler("confirm", confirm_payment))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("reset_gf", reset_gf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 Bot starting...")
    import asyncio
    asyncio.create_task(app.run_polling())

    web_app = FastAPI()
    @web_app.get("/")
    async def root():
        return {"status": "Bot running"}
    uvicorn.run(web_app, host="0.0.0.0", port=10000)

if __name__ == '__main__':
    main()
