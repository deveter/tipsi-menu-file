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
Extrae todos los productos de esta carta de restaurante con su familia/Sección, precio y formato. No incluyas ningún comentario ni explicación. Fíjate bien en los precios de cada uno de ellos y el formato al que corresponde. Si un producto sólo tiene un precio, el formato es ÚNICO. Énvíame sólo el JSON con este formato:

[
  {
    "familia": "Nombre de la sección",
    "producto": "Nombre del plato",
    "precio": número sin €,
    "formato": "tapa", "ración", etc. Si no se indica, pon "Único"
  }
]
"""

def recortar_bordes_si_hay(imagen: Image.Image) -> Image.Image:
    # Crea una imagen blanca del mismo tamaño
    bg = Image.new(imagen.mode, imagen.size, imagen.getpixel((0, 0)))
    diff = ImageChops.difference(imagen, bg)
    bbox = diff.getbbox()
    if bbox:
        return imagen.crop(bbox)
    return imagen


def extract_json_array(texto):
    try:
        texto = texto.strip()
        if texto.startswith("```json"):
            texto = texto.removeprefix("```json").strip()
        if texto.endswith("```"):
            texto = texto.removesuffix("```").strip()

        parsed = json.loads(texto)
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        print("❌ Error en json.loads:", e)
        return []

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
        print(text)
    return text

# 🖼️ Procesar una imagen individual con OpenAI
def procesar_imagen_con_openai(image_file):
    try:
        image = Image.open(image_file)

        if image.mode == "RGBA":
            image = image.convert("RGB")


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
            max_tokens=8192,
            temperature=0.2
        )

        content = response.choices[0].message.content
        print("📸 Contenido OpenAI (imagen):\n", content)        
        return extract_json_array(content)

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
            max_tokens=8000,
            temperature=0.2
        )

        content = response.choices[0].message.content
        print("📥 Contenido OpenAI:\n", content)

        resultado = extract_json_array(content)
        print("🔍 Resultado JSON parseado:", resultado)

        return resultado

    except Exception as e:
        logger.exception("❌ Error al procesar texto con OpenAI:")
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
            with ThreadPoolExecutor(max_workers=min(9, len(files))) as executor:
                future_to_index = {
                    executor.submit(procesar_imagen_con_openai, f): i
                    for i, f in enumerate(files)
                }

                results_by_index = {}

                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        result = future.result()
                        results_by_index[index] = result if isinstance(result, list) else []
                    except Exception as e:
                        logger.exception(f"❌ Error procesando imagen {index}:")
                        results_by_index[index] = []

                for i in sorted(results_by_index.keys()):
                    all_results.extend(results_by_index[i])
          
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
        archivos = request.data.get("archivos_extra", [])

        if not nombre or not carta:
            return Response({"error": "Faltan datos"}, status=400)

        try:
            df = pd.DataFrame(carta)
            print("Columnas recibidas:", df.columns.tolist())

            # Renombrar 'producto' a 'articulo'
            df = df.rename(columns={'producto': 'articulo'})

            # Limpieza opcional (espacios/minúsculas)
            df.columns = (
                df.columns
                  .astype(str)
                  .str.strip()
                  .str.lower()
            )

            # Reordenar las columnas
            nuevo_orden = ["articulo", "formato", "precio", "familia"]
            df = df[nuevo_orden]

            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                excel_path = tmp.name
                df.to_excel(excel_path, index=False)

            adjuntos = [{"name": f"Carta - {nombre}.xlsx", "path": excel_path}]

            for archivo in archivos:
                n_archivo = archivo.get("name")
                contenido = archivo.get("content")
                if n_archivo and contenido:
                    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                        tmp_file.write(base64.b64decode(contenido))
                    adjuntos.append({"name": n_archivo, "path": tmp_file.name})

            asunto = f"📋 Nueva carta enviada por {nombre}"
            cuerpo = (
                f"El restaurante '{nombre}' ha enviado su carta adjunta en Excel.\n"
                f"Email de contacto: {email or '(no proporcionado)'}"
            )

            enviar_email_brevo(
                destinatario="customer@tipsitpv.com",
                asunto=asunto,
                cuerpo=cuerpo,
                adjuntos=adjuntos
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
