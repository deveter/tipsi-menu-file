import os
import tempfile
import json
import base64
import logging
import re
import time
import fitz  # PyMuPDF
import mimetypes
from docx import Document
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser,JSONParser
from rest_framework.response import Response
import openai
import pandas as pd
from api.email import enviar_email_brevo
from django.http import HttpResponse, Http404
from django.views.generic import View
from django.conf import settings

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
logger = logging.getLogger(__name__)

PROMPT = """
Extrae todos los productos de esta carta de restaurante con su familia/Sección, precio y formato

Formato del JSON:
[
  {
    "familia": "Nombre de la sección",
    "producto": "Nombre del plato",
    "precio": número sin símbolo €,
    "formato": "tapa", "ración", etc. Si no se indica, pon "Único"
  }
]

No incluyas ningún comentario ni explicación. Solo el JSON.
"""

# 📄 Extraer texto de Word
def extract_text_from_docx(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

# 📄 Extraer texto de PDF
def extract_text_from_pdf(file):
    text = ""
    pdf = fitz.open(stream=file.read(), filetype="pdf")
    for page in pdf:
        text += page.get_text()
    return text

# 🖼️ Procesar una imagen individual con OpenAI
def procesar_imagen_con_openai(image_file):
    try:
        image = Image.open(image_file)
        max_width = 1280
        if image.width > max_width:
            ratio = max_width / image.width
            new_size = (max_width, int(image.height * ratio))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        buffer = BytesIO()
        image.save(buffer, format='JPEG', optimize=True, quality=85)
        buffer.seek(0)
        encoded = base64.b64encode(buffer.read()).decode('utf-8')

        image_msg = [{
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{encoded}"
            }
        }]

        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Eres un asistente que analiza cartas de restaurante."},
                {"role": "user", "content": [{"type": "text", "text": PROMPT}] + image_msg}
            ],
            max_tokens=2000,
            temperature=0.2
        )

        content = response.choices[0].message.content
        match = re.search(r"\[\s*{.*?}\s*]", content, re.DOTALL)
        return json.loads(match.group(0)) if match else []

    except Exception as e:
        logger.exception("❌ Error al procesar imagen:")
        return []

# 🧠 Procesar texto con OpenAI (desde PDF o DOCX)
def procesar_texto_con_openai(texto):
    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Eres un asistente que analiza cartas de restaurante."},
                {"role": "user", "content": f"{PROMPT}\n\n{texto}"}
            ],
            max_tokens=2000,
            temperature=0.2
        )
        content = response.choices[0].message.content
        match = re.search(r"\[\s*{.*?}\s*]", content, re.DOTALL)
        return json.loads(match.group(0)) if match else []
    except Exception as e:
        logger.exception("❌ Error al procesar texto:")
        return []

class TranscribeView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request):
        start = time.time()
        files = request.FILES.getlist('images')

        if not files:
            return Response({"error": "No se recibió ningún archivo"}, status=400)
        if len(files) > 10:
            return Response({"error": "Máximo 10 archivos permitidos."}, status=400)

        # 🔍 Detectar tipos de archivo
        ext_set = set([mimetypes.guess_extension(f.content_type) for f in files])
        is_all_images = all(f.content_type.startswith('image/') for f in files)
        is_all_pdf = all(f.name.lower().endswith('.pdf') for f in files)
        is_all_docx = all(f.name.lower().endswith('.docx') for f in files)

        if not (is_all_images or is_all_pdf or is_all_docx):
            return Response({
                "error": "No se pueden mezclar imágenes con documentos. Sube solo imágenes, o solo PDFs, o solo Word."
            }, status=400)

        # 🖼️ Procesar imágenes
        if is_all_images:
            all_results = []
            with ThreadPoolExecutor(max_workers=min(4, len(files))) as executor:
                futures = {executor.submit(procesar_imagen_con_openai, f): f.name for f in files}
                for future in as_completed(futures):
                    result = future.result()
                    if isinstance(result, list):
                        all_results.extend(result)
            print(f"✅ Procesamiento de imágenes: {time.time() - start:.2f} s")
            return Response({"structured": all_results})

        # 📄 Procesar PDFs o DOCX
        texto_completo = ""
        for f in files:
            if is_all_pdf:
                texto_completo += extract_text_from_pdf(f)
            elif is_all_docx:
                texto_completo += extract_text_from_docx(f)
            texto_completo += "\n"

        resultado = procesar_texto_con_openai(texto_completo)
        print(f"✅ Procesamiento de texto: {time.time() - start:.2f} s")
        return Response({"structured": resultado})


    
class EnviarCartaView(APIView):
    parser_classes = [JSONParser]

    def post(self, request):
        nombre = request.data.get("nombre_restaurante")
        email = request.data.get("email")
        carta = request.data.get("carta")

        if not nombre or not email or not carta:
            return Response({"error": "Faltan datos"}, status=400)

        try:
            df = pd.DataFrame(carta)
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                excel_path = tmp.name
                df.to_excel(excel_path, index=False)

            asunto = f"📋 Nueva carta enviada por {nombre}"
            cuerpo = f"El restaurante '{nombre}' con email '{email}' ha enviado su carta adjunta en Excel."

            enviar_email_brevo(
                destinatario="ppinar@tipsitpv.com",
                asunto=asunto,
                cuerpo=cuerpo,
                adjunto=excel_path
            )

            return Response({"message": "Carta enviada correctamente"})

        except Exception as e:
            logger.exception("❌ Error al enviar el email:")
            return Response({"error": str(e)}, status=500)


class FrontendAppView(View):
    def get(self, request):
        index_path = os.path.join(settings.BASE_DIR, 'staticfiles', 'index.html')
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                return HttpResponse(f.read())
        else:
            raise Http404("index.html no encontrado en STATIC_ROOT")
