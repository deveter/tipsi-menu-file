import os
import requests
import base64

def enviar_email_brevo(destinatario, asunto, cuerpo, adjunto=None):
    """
    Envía un email a través de la API de Brevo (Sendinblue).
    """
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL")

    if not BREVO_API_KEY:
        raise Exception("BREVO_API_KEY no está definido en el entorno")

    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }

    data = {
        "sender": {
            "name": "Tipsi Voice",
            "email": DEFAULT_FROM_EMAIL
        },
        "to": [{"email": destinatario}],
        "subject": asunto,
        "textContent": cuerpo,
    }

    if adjunto:
        with open(adjunto, "rb") as f:
            contenido_base64 = base64.b64encode(f.read()).decode("utf-8")
            data["attachment"] = [{
                "name": os.path.basename(adjunto),
                "content": contenido_base64
            }]

    response = requests.post("https://api.brevo.com/v3/smtp/email", json=data, headers=headers)

    if response.status_code >= 400:
        raise Exception(f"Error al enviar el email: {response.status_code} - {response.text}")

    return response.json()
