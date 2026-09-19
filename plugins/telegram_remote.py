"""
Telegram remote — settings (token + pairing) and a tool to message the user's phone.
The listening side lives in core/telegram_bridge.py and is started by main.py.
"""
from core import telegram_bridge

PLUGIN = {
    "name": "telegram_send",
    "description": (
        "Send a text message to the user's own phone via their paired Telegram bot "
        "(e.g. 'send that to my phone', 'text me the link'). Not for messaging other people — "
        "use send_message for that."
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
