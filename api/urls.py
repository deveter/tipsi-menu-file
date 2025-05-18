from django.urls import path
from .views import TranscribeView, EnviarCartaView

urlpatterns = [
    path('transcribe/', TranscribeView.as_view()),
    path('enviar-carta/', EnviarCartaView.as_view()),
]
