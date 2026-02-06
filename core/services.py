# core/services.py
import logging
from .models import Notificacion


def enviar_alerta_whatsapp(mensaje):
    """
    Simulación de envío vía API (Twilio, UltraMsg, etc.)
    En un entorno real, aquí se llamaría a un servicio externo.
    """
    print(f"--- ALERTA WHATSAPP ENVIADA: {mensaje} ---")
    # Log para auditoría
    logging.info(f"WhatsApp enviado: {mensaje}")


def crear_notificacion_interna(empresa, mensaje, tipo):
    """Guarda la alerta para que aparezca en la campanita y dashboard"""
    Notificacion.objects.create(
        empresa=empresa,
        mensaje=mensaje,
        tipo=tipo
    )

def verificar_variacion_precio(producto, nuevo_precio):
    precio_anterior = producto.precio_compra_referencial
    if precio_anterior > 0:
        variacion = ((nuevo_precio - precio_anterior) / precio_anterior) * 100
        if variacion > 5:
            mensaje = f"¡Atención! El producto {producto.nombre_interno} subió un {variacion:.2f}%"
            # 1. WhatsApp (Lo que ya teníamos)
            enviar_alerta_whatsapp(f"⚠️ {mensaje}")
            # 2. Interna (RF-22)
            crear_notificacion_interna(producto.empresa, mensaje, 'PRECIO')
