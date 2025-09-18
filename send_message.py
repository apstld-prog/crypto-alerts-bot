import requests

# Βάλε εδώ το bot token από το BotFather
BOT_TOKEN = "8290365345:AAGUwy0cFbSt3FLI2AmOjzaF21gvqJvw5Jc"

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
    text = "Hi. I would like to share your comment about my bot. Please help me to make it better. thanks for your time"
    result = send_message(chat_id, text)
    print(result)
