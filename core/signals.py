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

# 2. FUNCIÓN DE RESUMEN (DEBE IR ARRIBA)
def generar_resumen_humano(instance, sender_name, accion):
    """
    Traduce los datos técnicos a mensajes simples para el Log.
    """
    try:
        if sender_name == 'Cotizacion':
            return f"Cotización {instance.numero} para {instance.nombre_cliente} por {instance.moneda} {instance.total}"
        
        elif sender_name == 'Comprobante':
            return f"{instance.operacion} {instance.codigo_factura} de {instance.entidad.nombre_razon_social} por {instance.moneda} {instance.total}"
        
        elif sender_name == 'MovimientoFinanciero':
            return f"{instance.tipo} de {instance.moneda} {instance.monto}: {instance.referencia}"
        
        elif sender_name == 'Producto':
            return f"Stock de {instance.nombre_interno} ({instance.sku}) actualizado a {instance.stock_actual}"
        
        elif sender_name == 'Prestamo':
            return f"Préstamo de {instance.prestamista} por {instance.moneda} {instance.monto_capital}"
        
        elif sender_name == 'CuentaEstado':
            return f"Deuda de {instance.comprobante.codigo_factura} actualizada. Pendiente: S/ {instance.saldo_pendiente}"
        
        elif sender_name == 'CertificadoRetencion':
            return f"Conciliación de Retención {instance.serie_numero} por S/ {instance.monto_total_pen}"
            
    except Exception as e:
        return f"Acción de {accion} en {sender_name} (Error al generar detalle: {str(e)})"
    
    return f"{accion} en {sender_name}"


# 3. SENSOR DE GUARDADO (POST_SAVE)
@receiver(post_save)
def monitor_guardado_global(sender, instance, created, **kwargs):
    if sender in MODELOS_A_VIGILAR:
        user = get_current_user()
        if user and user.is_authenticated:
            if sender == LogAuditoria: return
            
            accion = 'INSERT' if created else 'UPDATE'
            
            # Buscamos la empresa vinculada
            empresa = getattr(instance, 'empresa', None)
            if not empresa and hasattr(instance, 'comprobante'):
                empresa = instance.comprobante.empresa

            # Llamamos a la función que estaba dando error (ahora ya está definida arriba)
            resumen = generar_resumen_humano(instance, sender.__name__, accion)
            
            if not created: 
                resumen = f"[EDICIÓN] {resumen}"

            LogAuditoria.objects.create(
                usuario=user,
                empresa=empresa,
                accion=accion,
                tabla_afectada=sender.__name__,
                referencia_id=instance.id,
                motivo_cambio=resumen
            )

# 4. SENSOR DE BORRADO (POST_DELETE)
@receiver(post_delete)
def monitor_borrado_global(sender, instance, **kwargs):
    if sender in MODELOS_A_VIGILAR:
        user = get_current_user()
        if user and user.is_authenticated:
            empresa = getattr(instance, 'empresa', None)
            if not empresa and hasattr(instance, 'comprobante'):
                empresa = instance.comprobante.empresa

            LogAuditoria.objects.create(
                usuario=user,
                empresa=empresa,
                accion='DELETE',
                tabla_afectada=sender.__name__,
                referencia_id=instance.id,
                motivo_cambio=f"ELIMINACIÓN: Se borró registro de {sender.__name__} ID {instance.id}"
            )