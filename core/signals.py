# core/signals.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.forms.models import model_to_dict
import json
import decimal
from .models import (
    LogAuditoria, Comprobante, MovimientoFinanciero, 
    Producto, Entidad, Prestamo, CertificadoRetencion, CuentaEstado,
    Cotizacion
)
from .middleware import get_current_user

# Lista de lo que vamos a vigilar
MODELOS_A_VIGILAR = [
    Comprobante, MovimientoFinanciero, Producto, 
    Entidad, Prestamo, CertificadoRetencion, CuentaEstado,
    Cotizacion
]



# Serializador simple para JSON (maneja fechas y decimales)
def custom_serializer(obj):
    if isinstance(obj, (decimal.Decimal,)):
        return float(obj)
    return str(obj)

@receiver(post_save)
def monitor_guardado_global(sender, instance, created, **kwargs):
    if sender in MODELOS_A_VIGILAR:
        user = get_current_user()
        # Solo registramos si hay un usuario logueado y no es el propio Log
        if user and user.is_authenticated:
            accion = 'INSERT' if created else 'UPDATE'
            
            # Buscamos la empresa vinculada al objeto
            empresa = getattr(instance, 'empresa', None)
            
            LogAuditoria.objects.create(
                usuario=user,
                empresa=empresa,
                accion=accion,
                tabla_afectada=sender.__name__,
                referencia_id=instance.id,
                motivo_cambio=f"Auto-Log: {accion}. Datos: {json.dumps(model_to_dict(instance), default=custom_serializer)}"
            )

@receiver(post_delete)
def monitor_borrado_global(sender, instance, **kwargs):
    if sender in MODELOS_A_VIGILAR:
        user = get_current_user()
        if user and user.is_authenticated:
            LogAuditoria.objects.create(
                usuario=user,
                empresa=getattr(instance, 'empresa', None),
                accion='DELETE',
                tabla_afectada=sender.__name__,
                referencia_id=instance.id,
                motivo_cambio=f"Auto-Log: ELIMINACIÓN de {sender.__name__} ID {instance.id}"
            )