# core/decorators.py
from django.core.exceptions import PermissionDenied

def admin_required(view_func):
    def _wrapped_view_func(request, *args, **kwargs):
        # Verificamos si el rol del usuario es Admin
        if request.user.is_authenticated and request.user.rol and request.user.rol.nombre == 'Admin':
            return view_func(request, *args, **kwargs)
        else:
            raise PermissionDenied # Lanza un error 403
    return _wrapped_view_func