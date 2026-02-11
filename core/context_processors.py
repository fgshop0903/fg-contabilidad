# core/context_processors.py
from .models import Notificacion

def global_context(request):
    # Valores por defecto
    context = {
        'n_count': 0,
        'n_alertas': [],
        'user_permisos': {}
    }

    if request.user.is_authenticated:
        # 1. Notificaciones
        emp_id = request.session.get('empresa_id')
        if emp_id:
            context['n_count'] = Notificacion.objects.filter(empresa_id=emp_id, leida=False).count()
            context['n_alertas'] = Notificacion.objects.filter(empresa_id=emp_id, leida=False)[:5]
        
        # 2. Permisos del Rol (Para ocultar botones)
        if hasattr(request.user, 'rol') and request.user.rol:
            context['user_permisos'] = request.user.rol.permisos
            
    return context