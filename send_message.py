import requests

# Βάλε εδώ το bot token από το BotFather
BOT_TOKEN = "ΒΑΛΕ_ΤΟ_TOKEN_ΣΟΥ_ΕΔΩ"

def send_message(chat_id: int, text: str):
    """
    Στέλνει μήνυμα μέσω Telegram Bot API
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    response = requests.post(url, data=payload)
    return response.json()

if __name__ == "__main__":
    # Παράδειγμα χρήσης
    chat_id = 1570161351   # βάλε εδώ το user_id που θες να στείλεις
    text = "Γεια σου! Αυτό είναι δοκιμαστικό μήνυμα από το bot μου."
    result = send_message(chat_id, text)
    print(result)
