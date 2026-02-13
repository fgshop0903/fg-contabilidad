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
from django.db.models.signals import pre_delete # Usamos pre_delete para actuar ANTES de que se borre

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


@receiver(post_save)
def monitor_guardado_global(sender, instance, created, **kwargs):
    if sender in MODELOS_A_VIGILAR:
        user = get_current_user()
        if user and user.is_authenticated:
            if sender == LogAuditoria: return
            accion = 'INSERT' if created else 'UPDATE'
            empresa = getattr(instance, 'empresa', None)
            resumen = generar_resumen_humano(instance, sender.__name__, accion)
            if not created: resumen = f"[EDICIÓN] {resumen}"

            LogAuditoria.objects.create(
                usuario=user, empresa=empresa, accion=accion,
                tabla_afectada=sender.__name__, referencia_id=instance.id,
                motivo_cambio=resumen
            )

# --- 3. SENSOR DE REVERSIÓN DE STOCK (PRE_DELETE COMPROBANTE) ---
@receiver(pre_delete, sender=Comprobante)
def revertir_impacto_total_documento(sender, instance, **kwargs):
    """ RF-24: Antes de borrar la factura, devolvemos el stock al almacén """
    for det in instance.detalles.all():
        if det.producto:
            if instance.operacion == 'Compra':
                det.producto.stock_actual -= det.cantidad # Quitamos lo que compramos
            else: 
                det.producto.stock_actual += det.cantidad # Devolvemos lo que vendimos
            det.producto.save()

    # Al borrar el comprobante, buscamos sus pagos y los borramos también
    # Esto disparará el sensor #4 (abajo) para limpiar el banco.
    MovimientoFinanciero.objects.filter(comprobante=instance).delete()

# --- 4. SENSOR DE REVERSIÓN BANCARIA (POST_DELETE MOVIMIENTO) ---
# ESTE ES EL QUE TE FALTABA PARA QUE EL BCP QUEDE EN CERO
@receiver(post_delete, sender=MovimientoFinanciero)
def revertir_saldo_bancario_al_borrar(sender, instance, **kwargs):
    """ Si borras un pago, el banco debe recuperar o restar ese dinero """
    monto = decimal.Decimal(str(instance.monto))
    itf = decimal.Decimal(str(instance.itf_monto))

    # Reversión en Cuenta Bancaria
    if instance.cuenta_bancaria:
        obj = instance.cuenta_bancaria
        if instance.tipo == 'Ingreso':
            obj.saldo_actual -= (monto - itf) # Si borro un cobro, resto del banco
        else:
            obj.saldo_actual += (monto + itf) # Si borro un pago, devuelvo al banco
        obj.save()
    
    # Reversión en Caja Efectivo
    elif instance.caja:
        obj = instance.caja
        if instance.tipo == 'Ingreso':
            obj.saldo_actual -= monto
        else:
            obj.saldo_actual += monto
        obj.save()