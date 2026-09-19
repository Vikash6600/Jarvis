"""
Telegram remote — settings (token + pairing) and a tool to message the user's phone.
The listening side lives in core/telegram_bridge.py and is started by main.py.
"""
from core import telegram_bridge

PLUGIN = {
    "name": "telegram_send",
    "description": (
        "ALWAYS use this when the user wants to be messaged themself: 'ping me', 'ping me on Telegram', "
        "'message me', 'text me', 'notify me', 'remind me on my phone', 'send that to my phone / to "
        "Telegram'. It sends instantly through the user's own paired Jarvis Telegram bot — do NOT open "
        "the Telegram app or a browser for this. Only for messaging OTHER people use send_message."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"text": {"type": "STRING", "description": "The message to send"}},
        "required": ["text"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "telegram",
    "title": "Telegram remote",
    "fields": [
        {"key": "token", "label": "Bot token (from @BotFather)", "type": "password",
         "placeholder": "123456789:AA…"},
    ],
    "action": {"label": "CONNECT & GET PAIRING CODE",
               "run": lambda values: telegram_bridge.connect(values.get("token", ""))},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    text = str(parameters.get("text") or "").strip()
    if not text:
        return "There was nothing to send, sir."
    if not telegram_bridge._cfg().get("chat_id"):
        return "Telegram isn't paired yet — open Plugin Settings, add the bot token and send the pairing code."
    ok = telegram_bridge.TelegramBridge().send(text)
    return "Sent to your phone." if ok else "I couldn't send that to Telegram."
