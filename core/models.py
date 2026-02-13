from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

# 1.1 Empresa (Sub-empresas)
class Empresa(models.Model):
    nombre = models.CharField(max_length=100)
    ruc = models.CharField(max_length=11, unique=True)
    estado = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    logo = models.ImageField(upload_to='logos/', null=True, blank=True)
    direccion = models.TextField(null=True, blank=True)
    telefono = models.CharField(max_length=20, null=True, blank=True)
    correo = models.EmailField(null=True, blank=True)

    def __str__(self):
        return self.nombre

# 1.2 Rol
class Rol(models.Model):
    # Quitamos los ROLES_CHOICES para que seas libre de crear cualquier nombre
    nombre = models.CharField(max_length=50, unique=True) # Ej: Socio, Administrador, Vendedor
    permisos = models.JSONField(default=dict, help_text="Define qué puede ver en el Dashboard")

    class Meta:
        verbose_name = "Rol"
        verbose_name_plural = "Roles"

    def __str__(self):
        return self.nombre

# 1.3 Usuario Personalizado
class Usuario(AbstractUser):
    empresa_actual = models.ForeignKey(Empresa, on_delete=models.SET_NULL, null=True, blank=True, related_name='usuarios_activos')
    rol = models.ForeignKey(Rol, on_delete=models.PROTECT, null=True)
    
    # Relación muchos a muchos para saber en qué empresas puede trabajar
    empresas_permitidas = models.ManyToManyField(Empresa, related_name='personal')

    def __str__(self):
        return f"{self.username} - {self.rol}"
    
# 2.1 Caja (Efectivo/Caja Chica)
class Caja(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100) # Ej: Caja Principal, Caja Chica
    moneda = models.CharField(max_length=3, default='PEN')
    saldo_actual = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = "Caja (Efectivo)"
        verbose_name_plural = "Cajas (Efectivo)"

    def __str__(self):
        return f"{self.nombre} ({self.moneda}) - S/ {self.saldo_actual}"

