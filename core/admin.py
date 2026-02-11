from django.contrib import admin
from .models import (
    Empresa, Rol, Usuario, Entidad, CategoriaProducto, Producto,
    Comprobante, ComprobanteDetalle, Prestamo, CategoriaGasto,
    GastoOperativo, CuentaEstado, Cuota, TipoCambioDia, 
    LogAuditoria, Notificacion, MovimientoFinanciero,
    CertificadoRetencion, RetencionDetalle, Caja, Cuenta_Bancaria
)
from django.contrib.auth.admin import UserAdmin 
from .models import Cotizacion, CotizacionDetalle

# --- 1. CONFIGURACIÓN DE INLINES (Vistas anidadas) ---

class ComprobanteDetalleInline(admin.TabularInline):
    model = ComprobanteDetalle
    extra = 0

class CuotaInline(admin.TabularInline):
    model = Cuota
    extra = 0

class RetencionDetalleInline(admin.TabularInline):
    model = RetencionDetalle
    extra = 0

# --- 2. REGISTRO DE MÓDULOS ---

# --- Módulo de Estructura y Acceso ---
admin.site.register(Empresa)
admin.site.register(Rol)
@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Información FG Network', {'fields': ('rol', 'empresa_actual', 'empresas_permitidas')}),
    )
    list_display = ('username', 'email', 'first_name', 'last_name', 'rol', 'is_staff')


admin.site.register(Caja)
admin.site.register(Cuenta_Bancaria)

@admin.register(MovimientoFinanciero)
class MovimientoFinancieroAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'tipo', 'moneda', 'monto', 'referencia', 'caja', 'cuenta_bancaria')
    list_filter = ('tipo', 'moneda', 'empresa')
    search_fields = ('referencia',)

# --- Módulo Comercial (SUNAT y Entidades) ---
@admin.register(Comprobante)
class ComprobanteAdmin(admin.ModelAdmin):
    list_display = ('fecha_emision', 'codigo_factura', 'entidad', 'operacion', 'moneda', 'total', 'estado_sunat')
    list_filter = ('operacion', 'estado_sunat', 'moneda', 'empresa')
    search_fields = ('serie', 'numero', 'entidad__nombre_razon_social')
    inlines = [ComprobanteDetalleInline]

    # --- ESTO ES LO NUEVO: CAPTURAR CAMBIOS DESDE EL ADMIN ---
    def save_model(self, request, obj, form, change):
        if change: # Si es una edición (UPDATE)
            from .utils import registrar_auditoria_update
            import copy
            # Obtenemos la versión vieja antes de guardar
            viejo = Comprobante.objects.get(pk=obj.pk)
            # Llamamos a nuestra función de auditoría
            registrar_auditoria_update(request.user, viejo, obj, "Cambio realizado desde el Panel de Administración")
        
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        # Registrar el borrado antes de que ocurra
        LogAuditoria.objects.create(
            usuario=request.user,
            empresa=obj.empresa,
            accion='DELETE',
            tabla_afectada='Comprobante',
            referencia_id=obj.id,
            motivo_cambio=f"Borrado desde Panel de Admin: {obj.codigo_factura}"
        )
        super().delete_model(request, obj)

admin.site.register(Entidad)
admin.site.register(ComprobanteDetalle) # Registro individual por si quieres buscar items sueltos

# --- Módulo de Inventario ---
@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ('sku', 'nombre_interno', 'stock_actual', 'precio_compra_referencial', 'precio_venta_referencial', 'empresa')
    search_fields = ('sku', 'nombre_interno')
    list_filter = ('empresa', 'categoria')

admin.site.register(CategoriaProducto)

# --- Módulo de Préstamos y Deudas (Cronogramas) ---
@admin.register(Prestamo)
class PrestamoAdmin(admin.ModelAdmin):
    list_display = ('prestamista', 'monto_capital', 'monto_interes', 'estado', 'fecha_vencimiento')
    list_filter = ('estado', 'empresa')
    # NUEVO: Ver las cuotas del préstamo aquí mismo
    inlines = [CuotaInline]

@admin.register(CuentaEstado)
class CuentaEstadoAdmin(admin.ModelAdmin):
    list_display = ('comprobante', 'monto_total', 'saldo_pendiente', 'estado')
    list_filter = ('estado',)
    inlines = [CuotaInline]

admin.site.register(Cuota) # Registro individual para ver el cronograma global

# --- Módulo de Retenciones ---
@admin.register(CertificadoRetencion)
class CertificadoRetencionAdmin(admin.ModelAdmin):
    list_display = ('serie_numero', 'agente_retencion', 'fecha_emision', 'monto_total_pen')
    inlines = [RetencionDetalleInline]

admin.site.register(RetencionDetalle)

# --- Módulo de Gastos ---
admin.site.register(CategoriaGasto)
admin.site.register(GastoOperativo)

# --- Módulo de Control y Auditoría ---
@admin.register(LogAuditoria)
class LogAuditoriaAdmin(admin.ModelAdmin):
    list_display = ('fecha_hora', 'usuario', 'accion', 'tabla_afectada', 'referencia_id')
    list_filter = ('accion', 'tabla_afectada', 'usuario')
    readonly_fields = ('fecha_hora', 'usuario', 'accion', 'tabla_afectada', 'referencia_id', 'motivo_cambio')

admin.site.register(TipoCambioDia)
admin.site.register(Notificacion)

from .models import DeclaracionMensual, PagoImpuesto

admin.site.register(DeclaracionMensual)
admin.site.register(PagoImpuesto)
class CotizacionDetalleInline(admin.TabularInline):
    model = CotizacionDetalle
    extra = 0

@admin.register(Cotizacion)
class CotizacionAdmin(admin.ModelAdmin):
    list_display = ('numero', 'empresa', 'nombre_cliente', 'total', 'estado')
    inlines = [CotizacionDetalleInline]