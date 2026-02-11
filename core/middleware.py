# core/middleware.py
import threading
from django.shortcuts import redirect
from django.urls import reverse

# --- LÓGICA PARA EL MONITOR GLOBAL ---
_thread_locals = threading.local()

def get_current_user():
    return getattr(_thread_locals, 'user', None)

class AuditoriaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.user = request.user
        response = self.get_response(request)
        return response

# --- TU LÓGICA DE SELECCIÓN DE EMPRESA (MANTENIDA) ---
class EmpresaContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        exempt_urls = [
            reverse('admin:index'),
            reverse('seleccionar_empresa'),
            '/login/',
            '/logout/',
            '/static/',
        ]

        if request.user.is_authenticated:
            if not request.session.get('empresa_id') and request.path not in exempt_urls:
                if not any(request.path.startswith(url) for url in exempt_urls):
                    return redirect('seleccionar_empresa')

        response = self.get_response(request)
        return response