# 2.2 Cuenta Bancaria
class Cuenta_Bancaria(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    banco = models.CharField(max_length=100) # Ej: BCP, BBVA
    numero_cuenta = models.CharField(max_length=50)
    moneda = models.CharField(max_length=3, default='PEN')
    saldo_actual = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cci = models.CharField(max_length=50, null=True, blank=True, verbose_name="Código de Cuenta Interbancaria (CCI)")

    class Meta:
        verbose_name = "Cuenta Bancaria"
        verbose_name_plural = "Cuentas Bancarias"

    def __str__(self):
        return f"{self.banco} {self.moneda} - {self.numero_cuenta}"

# 3.1 Proveedor / Cliente (Entidades)
class Entidad(models.Model):
    TIPO_CHOICES = [('Proveedor', 'Proveedor'), ('Cliente', 'Cliente'), ('Ambos', 'Ambos')]
    DOC_CHOICES = [('RUC', 'RUC'), ('DNI', 'DNI')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    tipo_entidad = models.CharField(max_length=20, choices=TIPO_CHOICES)
    tipo_documento = models.CharField(max_length=10, choices=DOC_CHOICES)
    numero_documento = models.CharField(max_length=20) # Único por empresa logicamente
    nombre_razon_social = models.CharField(max_length=200)
    direccion = models.TextField(null=True, blank=True)

    def __str__(self):
        return self.nombre_razon_social

# 4.1 Categoria
class CategoriaProducto(models.Model):
    nombre = models.CharField(max_length=100)
    def __str__(self): return self.nombre

# 4.2 Producto
class Producto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    categoria = models.ForeignKey(CategoriaProducto, on_delete=models.SET_NULL, null=True)
    sku = models.CharField(max_length=50) # RF-08
    nombre_interno = models.CharField(max_length=200)
    nombres_alternativos = models.JSONField(default=list, blank=True) # RF-07 (Aprendizaje)
    stock_actual = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    precio_compra_referencial = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    precio_venta_referencial = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def __str__(self): return self.nombre_interno

class AjusteStock(models.Model):
    TIPO_CHOICES = [('Ingreso', 'Ingreso (Suma)'), ('Egreso', 'Egreso (Resta)')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='ajustes')
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    cantidad = models.DecimalField(max_digits=10, decimal_places=2)
    motivo = models.TextField() # Ej: "Producto dañado en almacén"
    fecha = models.DateTimeField(auto_now_add=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.PROTECT)

    class Meta:
        verbose_name = "Ajuste de Stock"
        verbose_name_plural = "Ajustes de Stock"

    def __str__(self):
        return f"{self.tipo} - {self.producto.nombre_interno} ({self.cantidad})"
    
# 3.2 Comprobante (Entidad Padre)
class Comprobante(models.Model):
    OP_CHOICES = [
        ('Compra', 'Compra'), 
        ('Venta', 'Venta')]
    DOC_TIPOS = [
        ('Factura', 'Factura'), 
        ('Boleta', 'Boleta'),
        ('Recibo', 'Recibo / Nota de Venta'),
        ('Otros', 'Otros (AliExpress/Amazon)')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    entidad = models.ForeignKey(Entidad, on_delete=models.PROTECT)
    tipo_documento = models.CharField(max_length=20, choices=DOC_TIPOS)
    operacion = models.CharField(max_length=20, choices=OP_CHOICES)
    serie = models.CharField(max_length=10)
    numero = models.CharField(max_length=20)
    fecha_emision = models.DateField()
    moneda = models.CharField(max_length=3, default='PEN')
    tipo_cambio = models.DecimalField(max_digits=10, decimal_places=3, default=1.0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    igv = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    estado_sunat = models.CharField(max_length=20, default='PENDIENTE')
    es_escudo_tributario = models.BooleanField(default=False)
    # Landed Cost (Prorrateo RF-12)
    es_flete = models.BooleanField(default=False)
    comprobante_asociado = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True)

    @property
    def codigo_factura(self):
        return f"{self.serie}-{self.numero}"

    def __str__(self):
        return f"{self.codigo_factura} ({self.entidad.nombre_razon_social})"

 
class ComprobanteDetalle(models.Model):
    comprobante = models.ForeignKey(Comprobante, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, null=True, blank=True)
    descripcion_libre = models.TextField(null=True, blank=True)
    cantidad = models.DecimalField(max_digits=10, decimal_places=2)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal_linea = models.DecimalField(max_digits=12, decimal_places=2)



class CategoriaGasto(models.Model):
    nombre = models.CharField(max_length=100)
    def __str__(self): return self.nombre

class GastoOperativo(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    categoria_gasto = models.ForeignKey(CategoriaGasto, on_delete=models.PROTECT)
    comprobante = models.ForeignKey(Comprobante, on_delete=models.SET_NULL, null=True, blank=True)
    descripcion = models.TextField()
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    moneda = models.CharField(max_length=3, default='PEN')
    fecha = models.DateField()

    def __str__(self):
        return f"Gasto: {self.descripcion[:30]} - {self.monto}"


class Prestamo(models.Model):
    ESTADO_CHOICES = [('Pendiente', 'Pendiente'), ('Pagado', 'Pagado')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    # CLAVE: Vincula el préstamo a una compra específica para el Margen Neto (RF-10)
    comprobante = models.ForeignKey(
        'Comprobante', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='prestamos'
    )
    
    prestamista = models.CharField(max_length=200)
    moneda = models.CharField(max_length=3, default='PEN')
    monto_capital = models.DecimalField(max_digits=12, decimal_places=2)
    porcentaje_interes = models.DecimalField(max_digits=5, decimal_places=2)
    monto_interes = models.DecimalField(max_digits=12, decimal_places=2, editable=False) # Se calcula solo
    
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='Pendiente')
    fecha_prestamo = models.DateField()
    fecha_vencimiento = models.DateField()

    def save(self, *args, **kwargs):
        # Convertimos a Decimal por si acaso lleguen como string
        import decimal
        capital = decimal.Decimal(str(self.monto_capital))
        interes = decimal.Decimal(str(self.porcentaje_interes))
        
        # Calcular el interés (RF-11)
        self.monto_interes = (capital * interes) / 100
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Préstamo {self.prestamista} - {self.monto_capital}"

class CuentaEstado(models.Model):
    ESTADO_CHOICES = [('Pendiente', 'Pendiente'), ('Parcial', 'Parcial'), ('Cancelado', 'Cancelado')]
    
    comprobante = models.ForeignKey(Comprobante, on_delete=models.CASCADE, related_name='cuenta_estado')
    monto_total = models.DecimalField(max_digits=12, decimal_places=2)
    saldo_pendiente = models.DecimalField(max_digits=12, decimal_places=2)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='Pendiente')
    fecha_vencimiento = models.DateField()

    def __str__(self):
        return f"Cuenta {self.comprobante} - Saldo: {self.saldo_pendiente}"

# 6.1 Tipo de Cambio del Día
class TipoCambioDia(models.Model):
    fecha = models.DateField(primary_key=True)
    compra = models.DecimalField(max_digits=6, decimal_places=3)
    venta = models.DecimalField(max_digits=6, decimal_places=3)
    fuente = models.CharField(max_length=50, default='SUNAT / SBS')

    def __str__(self):
        return f"{self.fecha}: C:{self.compra} V:{self.venta}"

class LogAuditoria(models.Model):
    ACCIONES = [('INSERT', 'INSERT'), ('UPDATE', 'UPDATE'), ('DELETE', 'DELETE')]
    
    id_log = models.AutoField(primary_key=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.PROTECT)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, null=True, blank=True)
    accion = models.CharField(max_length=10, choices=ACCIONES)
    tabla_afectada = models.CharField(max_length=100)
    referencia_id = models.IntegerField()
    motivo_cambio = models.TextField() # Obligatorio para borrados
    fecha_hora = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.accion} en {self.tabla_afectada} por {self.usuario.username}"
    

class MovimientoFinanciero(models.Model):
    TIPO_CHOICES = [('Ingreso', 'Ingreso'), ('Egreso', 'Egreso')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    
    # --- NUEVOS CAMPOS SEGÚN MER 2.3 ---
    caja = models.ForeignKey(Caja, on_delete=models.SET_NULL, null=True, blank=True)
    cuenta_bancaria = models.ForeignKey(Cuenta_Bancaria, on_delete=models.SET_NULL, null=True, blank=True)
    # -----------------------------------
    diferencia_cambio_soles = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tipo_cambio_operacion = models.DecimalField(max_digits=10, decimal_places=3, default=1.0)
    
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    moneda = models.CharField(max_length=3, default='PEN')
    fecha = models.DateTimeField(auto_now_add=True)
    referencia = models.CharField(max_length=255)
    itf_monto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    comprobante = models.ForeignKey(Comprobante, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Movimiento Financiero"
        verbose_name_plural = "Movimientos Financieros"

    def __str__(self):
        return f"{self.tipo} ({self.moneda}): {self.monto} - {self.referencia}"
    
class Cuota(models.Model):
    # Relación con Facturas (Venta/Compra) - Ahora es opcional (null=True)
    cuenta = models.ForeignKey(CuentaEstado, on_delete=models.CASCADE, related_name='cuotas', null=True, blank=True)
    
    # NUEVA Relación con Préstamos (Financiamiento)
    prestamo = models.ForeignKey(Prestamo, on_delete=models.CASCADE, related_name='cuotas', null=True, blank=True)
    
    numero_cuota = models.IntegerField()
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    fecha_vencimiento = models.DateField()
    pagada = models.BooleanField(default=False)
    fecha_pago = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Cuota de Pago/Cobro"
        verbose_name_plural = "Cuotas de Pago/Cobro"
        ordering = ['fecha_vencimiento'] # Orden automático por fecha

    def __str__(self):
        origen = self.cuenta.comprobante.codigo_factura if self.cuenta else f"Préstamo {self.prestamo.prestamista}"
        return f"Cuota {self.numero_cuota} - {origen}"
    

class Notificacion(models.Model):
    TIPOS = [('PRECIO', 'Variación de Precio'), ('VENTA', 'Venta Registrada'), ('COBRANZA', 'Cobranza Recibida')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    mensaje = models.TextField()
    tipo = models.CharField(max_length=20, choices=TIPOS)
    fecha = models.DateTimeField(auto_now_add=True)
    leida = models.BooleanField(default=False)

    class Meta:
        ordering = ['-fecha'] # Las más recientes primero

    def __str__(self):
        return f"{self.tipo} - {self.mensaje[:30]}"
    
@receiver(post_save, sender=MovimientoFinanciero)
def actualizar_saldo_al_guardar(sender, instance, created, **kwargs):
    if created:
        import decimal
        # Forzamos que tanto el monto como el itf sean Decimales antes de operar
        monto_decimal = decimal.Decimal(str(instance.monto))
        itf_decimal = decimal.Decimal(str(instance.itf_monto))

        if instance.cuenta_bancaria:
            obj = instance.cuenta_bancaria
            if instance.tipo == 'Ingreso':
                # Ahora la operación es entre dos Decimales: ÉXITO
                obj.saldo_actual += (monto_decimal - itf_decimal)
            else:
                obj.saldo_actual -= (monto_decimal + itf_decimal)
            obj.save()
            
        elif instance.caja:
            obj = instance.caja
            if instance.tipo == 'Ingreso':
                obj.saldo_actual += monto_decimal
            else:
                obj.saldo_actual -= monto_decimal
            obj.save()


@receiver(post_delete, sender=MovimientoFinanciero)
def revertir_saldo_al_eliminar(sender, instance, **kwargs):
    """Reversión total incluyendo el ITF"""
    import decimal
    if instance.cuenta_bancaria:
        obj = instance.cuenta_bancaria
        itf = decimal.Decimal(str(instance.itf_monto))
        if instance.tipo == 'Ingreso':
            obj.saldo_actual -= (instance.monto - itf)
        else:
            obj.saldo_actual += (instance.monto + itf)
        obj.save()
    elif instance.caja:
        obj = instance.caja
        if instance.tipo == 'Ingreso':
            obj.saldo_actual -= instance.monto
        else:
            obj.saldo_actual += instance.monto
        obj.save()

class CertificadoRetencion(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    agente_retencion = models.ForeignKey(Entidad, on_delete=models.PROTECT) # El cliente que te retuvo
    serie_numero = models.CharField(max_length=20) # Ej: E001-641
    fecha_emision = models.DateField()
    monto_total_pen = models.DecimalField(max_digits=12, decimal_places=2) # El total en Soles

    class Meta:
        verbose_name = "Certificado de Retención"
        verbose_name_plural = "Certificados de Retención"

    def __str__(self):
        # Aquí serie_numero SI existe como campo, así que funciona bien.
        return f"Certificado {self.serie_numero}"

class RetencionDetalle(models.Model):
    certificado = models.ForeignKey(CertificadoRetencion, on_delete=models.CASCADE, related_name='detalles')
    # Factura a la que se le aplica la retención
    comprobante = models.ForeignKey(Comprobante, on_delete=models.CASCADE, limit_choices_to={'operacion': 'Venta'})
    
    monto_retencion_pen = models.DecimalField(max_digits=12, decimal_places=2) # Valor legal en Soles
    monto_descuento_moneda_origen = models.DecimalField(max_digits=12, decimal_places=2) # Valor para matar deuda ($)
    tipo_cambio_aplicado = models.DecimalField(max_digits=10, decimal_places=4) # El TC que usó el cliente

    def __str__(self):
        # Cambiamos serie_numero por codigo_factura
        return f"Retención de {self.comprobante.codigo_factura} - S/ {self.monto_retencion_pen}"

class CierreMensual(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    periodo = models.CharField(max_length=7) # Ej: 2025-09
    igv_final_calculado = models.DecimalField(max_digits=12, decimal_places=2)
    saldo_a_favor_generado = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cerrado = models.BooleanField(default=False)
    fecha_cierre = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('empresa', 'periodo')
        verbose_name = "Cierre Mensual Tributario"
        verbose_name_plural = "Cierres Mensuales Tributarios"

    def __str__(self):
        return f"Cierre {self.periodo} - {self.empresa.nombre}"
    
class DeclaracionMensual(models.Model):
    TRIBUTO_CHOICES = [('1011', 'IGV'), ('3111', 'Renta')]
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    periodo = models.CharField(max_length=7) # Ej: 2025-10
    tributo = models.CharField(max_length=4, choices=TRIBUTO_CHOICES)
    monto_declarado = models.DecimalField(max_digits=12, decimal_places=2)
    numero_orden = models.CharField(max_length=20)
    fecha_presentacion = models.DateField()

    class Meta:
        verbose_name = "Declaración PDT 0621"
        verbose_name_plural = "Declaraciones PDT 0621"

class PagoImpuesto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    declaracion = models.ForeignKey(DeclaracionMensual, on_delete=models.SET_NULL, null=True, blank=True, related_name='pagos')
    monto_pagado = models.DecimalField(max_digits=12, decimal_places=2)
    fecha_pago = models.DateField()
    numero_operacion = models.CharField(max_length=20)
    periodo = models.CharField(max_length=7, null=True, blank=True) # Ej: 202509
    tributo_codigo = models.CharField(max_length=4, null=True, blank=True) # Ej: 1011
    # Vinculamos al movimiento de caja para que el banco cuadre
    movimiento = models.OneToOneField(MovimientoFinanciero, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        verbose_name = "Pago SUNAT 1662"
        verbose_name_plural = "Pagos SUNAT 1662"

# 2. NUEVA CLASE COTIZACIÓN
class Cotizacion(models.Model):
    ESTADOS = [('Pendiente', 'Pendiente'), ('Aceptada', 'Aceptada'), ('Rechazada', 'Rechazada')]
    
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    numero = models.CharField(max_length=20) # Ej: COT-0001
    fecha = models.DateField(auto_now_add=True)
    validez_dias = models.IntegerField(default=5)
    
    # Datos del Cliente
    ruc_dni_cliente = models.CharField(max_length=20)
    nombre_cliente = models.CharField(max_length=200)
    direccion_cliente = models.TextField(null=True, blank=True)
    atencion_a = models.CharField(max_length=100, null=True, blank=True)
    
    # Finanzas
    moneda = models.CharField(max_length=3, default='PEN')
    tipo_cambio = models.DecimalField(max_digits=10, decimal_places=3, default=1.0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Condiciones
    garantia = models.CharField(max_length=100, default="12 meses")
    tiempo_entrega = models.CharField(max_length=100, default="Inmediato")
    notas = models.TextField(null=True, blank=True)
    estado = models.CharField(max_length=20, choices=ESTADOS, default='Pendiente')

    class Meta:
        verbose_name = "Cotización"
        verbose_name_plural = "Cotizaciones"

class CotizacionDetalle(models.Model):
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name='detalles')
    # Opcional: amarrar a un producto real si existe
    producto = models.ForeignKey(Producto, on_delete=models.SET_NULL, null=True, blank=True)
    # Si no existe en el catálogo, usamos este campo:
    descripcion_libre = models.TextField()
    cantidad = models.DecimalField(max_digits=10, decimal_places=2)
    precio_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    
    @property
    def total_linea(self):
        return self.cantidad * self.precio_unitario
    def __str__(self):
        return f"{self.cantidad} x {self.descripcion_libre[:30]}"
    
