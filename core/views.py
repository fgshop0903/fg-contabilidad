from datetime import datetime
import decimal
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from .models import AjusteStock, Caja, CategoriaGasto, CategoriaProducto, CertificadoRetencion, Cotizacion, CotizacionDetalle, Cuenta_Bancaria, CuentaEstado, Cuota, DeclaracionMensual, Empresa, Entidad, GastoOperativo, LogAuditoria, MovimientoFinanciero, Notificacion, PagoImpuesto, Prestamo, Producto, Comprobante, ComprobanteDetalle, RetencionDetalle, TipoCambioDia
from .utils import consultar_validez_sunat, procesar_pdf_sunat, procesar_xml_sunat
import uuid
from django.db import transaction
from django.db.models import Sum
from .services import verificar_variacion_precio
from .decorators import admin_required
from core import models
from .utils import registrar_auditoria_update
import copy
import datetime
from django.db.models import F
from .utils import procesar_xml_retencion
from .utils import procesar_pdf_impuestos
from .models import CierreMensual
from django.contrib.auth import logout as auth_logout
import re
from itertools import chain
from operator import attrgetter

@login_required
def seleccionar_empresa(request):
    # El usuario solo ve las empresas a las que tiene acceso
    empresas = request.user.empresas_permitidas.all()
    
    if request.method == 'POST':
        empresa_id = request.POST.get('empresa_id')
        empresa = Empresa.objects.get(id=empresa_id)
        # Guardamos en la sesión la empresa seleccionada para filtrar todo el sistema (RF-03)
        request.session['empresa_id'] = empresa.id
        request.session['empresa_nombre'] = empresa.nombre
        return redirect('dashboard') # Redirigir al dashboard una vez seleccionada
        
    return render(request, 'core/seleccionar_empresa.html', {'empresas': empresas})

@login_required
def cargar_compra(request):
    empresa_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=empresa_id)
    datos = None 

    if request.method == 'POST' and request.FILES.get('documento'):
        archivo = request.FILES['documento']
        nombre_archivo = archivo.name.lower() # Convertimos a minúsculas para comparar bien

        # 1. PROCESAMIENTO SEGÚN FORMATO
        try:
            if nombre_archivo.endswith('.xml'):
                datos = procesar_xml_sunat(archivo)
            elif nombre_archivo.endswith('.pdf'):
                datos = procesar_pdf_sunat(archivo)
            else:
                return render(request, 'core/cargar_compra.html', {'error': 'Formato no soportado. Use XML o PDF.'})
        except Exception as e:
            return render(request, 'core/cargar_compra.html', {'error': f'Error al leer el archivo: {str(e)}'})
        

        # 2. VALIDACIÓN DE SEGURIDAD (Si 'datos' no se llenó correctamente)
        if not datos or not datos.get('ruc_proveedor') or not datos.get('serie_numero'):
            return render(request, 'core/cargar_compra.html', {
                'error': 'No se pudieron extraer los datos críticos (RUC o Número). Asegúrese de que el archivo sea un comprobante válido de SUNAT.'
            })

        # --- RF-23: VALIDACIÓN AUTOMÁTICA SUNAT ---
        estado_sunat_validado = consultar_validez_sunat(
            serie=datos['serie_numero'].split('-')[0],
            numero=datos['serie_numero'].split('-')[1],
            ruc_emisor=datos['ruc_proveedor'],
            total=float(datos.get('total', 0))
        )

        # 3. RF-06: CREACIÓN AUTOMÁTICA DE PROVEEDOR
        proveedor, created = Entidad.objects.get_or_create(
            empresa=empresa,
            numero_documento=datos['ruc_proveedor'],
            defaults={
                'nombre_razon_social': datos['razon_social_proveedor'],
                'tipo_entidad': 'Proveedor',
                'tipo_documento': 'RUC'
            }
        )

         # --- NUEVA LÓGICA DE DUPLICADOS (RF-23) ---
        sn_partes = datos['serie_numero'].split('-')
        serie_doc = sn_partes[0]
        numero_doc = sn_partes[1] if len(sn_partes) > 1 else "0"

        # Buscamos si ya existe esta factura para este proveedor en esta empresa
        factura_duplicada = Comprobante.objects.filter(
            empresa=empresa,
            entidad=proveedor,
            serie=serie_doc,
            numero=numero_doc,
            operacion='Compra'
        ).exists()

        # CARGA DE CATÁLOGO: Traemos todos los productos para el select manual
        todos_los_productos = Producto.objects.filter(empresa=empresa).order_by('nombre_interno')

        # 4. RF-07: PROCESAMIENTO DE ÍTEMS (APRENDIZAJE)
        items_procesados = []
        for item in datos['items']:
            producto_existente = Producto.objects.filter(
                empresa=empresa, 
                nombre_interno__icontains=item['descripcion'][:30]
            ).first()
            
            if not producto_existente:
                producto_existente = Producto.objects.filter(
                    empresa=empresa, 
                    nombre_interno__iexact=item['descripcion']
                ).first()

            items_procesados.append({
                    'descripcion_xml': item['descripcion'],
                    'cantidad': float(item['cantidad']),
                    'precio_unitario': float(item['precio_unitario']),
                    'producto_id': producto_existente.id if producto_existente else None,
                    'nombre_sistema': producto_existente.nombre_interno if producto_existente else None,
                    'sugerencia_sku': f"SKU-{uuid.uuid4().hex[:6].upper()}"
                })


        request.session['temp_compra'] = {
            'proveedor_id': proveedor.id,
            'datos_xml': datos,
            'items_procesados': items_procesados,
            'estado_sunat': estado_sunat_validado
        }
        
        return render(request, 'core/confirmar_compra.html', {
            'proveedor': proveedor,
            'datos': datos,
            'items': items_procesados,
            'estado_sunat': estado_sunat_validado,
            'mis_productos': todos_los_productos, # <-- ENVIAMOS EL CATÁLOGO
            'ya_existe': factura_duplicada
        })

    return render(request, 'core/cargar_compra.html')

@login_required
@transaction.atomic
def guardar_compra(request):
    if request.method == 'POST':
    # 1. Recuperar datos temporales de la sesión
        temp_data = request.session.get('temp_compra')
        if not temp_data:
            return redirect('cargar_compra')

        empresa = Empresa.objects.get(id=request.session['empresa_id'])
        datos_xml = temp_data['datos_xml']
        proveedor = Entidad.objects.get(id=temp_data['proveedor_id'])
        tc_manual = decimal.Decimal(request.POST.get('tipo_cambio', '1.000'))

        # 2. Datos de cabecera
        total_xml = decimal.Decimal(str(datos_xml['total']))
        subtotal_calc = total_xml / decimal.Decimal('1.18')
        igv_calc = total_xml - subtotal_calc
        
        # Detectar si el usuario activó el switch de Escudo Tributario
        es_escudo = 'es_escudo' in request.POST

        comprobante = Comprobante.objects.create(
            empresa=empresa,
            entidad=proveedor,
            tipo_documento='Factura',
            operacion='Compra',
            serie=datos_xml['serie_numero'].split('-')[0],
            numero=datos_xml['serie_numero'].split('-')[1],
            fecha_emision=datos_xml['fecha_emision'],
            moneda=datos_xml['moneda'],
            subtotal=subtotal_calc,
            tipo_cambio=tc_manual,
            igv=igv_calc,
            total=total_xml,
            estado_sunat=temp_data.get('estado_sunat', 'PENDIENTE'),
            es_escudo_tributario=es_escudo
        )

        # --- CAMINO A: ES ESCUDO TRIBUTARIO (SOLO IGV) ---
        if es_escudo:
            for item in temp_data['items_procesados']:
                ComprobanteDetalle.objects.create(
                    comprobante=comprobante,
                    producto=None, # No afecta inventario
                    cantidad=decimal.Decimal(str(item['cantidad'])),
                    precio_unitario=decimal.Decimal(str(item['precio_unitario'])),
                    subtotal_linea=decimal.Decimal(str(item['cantidad'])) * decimal.Decimal(str(item['precio_unitario']))
                )

            # Cuenta se crea CANCELADA porque no es deuda de la empresa
            CuentaEstado.objects.create(
                comprobante=comprobante,
                monto_total=total_xml,
                saldo_pendiente=0, # No debemos nada
                fecha_vencimiento=datetime.date.today(),
                estado='Cancelado'
            )

        # --- CAMINO B: COMPRA REAL DEL NEGOCIO ---
        else:
            # Lógica de prorrateo de flete
            monto_flete_interno = decimal.Decimal('0.00')
            valor_base_productos = decimal.Decimal('0.00')

            for i, item in enumerate(temp_data['items_procesados']):
                destino = request.POST.get(f'destino_{i}')
                sub_item = decimal.Decimal(str(item['cantidad'])) * decimal.Decimal(str(item['precio_unitario']))
                if destino == 'flete': monto_flete_interno += sub_item
                elif destino == 'inventario': valor_base_productos += sub_item

            # Procesar ítems (Inventario / Gasto / Flete)
            for i, item in enumerate(temp_data['items_procesados']):
                destino = request.POST.get(f'destino_{i}')
                nombre_xml = item['descripcion_xml']
                cant = decimal.Decimal(str(item['cantidad']))
                precio_uni_xml = decimal.Decimal(str(item['precio_unitario']))
                subtotal_item_base = cant * precio_uni_xml

                if destino == 'inventario':
                    prod_id = request.POST.get(f'prod_id_{i}')
                    p_v_raw = request.POST.get(f'precio_venta_{i}')
                    p_venta_sugerido = decimal.Decimal(p_v_raw) if p_v_raw else decimal.Decimal('0.00')
                    if prod_id:
                        producto = Producto.objects.get(id=prod_id)
                        if nombre_xml not in producto.nombres_alternativos:
                            producto.nombres_alternativos.append(nombre_xml)
                    else:
                        producto = Producto.objects.create(
                            empresa=empresa,
                            sku=request.POST.get(f'sku_{i}'),
                            nombre_interno=nombre_xml,
                            nombres_alternativos=[nombre_xml],
                            stock_actual=0, 
                            precio_compra_referencial=0 
                        )

                    # Landed Cost (Costo real en Soles)
                    flete_unid = (monto_flete_interno * (subtotal_item_base / valor_base_productos)) / cant if monto_flete_interno > 0 else 0
                    p_unit_real_moneda = precio_uni_xml + flete_unid
                    p_unit_pen = p_unit_real_moneda * tc_manual

                    verificar_variacion_precio(producto, p_unit_pen)
                    
                    producto.precio_compra_referencial = p_unit_pen
                    producto.precio_venta_referencial = p_venta_sugerido
                    producto.stock_actual += cant
                    producto.save()

                    ComprobanteDetalle.objects.create(
                        comprobante=comprobante, producto=producto,
                        cantidad=cant, precio_unitario=p_unit_real_moneda,
                        subtotal_linea=cant * p_unit_real_moneda
                    )

                elif destino == 'gasto':
                    cat_gasto, _ = CategoriaGasto.objects.get_or_create(nombre="General")
                    GastoOperativo.objects.create(
                        empresa=empresa, categoria_gasto=cat_gasto, comprobante=comprobante,
                        descripcion=nombre_xml, monto=subtotal_item_base,
                        moneda=datos_xml['moneda'], fecha=datos_xml['fecha_emision']
                    )
                    ComprobanteDetalle.objects.create(
                        comprobante=comprobante, producto=None,
                        cantidad=cant, precio_unitario=precio_uni_xml, subtotal_linea=subtotal_item_base
                    )
                    
                elif destino == 'flete':
                    ComprobanteDetalle.objects.create(
                        comprobante=comprobante, producto=None,
                        cantidad=cant, precio_unitario=precio_uni_xml, subtotal_linea=subtotal_item_base
                    )

            # Crear Cuenta por Pagar real
            CuentaEstado.objects.create(
                comprobante=comprobante,
                monto_total=total_xml,
                saldo_pendiente=total_xml,
                fecha_vencimiento=datetime.date.today() + datetime.timedelta(days=30),
                estado='Pendiente'
            )

        # 5. Limpiar sesión y terminar
        if 'temp_compra' in request.session:
            del request.session['temp_compra']
            
        return redirect('dashboard')

    return redirect('cargar_compra') ,

# core/views.py

@login_required
@transaction.atomic # Garantiza que si falla el movimiento, no se guarde el préstamo
def registrar_prestamo(request):
    emp_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=emp_id)

    if request.method == 'POST':
        # 1. Captura y Conversión Segura de Montos
        try:
            monto = decimal.Decimal(request.POST.get('monto_capital', '0'))
            interes_p = decimal.Decimal(request.POST.get('porcentaje_interes', '0'))
        except (decimal.InvalidOperation, TypeError):
            monto = decimal.Decimal('0')
            interes_p = decimal.Decimal('0')

        # 2. Captura de datos de cabecera
        comp_id = request.POST.get('comprobante_id')
        prestamista = request.POST.get('prestamista')
        moneda = request.POST.get('moneda')
        fecha_p = request.POST.get('fecha_prestamo')
        fecha_v = request.POST.get('fecha_vencimiento')
        
        # Cuentas de destino para que el dinero "exista" en el banco
        caja_id = request.POST.get('caja_id')
        banco_id = request.POST.get('banco_id')

        # 3. Crear el registro del Préstamo (La Deuda)
        # Si comp_id está vacío, se guarda como None (Libre disponibilidad)
        nuevo_prestamo = Prestamo.objects.create(
            empresa=empresa,
            comprobante_id=comp_id if comp_id else None,
            prestamista=prestamista,
            moneda=moneda,
            monto_capital=monto,
            porcentaje_interes=interes_p,
            fecha_prestamo=fecha_p,
            fecha_vencimiento=fecha_v,
            estado='Pendiente'
        )

        # 4. LÓGICA DE INYECCIÓN DE CAPITAL (Ingreso Automático a Bancos/Caja)
        # Creamos un movimiento de tipo Ingreso para que tu Saldo Bruto suba
        MovimientoFinanciero.objects.create(
            empresa=empresa,
            tipo='Ingreso',
            monto=monto,
            moneda=moneda,
            referencia=f"Inyección de Capital: Préstamo de {prestamista}",
            # Vinculamos a la cuenta elegida para que el Signal actualice el saldo
            caja_id=caja_id if caja_id else None,
            cuenta_bancaria_id=banco_id if banco_id else None
        )

        # Redirigimos a la lista para ver el nuevo préstamo
        return redirect('lista_prestamos')

    # --- LÓGICA PARA EL MÉTODO GET (Mostrar el Formulario) ---
    # Compras para el selector opcional
    compras = Comprobante.objects.filter(empresa=empresa, operacion='Compra').order_by('-fecha_emision')
    # Cajas y Bancos para elegir dónde entra el dinero
    cajas = Caja.objects.filter(empresa=empresa)
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)

    return render(request, 'core/registrar_prestamo.html', {
        'compras': compras,
        'cajas': cajas,
        'bancos': bancos,
        'hoy': datetime.date.today()
    })

@login_required
@transaction.atomic
def registrar_flete(request):
    empresa_id = request.session.get('empresa_id')
    compras = Comprobante.objects.filter(empresa_id=empresa_id, operacion='Compra', es_flete=False)

    if request.method == 'POST':
        compra_asociada = Comprobante.objects.get(id=request.POST.get('compra_id'))
        monto_flete = decimal.Decimal(request.POST.get('monto_flete'))

        # 1. Crear el comprobante de flete
        # Usamos round(..., 2) para evitar los decimales infinitos
        subtotal = round(monto_flete / decimal.Decimal('1.18'), 2)
        igv = monto_flete - subtotal

        flete = Comprobante.objects.create(
            empresa_id=empresa_id,
            entidad_id=request.POST.get('proveedor_flete_id'),
            tipo_documento='Factura',
            operacion='Compra',
            serie=request.POST.get('serie'),
            numero=request.POST.get('numero'),
            fecha_emision=request.POST.get('fecha'),
            total=monto_flete,
            subtotal=subtotal,
            igv=igv,
            es_flete=True,
            comprobante_asociado=compra_asociada
        )

        # --- AQUÍ ESTÁ LA CORRECCIÓN CLAVE ---
        # Registramos la deuda por el TOTAL (S/ 39.00)
        CuentaEstado.objects.create(
            comprobante=flete,
            monto_total=monto_flete,
            saldo_pendiente=monto_flete,
            fecha_vencimiento=request.POST.get('fecha'),
            estado='Pendiente'
        )
        # -------------------------------------

        # 2. PRORRATEO (RF-13): Distribuir el flete proporcionalmente
        detalles = compra_asociada.detalles.all()
        total_compra_base = compra_asociada.subtotal

        if total_compra_base > 0:
            for detalle in detalles:
                # El flete se reparte basado en el valor de cada producto
                porcentaje_del_valor = detalle.subtotal_linea / total_compra_base
                parte_proporcional_flete = monto_flete * porcentaje_del_valor
                
                # Actualizamos el costo unitario del producto en el inventario
                # sumándole su pedacito de flete
                flete_por_unidad = parte_proporcional_flete / detalle.cantidad
                producto = detalle.producto
                if producto:
                    producto.precio_compra_referencial += flete_por_unidad
                    producto.save()
            
        return redirect('dashboard')

    proveedores = Entidad.objects.filter(empresa_id=empresa_id, tipo_entidad__in=['Proveedor', 'Ambos'])
    return render(request, 'core/registrar_flete.html', {
        'compras': compras,
        'proveedores': proveedores
    })


@login_required
def cargar_venta(request):
    empresa_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == 'POST' and request.FILES.get('documento'):
        archivo = request.FILES['documento']
        nombre_archivo = archivo.name.lower()

        try:
            if archivo.name.lower().endswith('.xml'):
                datos = procesar_xml_sunat(archivo)
                ruc_cli, rs_cli = datos['ruc_proveedor'], datos['razon_social_proveedor']
            else:
                datos = procesar_pdf_sunat(archivo)
                ruc_cli, rs_cli = datos.get('ruc_cliente'), datos.get('razon_social_cliente')

            cliente, _ = Entidad.objects.get_or_create(
                empresa=empresa, numero_documento=ruc_cli or "00000000",
                defaults={'nombre_razon_social': rs_cli or "CLIENTE VARIOS", 'tipo_entidad': 'Cliente'}
            )

            # --- NUEVA LÓGICA DE DUPLICADOS ---
            sn_partes = datos['serie_numero'].split('-')
            serie_doc = sn_partes[0]
            numero_doc = sn_partes[1] if len(sn_partes) > 1 else "0"

            factura_duplicada = Comprobante.objects.filter(
                empresa=empresa,
                entidad=cliente,
                serie=serie_doc,
                numero=numero_doc,
                operacion='Venta'
            ).exists()

            # --- CORRECCIÓN: Definir el catálogo de productos ---
            todos_los_productos = Producto.objects.filter(empresa=empresa).order_by('nombre_interno')

            items_procesados = []
            for item in datos['items']:
                producto_existente = Producto.objects.filter(
                    empresa=empresa, 
                    nombre_interno__icontains=item['descripcion'][:50]
                ).first()

                items_procesados.append({
                    'descripcion_xml': item['descripcion'],
                    'cantidad': float(item['cantidad']),
                    'precio_unitario': float(item['precio_unitario']),
                    'producto_id': producto_existente.id if producto_existente else None,
                    'nombre_sistema': producto_existente.nombre_interno if producto_existente else "NO ENCONTRADO"
                })

            request.session['temp_venta'] = {
                'cliente_id': cliente.id,
                'datos_doc': datos,
                'items_procesados': items_procesados
            }

            return render(request, 'core/confirmar_venta.html', {
                'cliente': cliente,
                'datos': datos,
                'items': items_procesados,
                'mis_productos': todos_los_productos,
                'ya_existe': factura_duplicada
            })

        except Exception as e:
            return render(request, 'core/cargar_venta.html', {'error': f'Error técnico: {str(e)}'})

    return render(request, 'core/cargar_venta.html')


@login_required
@transaction.atomic
def guardar_venta(request):
    if request.method == 'POST':
        temp_data = request.session.get('temp_venta')
        if not temp_data:
            return redirect('cargar_venta')

        empresa = Empresa.objects.get(id=request.session['empresa_id'])
        tc_manual = decimal.Decimal(request.POST.get('tipo_cambio', '1.000'))
        datos_doc = temp_data['datos_doc']
        cliente = Entidad.objects.get(id=temp_data['cliente_id'])
        
        partes = datos_doc['serie_numero'].split('-')
        total_decimal = decimal.Decimal(str(datos_doc['total']))

        # 1. Crear el Comprobante con TIPO DE CAMBIO (RF-18)
        venta = Comprobante.objects.create(
            empresa=empresa,
            entidad=cliente,
            tipo_documento='Factura',
            operacion='Venta',
            serie=partes[0],
            numero=partes[1] if len(partes) > 1 else "0",
            fecha_emision=datos_doc['fecha_emision'] or datetime.date.today(),
            moneda=datos_doc['moneda'],
            tipo_cambio=tc_manual, # <-- CORRECCIÓN: Guardar el TC manual
            subtotal=total_decimal / decimal.Decimal('1.18'),
            igv=total_decimal - (total_decimal / decimal.Decimal('1.18')),
            total=total_decimal,
            estado_sunat='ACEPTADO'
        )

        # 2. Procesar Items, Aprendizaje y Stock
        for i, item in enumerate(temp_data['items_procesados']):
            prod_id = request.POST.get(f'prod_id_{i}')
            nombre_largo_pdf = item['descripcion_xml']
            
            if prod_id:
                producto = Producto.objects.get(id=prod_id)
                
                # APRENDIZAJE: Si no conoce este nombre, lo guarda
                if nombre_largo_pdf not in producto.nombres_alternativos:
                    producto.nombres_alternativos.append(nombre_largo_pdf)
                    producto.save()
                    
                ComprobanteDetalle.objects.create(
                    comprobante=venta,
                    producto=producto,
                    cantidad=item['cantidad'],
                    precio_unitario=item['precio_unitario'],
                    subtotal_linea=decimal.Decimal(str(item['cantidad'])) * decimal.Decimal(str(item['precio_unitario']))
                )
                producto.stock_actual -= decimal.Decimal(str(item['cantidad']))
                producto.save()

        # 3. Crear Cuenta por Cobrar
        CuentaEstado.objects.create(
            comprobante=venta,
            monto_total=total_decimal,
            saldo_pendiente=total_decimal,
            fecha_vencimiento=datetime.date.today() + datetime.timedelta(days=30)
        )

        del request.session['temp_venta']
        return redirect('dashboard')

    return redirect('cargar_venta')

@login_required
@transaction.atomic
def registrar_cobranza(request, cuenta_id):
    cuenta = get_object_or_404(CuentaEstado, id=cuenta_id)
    
    if request.method == 'POST':
        monto_pagado = decimal.Decimal(request.POST.get('monto_pagado'))
        
        # Actualizar saldo
        cuenta.saldo_pendiente -= monto_pagado
        if cuenta.saldo_pendiente <= 0:
            cuenta.estado = 'Cancelado'
            cuenta.saldo_pendiente = 0
        else:
            cuenta.estado = 'Parcial'
        cuenta.save()
        
        # Aquí también deberías registrar un "Movimiento_Financiero" (Sección 2.3 MER)
        # para que el dinero entre a Caja/Bancos.
        
        return redirect('dashboard')
    
    return render(request, 'core/registrar_cobranza.html', {'cuenta': cuenta})

@login_required
def dashboard_analitico(request):
    emp_id = request.session.get('empresa_id')
    if not emp_id:
        return redirect('seleccionar_empresa')
    
    emp_id = int(emp_id)
    hoy = datetime.date.today()

    # --- 1. LÓGICA DE ARRASTRE (MES PASADO) ---
    fecha_mes_pasado = hoy.replace(day=1) - datetime.timedelta(days=1)
    periodo_mes_pasado = fecha_mes_pasado.strftime("%Y-%m")
    cierre_previo = CierreMensual.objects.filter(empresa_id=emp_id, periodo=periodo_mes_pasado, cerrado=True).first()
    saldo_arrastre = cierre_previo.saldo_a_favor_generado if cierre_previo else decimal.Decimal('0.00')

    # --- 2. LÓGICA MULTIMONEDA (BANCOS) ---
    tc_dia = TipoCambioDia.objects.filter(fecha=hoy).first()
    factor = tc_dia.venta if tc_dia else decimal.Decimal('3.75')
    cajas_empresa = Caja.objects.filter(empresa_id=emp_id)
    bancos_empresa = Cuenta_Bancaria.objects.filter(empresa_id=emp_id)
    saldo_pen = (cajas_empresa.filter(moneda='PEN').aggregate(Sum('saldo_actual'))['saldo_actual__sum'] or 0) + \
                (bancos_empresa.filter(moneda='PEN').aggregate(Sum('saldo_actual'))['saldo_actual__sum'] or 0)
    saldo_usd = (cajas_empresa.filter(moneda='USD').aggregate(Sum('saldo_actual'))['saldo_actual__sum'] or 0) + \
                (bancos_empresa.filter(moneda='USD').aggregate(Sum('saldo_actual'))['saldo_actual__sum'] or 0)
    saldo_bruto = decimal.Decimal(str(saldo_pen)) + (decimal.Decimal(str(saldo_usd)) * factor)

    # --- 3. VENTAS ---
    v_pen = Comprobante.objects.filter(empresa_id=emp_id, operacion='Venta', moneda='PEN')
    v_usd = Comprobante.objects.filter(empresa_id=emp_id, operacion='Venta', moneda='USD')
    total_ventas_pen = v_pen.aggregate(Sum('total'))['total__sum'] or 0
    total_ventas_usd = v_usd.aggregate(Sum('total'))['total__sum'] or 0

    # --- 4. CONVERSIÓN VENTAS (MARGEN E IGV) ---
    v_subtotal_total_convertido = (v_pen.aggregate(Sum('subtotal'))['subtotal__sum'] or 0) + \
                                  (v_usd.aggregate(res=Sum(F('subtotal') * F('tipo_cambio')))['res'] or 0)
    v_fiscales_pen = v_pen.exclude(tipo_documento='Recibo').exclude(estado_sunat='INTERNO')
    v_fiscales_usd = v_usd.exclude(tipo_documento='Recibo').exclude(estado_sunat='INTERNO')
    v_igv_total_convertido = (v_fiscales_pen.aggregate(Sum('igv'))['igv__sum'] or 0) + \
                             (v_fiscales_usd.aggregate(res=Sum(F('igv') * F('tipo_cambio')))['res'] or 0)

    # --- 5. COMPRAS Y FLETES ---
    compras_reales_q = Comprobante.objects.filter(empresa_id=emp_id, operacion='Compra', es_flete=False, es_escudo_tributario=False)
    total_compras_pen = compras_reales_q.aggregate(res=Sum(F('total') * F('tipo_cambio')))['res'] or 0
    c_subtotal_pen_real = compras_reales_q.aggregate(res=Sum(F('subtotal') * F('tipo_cambio')))['res'] or 0
    compras_todas_q = Comprobante.objects.filter(empresa_id=emp_id, operacion='Compra').exclude(tipo_documento='Recibo')
    c_igv_total_para_sunat = compras_todas_q.aggregate(res=Sum(F('igv') * F('tipo_cambio')))['res'] or 0
    total_fletes = Comprobante.objects.filter(empresa_id=emp_id, es_flete=True, es_escudo_tributario=False).aggregate(res=Sum(F('subtotal') * F('tipo_cambio')))['res'] or 0
    total_intereses = Prestamo.objects.filter(empresa_id=emp_id).aggregate(res=Sum(F('monto_interes') * F('comprobante__tipo_cambio')))['res'] or 0

    # --- 6. MARGEN Y PROYECCIÓN IGV BRUTA ---
    margen_neto_real = v_subtotal_total_convertido - (c_subtotal_pen_real + total_fletes + total_intereses)
    proyeccion_igv = v_igv_total_convertido - c_igv_total_para_sunat

    # --- 7. PAGOS REALIZADOS, RETENCIONES Y AJUSTES (ESTO ES LO QUE ESTABA ABAJO) ---
    # 7.1 Retenciones
    total_retenciones_acumuladas = CertificadoRetencion.objects.filter(empresa_id=emp_id).aggregate(Sum('monto_total_pen'))['monto_total_pen__sum'] or decimal.Decimal('0.00')
    # 7.2 Pagos 1662
    total_pagos_sunat_igv = PagoImpuesto.objects.filter(empresa_id=emp_id, tributo_codigo='1011').aggregate(Sum('monto_pagado'))['monto_pagado__sum'] or decimal.Decimal('0.00')
    # 7.3 Diferencia de Cambio e ITF
    total_diff_cambio = MovimientoFinanciero.objects.filter(empresa_id=emp_id).aggregate(Sum('diferencia_cambio_soles'))['diferencia_cambio_soles__sum'] or decimal.Decimal('0.00')
    total_itf_pagado = MovimientoFinanciero.objects.filter(empresa_id=emp_id).aggregate(Sum('itf_monto'))['itf_monto__sum'] or 0

    # --- 8. CÁLCULOS FINALES ---
    monto_real_pago_sunat = proyeccion_igv - total_retenciones_acumuladas - total_pagos_sunat_igv - saldo_arrastre
    utilidad_final_con_tc = margen_neto_real + total_diff_cambio
    
    total_deuda_prestamos_pen = Prestamo.objects.filter(empresa_id=emp_id, estado='Pendiente').aggregate(res=Sum(F('monto_capital') * F('comprobante__tipo_cambio')))['res'] or 0
    saldo_neto = saldo_bruto - total_deuda_prestamos_pen

    por_cobrar = CuentaEstado.objects.filter(comprobante__empresa_id=emp_id, comprobante__operacion='Venta').aggregate(res=Sum(F('saldo_pendiente') * F('comprobante__tipo_cambio')))['res'] or 0
    por_pagar = CuentaEstado.objects.filter(comprobante__empresa_id=emp_id, comprobante__operacion='Compra', comprobante__es_escudo_tributario=False).aggregate(res=Sum(F('saldo_pendiente') * F('comprobante__tipo_cambio')))['res'] or 0

    context = {
        'saldo_bruto': float(saldo_bruto),
        'saldo_neto': float(saldo_neto),
        'total_ventas_pen': float(total_ventas_pen),
        'total_ventas_usd': float(total_ventas_usd),
        'total_compras': float(total_compras_pen),
        'margen_neto_real': float(margen_neto_real),
        'proyeccion_igv': float(proyeccion_igv),
        'por_cobrar': float(por_cobrar),
        'por_pagar': float(por_pagar),
        'total_fletes': float(total_fletes),
        'total_intereses': float(total_intereses),
        'cajas': cajas_empresa,
        'bancos': bancos_empresa,
        'total_retenciones': float(total_retenciones_acumuladas),
        'total_pagos_sunat': float(total_pagos_sunat_igv),
        'pago_final_sunat': float(monto_real_pago_sunat),
        'pago_final_sunat_abs': abs(float(monto_real_pago_sunat)), 
        'total_diff_cambio': float(total_diff_cambio),
        'utilidad_final_real': float(utilidad_final_con_tc),
        'periodo_actual': hoy.strftime("%Y-%m"),
        'saldo_arrastre': float(saldo_arrastre),
        'factor_tc': factor,
        'total_itf': float(total_itf_pagado),
    }
    return render(request, 'core/dashboard.html', context)

@login_required
@admin_required
def eliminar_comprobante(request, pk):
    if request.user.rol.nombre != 'Admin':
        return redirect('dashboard') # Solo Admins (RF-24)

    comprobante = get_object_or_404(Comprobante, id=pk, empresa_id=request.session['empresa_id'])
    
    # --- CALCULAMOS EL IMPACTO PARA EL MENSAJE ---
    impacto = {
        'cuotas': Cuota.objects.filter(cuenta__comprobante=comprobante).count(),
        'pagos': MovimientoFinanciero.objects.filter(comprobante=comprobante).count(),
        'fletes_hijos': Comprobante.objects.filter(comprobante_asociado=comprobante).count(),
        'prestamos': Prestamo.objects.filter(comprobante=comprobante).count(),
    }

    if request.method == 'POST':
        motivo = request.POST.get('motivo')
        
        # Auditoría
        LogAuditoria.objects.create(
            usuario=request.user,
            empresa_id=request.session['empresa_id'],
            accion='DELETE',
            tabla_afectada='Comprobante',
            referencia_id=comprobante.id,
            motivo_cambio=f"BORRADO ATÓMICO: {comprobante.codigo_factura}. Motivo: {motivo}"
        )
        
        # El signal pre_delete que ya tenemos se encargará de revertir stock y bancos
        comprobante.delete()
        return redirect('lista_comprobantes')

    return render(request, 'core/confirmar_borrado.html', {
        'obj': comprobante,
        'impacto': impacto # Pasamos los números al HTML
    })

@login_required
@transaction.atomic
def registrar_devolucion(request):
    emp_id = request.session.get('empresa_id')
    
    if request.method == 'POST':
        producto = get_object_or_404(Producto, id=request.POST.get('producto_id'), empresa_id=emp_id)
        cantidad = decimal.Decimal(request.POST.get('cantidad'))
        monto_reembolso = decimal.Decimal(request.POST.get('monto_reembolso'))

        # 1. Ajustar Inventario
        producto.stock_actual -= cantidad
        producto.save()

        # 2. Registrar Ingreso a Caja (Módulo 7)
        MovimientoFinanciero.objects.create(
            empresa_id=emp_id,
            tipo='Ingreso',
            monto=monto_reembolso,
            moneda='USD',
            referencia=f"Devolución de {cantidad} unidades de {producto.nombre_interno}"
        )

        return redirect('dashboard')

    productos = Producto.objects.filter(empresa_id=emp_id)
    return render(request, 'core/registrar_devolucion.html', {'productos': productos})

@login_required
def validar_sunat(request, comprobante_id):
    # Buscamos el comprobante asegurando que sea de la empresa activa
    comprobante = get_object_or_404(Comprobante, id=comprobante_id, empresa_id=request.session['empresa_id'])
    
    # Aquí en el futuro conectarás con un buscador de SUNAT
    # Por ahora, simulamos que el sistema lo marca como ACEPTADO (RF-23)
    comprobante.estado_sunat = 'ACEPTADO'
    comprobante.save()
    
    return redirect('dashboard')

@login_required
@transaction.atomic
def transferir_moneda(request):
    emp_id = request.session.get('empresa_id')
    
    if request.method == 'POST':
        monto_origen = decimal.Decimal(request.POST.get('monto'))
        tc_pactado = decimal.Decimal(request.POST.get('tipo_cambio'))
        sentido = request.POST.get('sentido')
        
       # --- NUEVO: Capturar las cuentas ---
        cuenta_origen_id = request.POST.get('cuenta_origen')
        cuenta_destino_id = request.POST.get('cuenta_destino')
        
        if sentido == 'PEN_TO_USD':
            monto_destino = monto_origen / tc_pactado
            # 1. Sale Soles de la cuenta origen
            MovimientoFinanciero.objects.create(
                empresa_id=emp_id, tipo='Egreso', monto=monto_origen, moneda='PEN', 
                referencia=f"Cambio a USD (TC: {tc_pactado})",
                cuenta_bancaria_id=cuenta_origen_id # <--- AMARRE
            )
            # 2. Entra Dólares a la cuenta destino
            MovimientoFinanciero.objects.create(
                empresa_id=emp_id, tipo='Ingreso', monto=monto_destino, moneda='USD', 
                referencia="Ingreso por cambio de moneda",
                cuenta_bancaria_id=cuenta_destino_id # <--- AMARRE
            )
        
        elif sentido == 'USD_TO_PEN':
            monto_destino = monto_origen * tc_pactado
            # 1. Sale Dólares
            MovimientoFinanciero.objects.create(
                empresa_id=emp_id, tipo='Egreso', monto=monto_origen, moneda='USD', 
                referencia=f"Cambio a PEN (TC: {tc_pactado})",
                cuenta_bancaria_id=cuenta_origen_id # <--- AMARRE
            )
            # 2. Entra Soles
            MovimientoFinanciero.objects.create(
                empresa_id=emp_id, tipo='Ingreso', monto=monto_destino, moneda='PEN', 
                referencia="Ingreso por cambio de moneda",
                cuenta_bancaria_id=cuenta_destino_id # <--- AMARRE
            )
        
        return redirect('dashboard')

    # Pasamos las cuentas para que el usuario las elija en el HTML
    cuentas = Cuenta_Bancaria.objects.filter(empresa_id=emp_id)
    return render(request, 'core/transferir_moneda.html', {'cuentas': cuentas})

@login_required
def lista_comprobantes(request):
    emp_id = request.session.get('empresa_id')
    tipo = request.GET.get('tipo') # Puede ser 'Compra' o 'Venta'
    
    comprobantes = Comprobante.objects.filter(empresa_id=emp_id)
    if tipo:
        comprobantes = comprobantes.filter(operacion=tipo)
        
    return render(request, 'core/lista_comprobantes.html', {
        'comprobantes': comprobantes,
        'titulo': f"Listado de {tipo}s" if tipo else "Todos los Comprobantes"
    })

@login_required
@transaction.atomic
def configurar_cuotas(request, cuenta_id):
    cuenta = get_object_or_404(CuentaEstado, id=cuenta_id)
    
    if request.method == 'POST':
        num_cuotas = int(request.POST.get('num_cuotas'))
        monto_cuota = cuenta.monto_total / num_cuotas
        frecuencia_dias = int(request.POST.get('frecuencia')) # Ej: cada 30 días
        
        # Borrar cuotas previas si existen (por si el usuario se equivoca y reintenta)
        cuenta.cuotas.all().delete()
        
        fecha_base = datetime.date.today()
        
        for i in range(1, num_cuotas + 1):
            fecha_venc = fecha_base + datetime.timedelta(days=frecuencia_dias * i)
            Cuota.objects.create(
                cuenta=cuenta,
                numero_cuota=i,
                monto=monto_cuota,
                fecha_vencimiento=fecha_venc
            )
        
        # Actualizamos la fecha de vencimiento de la cuenta al de la última cuota
        cuenta.fecha_vencimiento = fecha_venc
        cuenta.estado = 'Pendiente'
        cuenta.save()
        
        return redirect('dashboard')

    return render(request, 'core/configurar_cuotas.html', {'cuenta': cuenta})

@login_required
@transaction.atomic
def configurar_cuotas_prestamo(request, prestamo_id):
    prestamo = get_object_or_404(Prestamo, id=prestamo_id, empresa_id=request.session['empresa_id'])
    
    if request.method == 'POST':
        num_cuotas = int(request.POST.get('num_cuotas'))
        # La deuda total del préstamo es Capital + Interés
        monto_total = prestamo.monto_capital + prestamo.monto_interes
        monto_cuota = monto_total / num_cuotas
        frecuencia_dias = int(request.POST.get('frecuencia'))
        
        # Limpiar programaciones anteriores
        prestamo.cuotas.all().delete()
        
        fecha_base = prestamo.fecha_prestamo
        for i in range(1, num_cuotas + 1):
            fecha_venc = fecha_base + datetime.timedelta(days=frecuencia_dias * i)
            Cuota.objects.create(
                prestamo=prestamo,
                numero_cuota=i,
                monto=monto_cuota,
                fecha_vencimiento=fecha_venc
            )
        
        return redirect('lista_prestamos')

    return render(request, 'core/configurar_cuotas_prestamo.html', {'prestamo': prestamo})

@login_required
@transaction.atomic
@admin_required
def editar_comprobante(request, pk):
    emp_id = request.session.get('empresa_id')
    comprobante = get_object_or_404(Comprobante, id=pk, empresa_id=emp_id)
    empresa = comprobante.empresa

    if request.method == 'POST':
        # 1. CAPTURAR DATOS
        ruc_dni = request.POST.get('ruc_dni')
        razon_social = request.POST.get('razon_social')
        fecha = request.POST.get('fecha')
        moneda = request.POST.get('moneda')
        # Cambiamos el tipo de documento para saber si calculamos IGV o no
        tipo_doc = request.POST.get('tipo_documento', comprobante.tipo_documento)
        tc_nuevo = decimal.Decimal(request.POST.get('tipo_cambio', '1.000'))
        motivo_audit = request.POST.get('motivo_cambio', 'Edición integral')

        # 2. REVERTIR STOCK (Deshacer lo que hizo la versión vieja)
        for det in comprobante.detalles.all():
            if det.producto:
                if comprobante.operacion == 'Compra':
                    det.producto.stock_actual -= det.cantidad
                else: # Venta
                    det.producto.stock_actual += det.cantidad
                det.producto.save() # Guardamos la reversión

        # 3. ACTUALIZAR CABECERA
        entidad, _ = Entidad.objects.get_or_create(
            empresa=empresa, numero_documento=ruc_dni,
            defaults={'nombre_razon_social': razon_social, 'tipo_entidad': 'Ambos'}
        )
        
        instancia_vieja = copy.copy(comprobante)
        
        comprobante.entidad = entidad
        comprobante.fecha_emision = fecha
        comprobante.moneda = moneda
        comprobante.tipo_cambio = tc_nuevo
        comprobante.tipo_documento = tipo_doc
        comprobante.save()

        # 4. ACTUALIZAR DETALLES
        comprobante.detalles.all().delete()
        
        prod_ids = request.POST.getlist('prod_id[]')
        descs = request.POST.getlist('desc[]')
        cants = request.POST.getlist('cant[]')
        precs = request.POST.getlist('prec[]')

        nuevo_total = decimal.Decimal('0.00')
        
        for i in range(len(descs)):
            c = decimal.Decimal(cants[i] if cants[i] else 0)
            p = decimal.Decimal(precs[i] if precs[i] else 0)
            sub = c * p
            nuevo_total += sub
            
            producto = Producto.objects.filter(id=prod_ids[i]).first() if i < len(prod_ids) and prod_ids[i] else None
            
            ComprobanteDetalle.objects.create(
                comprobante=comprobante, producto=producto,
                descripcion_libre=descs[i], cantidad=c, 
                precio_unitario=p, subtotal_linea=sub
            )
            
            # APLICAR NUEVO STOCK (Reflejo inmediato en Control de Inventario)
            if producto:
                if comprobante.operacion == 'Compra':
                    producto.stock_actual += c
                    producto.precio_compra_referencial = p * tc_nuevo
                else: # Venta
                    producto.stock_actual -= c
                    
                    if comprobante.moneda == 'PEN':
                        producto.precio_venta_referencial = p
                    else:
                        producto.precio_venta_referencial = p * tc_nuevo
                
                producto.save()

        # 5. LÓGICA DE IGV INTELIGENTE (Solución a tu observación 2)
        comprobante.total = nuevo_total
        
        if tipo_doc in ['Factura', 'Boleta']:
            # Son documentos fiscales: desglosamos IGV
            comprobante.subtotal = nuevo_total / decimal.Decimal('1.18')
            comprobante.igv = nuevo_total - comprobante.subtotal
        else:
            # Es Recibo u Otros (AliExpress/Manual): IGV es CERO
            comprobante.subtotal = nuevo_total
            comprobante.igv = decimal.Decimal('0.00')
        
        comprobante.save()

        # 6. SINCRONIZAR DEUDA
        cuenta = comprobante.cuenta_estado.first()
        if cuenta:
            cuenta.monto_total = nuevo_total
            cuenta.saldo_pendiente = nuevo_total
            cuenta.save()

        registrar_auditoria_update(request.user, instancia_vieja, comprobante, motivo_audit)
        return redirect('lista_comprobantes')

    mis_productos = Producto.objects.filter(empresa=empresa).order_by('nombre_interno')
    return render(request, 'core/comprobante_edit_form.html', {
        'c': comprobante,
        'mis_productos': mis_productos,
    })

@login_required
def lista_entidades(request):
    emp_id = request.session.get('empresa_id')
    entidades = Entidad.objects.filter(empresa_id=emp_id)
    return render(request, 'core/entidades_list.html', {'entidades': entidades})


@login_required
def crear_entidad(request):
    if request.method == 'POST':
        Entidad.objects.create(
            empresa_id=request.session.get('empresa_id'),
            tipo_entidad=request.POST.get('tipo_entidad'),
            tipo_documento=request.POST.get('tipo_documento'),
            numero_documento=request.POST.get('numero_documento'),
            nombre_razon_social=request.POST.get('nombre_razon_social'),
            direccion=request.POST.get('direccion')
        )
        return redirect('lista_entidades')
    return render(request, 'core/entidad_form.html')

@login_required
def detalle_entidad(request, pk):
    emp_id = request.session.get('empresa_id')
    entidad = get_object_or_404(Entidad, id=pk, empresa_id=emp_id)
    
    # 1. Obtenemos todos sus comprobantes
    comprobantes = Comprobante.objects.filter(entidad=entidad, empresa_id=emp_id).order_by('-fecha_emision')

    # 2. Calculamos el Volumen de Negocio (Total de lo facturado históricamente)
    volumen_pen = comprobantes.filter(moneda='PEN').aggregate(Sum('total'))['total__sum'] or 0
    volumen_usd = comprobantes.filter(moneda='USD').aggregate(Sum('total'))['total__sum'] or 0

    # 3. Calculamos la Deuda Pendiente Actual (Lo que falta cobrar o pagar)
    deudas = CuentaEstado.objects.filter(comprobante__entidad=entidad, comprobante__empresa_id=emp_id)
    
    deuda_pen = deudas.filter(comprobante__moneda='PEN').aggregate(Sum('saldo_pendiente'))['saldo_pendiente__sum'] or 0
    deuda_usd = deudas.filter(comprobante__moneda='USD').aggregate(Sum('saldo_pendiente'))['saldo_pendiente__sum'] or 0

    # 4. Traemos los últimos 10 movimientos para la tabla
    ultimos_movimientos = comprobantes[:10]

    return render(request, 'core/entidad_detalle.html', {
        'e': entidad,
        'v_pen': volumen_pen,
        'v_usd': volumen_usd,
        'd_pen': deuda_pen,
        'd_usd': deuda_usd,
        'movimientos': ultimos_movimientos
    })

@login_required
def editar_entidad(request, pk):
    # Buscamos la entidad asegurándonos que sea de la empresa actual
    emp_id = request.session.get('empresa_id')
    entidad = get_object_or_404(Entidad, id=pk, empresa_id=emp_id)
    
    if request.method == 'POST':
        entidad.tipo_entidad = request.POST.get('tipo_entidad')
        entidad.tipo_documento = request.POST.get('tipo_documento')
        entidad.numero_documento = request.POST.get('numero_documento')
        entidad.nombre_razon_social = request.POST.get('nombre_razon_social')
        entidad.direccion = request.POST.get('direccion')
        entidad.save()
        return redirect('lista_entidades')
        
    return render(request, 'core/entidad_edit_form.html', {'e': entidad})

@login_required
def eliminar_entidad(request, pk):
    emp_id = request.session.get('empresa_id')
    entidad = get_object_or_404(Entidad, id=pk, empresa_id=emp_id)
    
    # --- CUCHILLA DE SEGURIDAD ---
    # Verificamos si tiene comprobantes (facturas/boletas) asociados
    tiene_movimientos = Comprobante.objects.filter(entidad=entidad).exists()
    
    if tiene_movimientos:
        # Aquí podrías usar messages de Django, pero por ahora redirigimos con un aviso simple
        # En una fase siguiente podemos poner una alerta roja en el listado
        return redirect('lista_entidades')
        
    entidad.delete()
    return redirect('lista_entidades')

# --- MANTENIMIENTO DE INVENTARIO (RF-07) ---
@login_required
def lista_productos(request):
    emp_id = request.session.get('empresa_id')
    productos = Producto.objects.filter(empresa_id=emp_id)
    return render(request, 'core/productos_list.html', {'productos': productos})

@login_required
def lista_categorias(request):
    # --- LÓGICA DE GUARDADO (Esto es lo que faltaba) ---
    if request.method == 'POST':
        nombre_cat = request.POST.get('nombre')
        if nombre_cat:
            # Usamos get_or_create para evitar que crees dos veces la misma categoría
            CategoriaProducto.objects.get_or_create(nombre=nombre_cat)
            return redirect('lista_categorias') # Refresca para mostrar la nueva
    # --------------------------------------------------

    # Lógica de listado (GET)
    categorias = CategoriaProducto.objects.all().order_by('nombre')
    return render(request, 'core/categorias_list.html', {'categorias': categorias})

@login_required
def editar_categoria(request, pk):
    categoria = get_object_or_404(CategoriaProducto, id=pk)
    
    if request.method == 'POST':
        categoria.nombre = request.POST.get('nombre')
        categoria.save()
        return redirect('lista_categorias')
        
    return render(request, 'core/categoria_edit_form.html', {'categoria': categoria})

@login_required
def eliminar_categoria(request, pk):
    categoria = get_object_or_404(CategoriaProducto, id=pk)
    # Al eliminar, los productos que tenían esta categoría pasarán a "Sin Categoría"
    # automáticamente por el SET_NULL que pusimos en el modelo.
    categoria.delete()
    return redirect('lista_categorias')

# --- MÓDULO DE CAJA Y BANCOS ---
@login_required
def lista_movimientos(request):
    emp_id = request.session.get('empresa_id')
    movimientos = MovimientoFinanciero.objects.filter(empresa_id=emp_id).order_by('-fecha')
    return render(request, 'core/movimientos_list.html', {'movimientos': movimientos})

# --- MÓDULO DE GASTOS ---
@login_required
def lista_gastos(request):
    emp_id = request.session.get('empresa_id')
    gastos = GastoOperativo.objects.filter(empresa_id=emp_id).order_by('-fecha')
    return render(request, 'core/gastos_list.html', {'gastos': gastos})

# --- MÓDULO DE TIPO DE CAMBIO ---
@login_required
def lista_tipo_cambio(request):
    historial = TipoCambioDia.objects.all().order_by('-fecha')
    return render(request, 'core/tipo_cambio_list.html', {'historial': historial})

# --- MÓDULO DE AUDITORÍA (Solo para Admin - RNF-02) ---
@login_required
@admin_required # Solo el admin ve la caja negra
def lista_auditoria(request):
    # Forzamos a que el ID sea un entero para el filtro
    emp_id = int(request.session.get('empresa_id'))
    
    # Traemos los logs de esta empresa
    logs = LogAuditoria.objects.filter(empresa_id=emp_id).order_by('-fecha_hora')
    
    print(f"DEBUG: Buscando logs para empresa {emp_id}. Encontrados: {logs.count()}") # Mira tu terminal
    
    return render(request, 'core/auditoria_list.html', {'logs': logs})

# core/views.py

@login_required
def lista_prestamos(request):
    emp_id = request.session.get('empresa_id')
    # Obtenemos todos los préstamos de la empresa seleccionada
    prestamos = Prestamo.objects.filter(empresa_id=emp_id).order_by('-fecha_prestamo')
    
    # Calculamos totales para un mini-resumen arriba
    total_capital = prestamos.filter(estado='Pendiente').aggregate(Sum('monto_capital'))['monto_capital__sum'] or 0
    total_intereses = prestamos.filter(estado='Pendiente').aggregate(Sum('monto_interes'))['monto_interes__sum'] or 0
    
    return render(request, 'core/prestamos_list.html', {
        'prestamos': prestamos,
        'total_capital': total_capital,
        'total_intereses': total_intereses,
        'deuda_total': total_capital + total_intereses
    })

@login_required
@transaction.atomic
def registrar_pago_comprobante(request, pk):
    comprobante = get_object_or_404(Comprobante, id=pk, empresa_id=request.session['empresa_id'])
    cuenta = comprobante.cuenta_estado.first()
    
    # Filtramos cajas/bancos que tengan la misma moneda que la factura
    cajas = Caja.objects.filter(empresa=comprobante.empresa, moneda=comprobante.moneda)
    bancos = Cuenta_Bancaria.objects.filter(empresa=comprobante.empresa, moneda=comprobante.moneda)

    if request.method == 'POST':
        # 1. Capturamos los datos del formulario
        monto_pago = decimal.Decimal(request.POST.get('monto', '0'))
        tc_pago = decimal.Decimal(request.POST.get('tipo_cambio_pago', '1.000'))
        itf_pago = decimal.Decimal(request.POST.get('itf_monto', '0.00'))
        caja_id = request.POST.get('caja_id')
        banco_id = request.POST.get('banco_id')
        referencia = request.POST.get('referencia', '')

        # 2. CÁLCULO DE DIFERENCIA DE CAMBIO (Antes de crear el movimiento)
        diff_cambio_soles = decimal.Decimal('0.00')
        if comprobante.moneda == 'USD':
            tc_factura = comprobante.tipo_cambio
            valor_pen_esperado = monto_pago * tc_factura
            valor_pen_real = monto_pago * tc_pago
            
            if comprobante.operacion == 'Venta': # Cobranza
                diff_cambio_soles = valor_pen_real - valor_pen_esperado
            else: # Pago
                diff_cambio_soles = valor_pen_esperado - valor_pen_real

        # 3. CREAR EL MOVIMIENTO FINANCIERO (Solo uno y con todos los datos)
        mov = MovimientoFinanciero.objects.create(
            empresa=comprobante.empresa,
            tipo='Egreso' if comprobante.operacion == 'Compra' else 'Ingreso',
            monto=monto_pago,
            moneda=comprobante.moneda,
            tipo_cambio_operacion=tc_pago,
            diferencia_cambio_soles=diff_cambio_soles,
            itf_monto=itf_pago,
            referencia=f"{'Pago' if comprobante.operacion == 'Compra' else 'Cobro'} de {comprobante.codigo_factura}: {referencia}",
            comprobante=comprobante,
            caja_id=caja_id if caja_id else None,
            cuenta_bancaria_id=banco_id if banco_id else None
        )

        # 4. REGISTRAR EN LOG DE AUDITORÍA (RNF-02)
        LogAuditoria.objects.create(
            usuario=request.user,
            empresa_id=int(request.session['empresa_id']),
            accion='INSERT',
            tabla_afectada='Pago/Cobranza',
            referencia_id=comprobante.id,
            motivo_cambio=f"Registro de {mov.tipo} por {comprobante.moneda} {monto_pago}. Ref: {referencia}. Dif. Cambio: S/ {diff_cambio_soles}"
        )

        # 5. ACTUALIZAR DEUDA (RF-16)
        if cuenta:
            cuenta.saldo_pendiente -= monto_pago
            if cuenta.saldo_pendiente <= 0:
                cuenta.estado = 'Cancelado'
                cuenta.saldo_pendiente = 0
            else:
                cuenta.estado = 'Parcial'
            cuenta.save()

        return redirect('lista_comprobantes')

    return render(request, 'core/registrar_pago.html', {
        'comprobante': comprobante,
        'cuenta': cuenta,
        'cajas': cajas,
        'bancos': bancos
    })


@login_required
def ver_comprobante_detalle(request, pk): # <--- Nombre Universal
    comprobante = get_object_or_404(Comprobante, id=pk, empresa_id=request.session['empresa_id'])
    empresa = comprobante.empresa
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)

    subtotal = comprobante.subtotal
    igv = comprobante.igv
    
    return render(request, 'core/comprobante_universal_view.html', {
        'c': comprobante,
        'empresa': empresa,
        'bancos': bancos,
        'subtotal': subtotal,
        'igv': igv,
        'mostrar_igv': (igv > 0)
    })



@login_required
def cargar_retencion(request):
    empresa_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == 'POST' and request.FILES.get('xml_retencion'):
        archivo = request.FILES['xml_retencion']
        
        try:
            datos = procesar_xml_retencion(archivo)
            
            # 1. Buscamos al Agente de Retención (Cliente)
            agente = Entidad.objects.filter(empresa=empresa, numero_documento=datos['ruc_agente']).first()

            # --- 2. LÓGICA DE DUPLICADOS (CORREGIDA: SE HACE UNA SOLA VEZ AQUÍ) ---
            # Verificamos si este certificado (Serie-Número) ya fue guardado para este agente
            ya_existe = CertificadoRetencion.objects.filter(
                empresa=empresa,
                agente_retencion=agente,
                serie_numero=datos['serie_numero']
            ).exists() if agente else False
            # ---------------------------------------------------------------------

            # 3. Realizamos el "Match" de las facturas que vienen dentro del certificado
            lineas_procesadas = []
            for linea in datos['lineas']:
                # Dividimos Serie-Número de la factura referenciada
                partes = linea['factura_ref'].split('-')
                factura = Comprobante.objects.filter(
                    empresa=empresa, 
                    serie=partes[0], 
                    numero=partes[1], 
                    operacion='Venta'
                ).first()

                # Buscamos el saldo actual para mostrarlo en la tabla informativa
                saldo = 0
                if factura:
                    cuenta = factura.cuenta_estado.first()
                    saldo = float(cuenta.saldo_pendiente) if cuenta else 0

                lineas_procesadas.append({
                    'factura_ref': linea['factura_ref'],
                    'monto_pen': linea['monto_pen'],
                    'monto_moneda_origen': linea['monto_moneda_origen'],
                    'tipo_cambio': linea['tipo_cambio'],
                    'factura_id': factura.id if factura else None,
                    'saldo_actual_factura': saldo
                })

            # 4. Guardamos en sesión para el paso final de guardado
            request.session['temp_retencion'] = {
                'ruc_agente': datos['ruc_agente'],
                'nombre_agente': datos['nombre_agente'],
                'serie_numero': datos['serie_numero'],
                'fecha_emision': datos['fecha_emision'],
                'monto_total': datos['monto_total_retencion'],
                'lineas': lineas_procesadas
            }

            return render(request, 'core/confirmar_retencion.html', {
                'agente': agente or datos['nombre_agente'],
                'datos': datos,
                'lineas': lineas_procesadas,
                'ya_existe': ya_existe # Pasamos el veredicto al template
            })

        except Exception as e:
            return render(request, 'core/cargar_retencion.html', {'error': f"Error técnico: {str(e)}"})

    return render(request, 'core/cargar_retencion.html')

@login_required
@transaction.atomic
def guardar_retencion(request):
    if request.method == 'POST':
        temp = request.session.get('temp_retencion')
        if not temp: return redirect('cargar_retencion')

        empresa = Empresa.objects.get(id=request.session['empresa_id'])
        
        # 1. Crear Cabecera del Certificado
        agente, _ = Entidad.objects.get_or_create(
            empresa=empresa, numero_documento=temp['ruc_agente'],
            defaults={'nombre_razon_social': temp['nombre_agente'], 'tipo_entidad': 'Cliente'}
        )
        
        certificado = CertificadoRetencion.objects.create(
            empresa=empresa,
            agente_retencion=agente,
            serie_numero=temp['serie_numero'],
            fecha_emision=temp['fecha_emision'],
            monto_total_pen=temp['monto_total']
        )

        # 2. Procesar Detalles y Matar Deudas
        for linea in temp['lineas']:
            if linea['factura_id']:
                factura = Comprobante.objects.get(id=linea['factura_id'])
                cuenta = factura.cuenta_estado.first()

                # Guardar el detalle de la retención
                RetencionDetalle.objects.create(
                    certificado=certificado,
                    comprobante=factura,
                    monto_retencion_pen=linea['monto_pen'],
                    monto_descuento_moneda_origen=linea['monto_moneda_origen'],
                    tipo_cambio_aplicado=linea['tipo_cambio']
                )

                # DESCUENTO REAL DE LA DEUDA
                cuenta.saldo_pendiente -= decimal.Decimal(str(linea['monto_moneda_origen']))
                if cuenta.saldo_pendiente <= 0:
                    cuenta.estado = 'Cancelado'
                    cuenta.saldo_pendiente = 0
                else:
                    cuenta.estado = 'Parcial'
                cuenta.save()

        del request.session['temp_retencion']
        return redirect('dashboard')
    
    return redirect('cargar_retencion')


@login_required
@transaction.atomic
def registrar_compra_manual(request):
    emp_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=emp_id)
    
    if request.method == 'POST':
        # 1. Datos de Cabecera
        ruc_dni = request.POST.get('ruc_dni')
        razon_social = request.POST.get('razon_social')
        tipo_doc = request.POST.get('tipo_documento', 'Otros')
        serie_num = request.POST.get('serie_numero')
        fecha = request.POST.get('fecha')
        moneda = request.POST.get('moneda')
        tc = decimal.Decimal(request.POST.get('tipo_cambio', '1.000'))
        
        # 2. Proveedor
        proveedor, _ = Entidad.objects.get_or_create(
            empresa=empresa, numero_documento=ruc_dni,
            defaults={'nombre_razon_social': razon_social, 'tipo_entidad': 'Proveedor'}
        )
        
        # 3. Comprobante
        monto_total = decimal.Decimal('0.00')
        comprobante = Comprobante.objects.create(
            empresa=empresa, entidad=proveedor, tipo_documento=tipo_doc,
            operacion='Compra', serie='MAN', numero=serie_num,
            fecha_emision=fecha, moneda=moneda, tipo_cambio=tc,
            subtotal=0, igv=0, total=0
        )

        # 4. Procesar los Productos
        descripciones = request.POST.getlist('desc[]')
        cantidades = request.POST.getlist('cant[]')
        precios = request.POST.getlist('prec[]')
        prod_ids = request.POST.getlist('prod_id[]')
        precios_venta = request.POST.getlist('prec_venta[]')

        for i in range(len(descripciones)):
            cant = decimal.Decimal(cantidades[i])
            prec_unit = decimal.Decimal(precios[i])
            p_v_sug = decimal.Decimal(precios_venta[i] if i < len(precios_venta) and precios_venta[i] else '0.00')
            subtotal_fila = cant * prec_unit
            monto_total += subtotal_fila
            
            # --- MEJORA: Búsqueda inteligente para no duplicar productos ---
            producto = None
            if i < len(prod_ids) and prod_ids[i]:
                producto = Producto.objects.filter(id=prod_ids[i]).first()
            
            if not producto:
                # Si no se eligió del buscador, intentamos buscar por nombre exacto
                producto = Producto.objects.filter(empresa=empresa, nombre_interno__iexact=descripciones[i]).first()

            if not producto:
                # Si sigue sin existir, recién lo creamos
                producto = Producto.objects.create(
                    empresa=empresa,
                    sku=f"MAN-{uuid.uuid4().hex[:5].upper()}",
                    nombre_interno=descripciones[i],
                    precio_compra_referencial=prec_unit * tc
                )
            # ---------------------------------------------------------------

            # ASIGNAR PRECIO DE VENTA Y SUMAR STOCK (UNA SOLA VEZ)
            producto.precio_venta_referencial = p_v_sug
            producto.stock_actual += cant # <--- SOLO ESTA LÍNEA
            producto.save()   
            
            # Crear detalle
            ComprobanteDetalle.objects.create(
                comprobante=comprobante, producto=producto,
                cantidad=cant, precio_unitario=prec_unit, subtotal_linea=subtotal_fila
            )

        # 5. Finalizar montos
        comprobante.subtotal = monto_total
        comprobante.total = monto_total
        comprobante.save()

        # 6. Crear Cuenta por Pagar
        CuentaEstado.objects.create(
            comprobante=comprobante, monto_total=monto_total,
            saldo_pendiente=monto_total, fecha_vencimiento=fecha
        )

        return redirect('dashboard')

    mis_productos = Producto.objects.filter(empresa=empresa)
    return render(request, 'core/compra_manual_form.html', {'mis_productos': mis_productos})

@login_required
def editar_producto(request, pk):
    producto = get_object_or_404(Producto, id=pk, empresa_id=request.session['empresa_id'])
    
    if request.method == 'POST':
        # Actualizamos datos básicos y precios
        producto.nombre_interno = request.POST.get('nombre')
        producto.sku = request.POST.get('sku')
        producto.precio_compra_referencial = decimal.Decimal(request.POST.get('p_compra'))
        producto.precio_venta_referencial = decimal.Decimal(request.POST.get('p_venta'))
        
        # Categoría
        cat_id = request.POST.get('categoria')
        if cat_id:
            producto.categoria_id = cat_id
            
        producto.save()
        return redirect('lista_productos')

    categorias = CategoriaProducto.objects.all()
    return render(request, 'core/producto_edit_form.html', {
        'p': producto,
        'categorias': categorias
    })

@login_required
@transaction.atomic
def ajustar_stock(request, pk):
    producto = get_object_or_404(Producto, id=pk, empresa_id=request.session['empresa_id'])
    
    if request.method == 'POST':
        tipo = request.POST.get('tipo')
        cant = decimal.Decimal(request.POST.get('cantidad'))
        motivo = request.POST.get('motivo')

        # 1. Registrar el Ajuste
        AjusteStock.objects.create(
            empresa_id=request.session['empresa_id'],
            producto=producto,
            tipo=tipo,
            cantidad=cant,
            motivo=motivo,
            usuario=request.user
        )

        # 2. Actualizar el Stock Real
        if tipo == 'Ingreso':
            producto.stock_actual += cant
        else:
            producto.stock_actual -= cant
        
        producto.save()
        return redirect('lista_productos')

    return render(request, 'core/producto_ajuste_form.html', {'p': producto})

@login_required
def producto_kardex(request, pk):
    emp_id = request.session.get('empresa_id')
    producto = get_object_or_404(Producto, id=pk, empresa_id=emp_id)

    # 1. Obtener movimientos desde Facturas/Boletas (Detalles)
    # Filtramos los detalles de este producto para la empresa actual
    mov_comprobantes = ComprobanteDetalle.objects.filter(
        producto=producto, 
        comprobante__empresa_id=emp_id
    ).select_related('comprobante', 'comprobante__entidad')

    # 2. Obtener movimientos desde Ajustes Manuales
    mov_ajustes = AjusteStock.objects.filter(producto=producto)

    # 3. Estandarizar y Unificar los datos para el reporte
    historial_sucio = []

    for d in mov_comprobantes:
        # Si es compra, es entrada. Si es venta, es salida.
        tipo_mov = 'Entrada' if d.comprobante.operacion == 'Compra' else 'Salida'
        historial_sucio.append({
            'fecha': d.comprobante.fecha_emision,
            'documento': d.comprobante.codigo_factura,
            'entidad': d.comprobante.entidad.nombre_razon_social,
            'tipo': tipo_mov,
            'cantidad': float(d.cantidad),
            'precio': float(d.precio_unitario),
        })

    for a in mov_ajustes:
        tipo_mov = 'Entrada' if a.tipo == 'Ingreso' else 'Salida'
        historial_sucio.append({
            'fecha': a.fecha.date(),
            'documento': 'AJUSTE MANUAL',
            'entidad': f"Usuario: {a.usuario.username}",
            'tipo': tipo_mov,
            'cantidad': float(a.cantidad),
            'precio': 0.0,
            'motivo': a.motivo
        })

    # 4. Ordenar por fecha y calcular Saldo Acumulado
    historial_ordenado = sorted(historial_sucio, key=lambda x: x['fecha'])
    
    saldo_acumulado = 0
    kardex_final = []

    for mov in historial_ordenado:
        if mov['tipo'] == 'Entrada':
            saldo_acumulado += mov['cantidad']
        else:
            saldo_acumulado -= mov['cantidad']
        
        mov['saldo_despues'] = saldo_acumulado
        kardex_final.append(mov)

    # Invertimos para ver lo más reciente arriba
    kardex_final.reverse()

    return render(request, 'core/producto_kardex.html', {
        'p': producto,
        'movimientos': kardex_final
    })


@login_required
def registrar_venta_manual(request):
    emp_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=emp_id)

    if request.method == 'POST':
        # 1. CAPTURAMOS DATOS Y CALCULAMOS
        modo = request.POST.get('modo_tributario') # 'oficial' o 'interno'
        prod_ids = request.POST.getlist('prod_id[]')
        descs = request.POST.getlist('desc[]')
        cantidades = request.POST.getlist('cant[]')
        precios = request.POST.getlist('prec[]')

        items_preview = []
        total_acumulado = decimal.Decimal('0.00')

        for i in range(len(descs)):
            c = decimal.Decimal(cantidades[i] or 0)
            p = decimal.Decimal(precios[i] or 0)
            sub = c * p
            total_acumulado += sub
            items_preview.append({
                'prod_id': prod_ids[i],
                'descripcion': descs[i],
                'cantidad': float(c),
                'precio': float(p),
                'subtotal': float(sub)
            })

        # 2. LÓGICA DE IGV SEGÚN TU ELECCIÓN
        if modo == 'oficial':
            subtotal_fin = total_acumulado / decimal.Decimal('1.18')
            igv_fin = total_acumulado - subtotal_fin
        else:
            subtotal_fin = total_acumulado
            igv_fin = decimal.Decimal('0.00')

        # 3. GUARDAMOS EN SESIÓN PARA EL PREVIEW
        request.session['temp_venta_manual'] = {
            'ruc_dni': request.POST.get('ruc_dni'),
            'razon_social': request.POST.get('razon_social'),
            'serie_numero': request.POST.get('serie_numero'),
            'fecha': request.POST.get('fecha'),
            'moneda': request.POST.get('moneda'),
            'tc': request.POST.get('tipo_cambio', '1.000'),
            'modo': modo,
            'forma_pago': request.POST.get('forma_pago'),
            'banco_id': request.POST.get('banco_id'),
            'subtotal': float(subtotal_fin),
            'igv': float(igv_fin),
            'total': float(total_acumulado),
            'items': items_preview
        }
        return redirect('preview_venta_manual')

    # Al entrar (GET)
    productos = Producto.objects.filter(empresa=empresa).order_by('nombre_interno')
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)
    return render(request, 'core/venta_manual_form.html', {
        'productos': productos, 'bancos': bancos, 'hoy': datetime.date.today()
    })

@login_required
def preview_venta_manual(request):
    datos = request.session.get('temp_venta_manual')
    if not datos: return redirect('registrar_venta_manual')
    empresa = get_object_or_404(Empresa, id=request.session['empresa_id'])
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)
    return render(request, 'core/venta_manual_preview.html', {'c': datos, 'empresa': empresa, 'bancos': bancos})

@login_required
@transaction.atomic
def guardar_venta_manual_final(request):
    datos = request.session.get('temp_venta_manual')
    if not datos or request.method != 'POST': return redirect('registrar_venta_manual')
    
    empresa = get_object_or_404(Empresa, id=request.session['empresa_id'])
    
    # 1. Crear Cliente
    cliente, _ = Entidad.objects.get_or_create(
        empresa=empresa, numero_documento=datos['ruc_dni'],
        defaults={'nombre_razon_social': datos['razon_social'], 'tipo_entidad': 'Cliente'}
    )

    # 2. Crear Comprobante (Boleta o Recibo)
    tipo_doc = 'Boleta' if datos['modo'] == 'oficial' else 'Recibo'
    
    # --- CORRECCIÓN DE SEGURIDAD PARA LA FECHA ---
    fecha_final = datos.get('fecha') # Intentamos jalar la fecha de la sesión
    if not fecha_final:
        fecha_final = datetime.date.today() # Si no hay nada, usamos HOY para no dar error
    # ---------------------------------------------

    venta = Comprobante.objects.create(
        empresa=empresa,
        entidad=cliente,
        tipo_documento=tipo_doc,
        operacion='Venta',
        serie='V-MAN',
        numero=datos['serie_numero'],
        fecha_emision = datos.get('fecha') or datetime.date.today(), # Usamos la fecha segura
        moneda=datos['moneda'],
        tipo_cambio=decimal.Decimal(str(datos['tc'])),
        subtotal=decimal.Decimal(str(datos['subtotal'])),
        igv=decimal.Decimal(str(datos['igv'])),
        total=decimal.Decimal(str(datos['total'])),
        estado_sunat='ACEPTADO' if datos['modo'] == 'oficial' else 'INTERNO'
    )

    # 3. Items y Stock
    for item in datos['items']:
        producto = get_object_or_404(Producto, id=item['prod_id'], empresa=empresa)
        ComprobanteDetalle.objects.create(
            comprobante=venta, producto=producto,
            cantidad=item['cantidad'], precio_unitario=item['precio'],
            subtotal_linea=item['subtotal']
        )
        producto.stock_actual -= decimal.Decimal(str(item['cantidad']))
        producto.save()

    # 4. Lógica de Dinero
    cuenta = CuentaEstado.objects.create(
        comprobante=venta, monto_total=datos['total'],
        saldo_pendiente=datos['total'], fecha_vencimiento=datetime.date.today()
    )

    if datos['forma_pago'] == 'contado':
        banco = get_object_or_404(Cuenta_Bancaria, id=datos['banco_id'])
        MovimientoFinanciero.objects.create(
            empresa=empresa, tipo='Ingreso', monto=decimal.Decimal(str(datos['total'])),
            moneda=datos['moneda'], cuenta_bancaria=banco,
            referencia=f"Pago Contado {tipo_doc} {datos['serie_numero']}",
            comprobante=venta
        )
        cuenta.saldo_pendiente = 0
        cuenta.estado = 'Cancelado'
        cuenta.save()
        dest = 'dashboard'
    else:
        dest = 'configurar_cuotas' # Si es crédito, va a cuotas

    del request.session['temp_venta_manual']
    
    if datos['forma_pago'] == 'contado':
        return redirect(dest)
    else:
        return redirect(dest, cuenta_id=cuenta.id)

@login_required
@transaction.atomic
def pagar_prestamo(request, pk):
    prestamo = get_object_or_404(Prestamo, id=pk, empresa_id=request.session['empresa_id'])
    empresa = prestamo.empresa

    if request.method == 'POST':
        caja_id = request.POST.get('caja_id')
        banco_id = request.POST.get('banco_id')
        
        # 1. Registrar el Movimiento Financiero (Egreso de Caja/Banco)
        # Sumamos capital + intereses porque eso es lo que sale del banco
        monto_total_devolucion = prestamo.monto_capital + prestamo.monto_interes
        
        MovimientoFinanciero.objects.create(
            empresa=empresa,
            tipo='Egreso',
            monto=monto_total_devolucion,
            moneda=prestamo.moneda,
            referencia=f"Devolución de Préstamo a: {prestamo.prestamista}",
            caja_id=caja_id if caja_id else None,
            cuenta_bancaria_id=banco_id if banco_id else None
        )

        # 2. Marcar el Préstamo como Pagado
        prestamo.estado = 'Pagado'
        prestamo.save()

        return redirect('lista_prestamos')

    cajas = Caja.objects.filter(empresa=empresa, moneda=prestamo.moneda)
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa, moneda=prestamo.moneda)
    
    return render(request, 'core/pagar_prestamo.html', {
        'prestamo': prestamo,
        'cajas': cajas,
        'bancos': bancos,
        'total_a_pagar': prestamo.monto_capital + prestamo.monto_interes
    })

@login_required
def cronograma_vencimientos(request):
    emp_id = request.session.get('empresa_id')
    hoy = datetime.date.today()
    
    # 1. Obtenemos todas las cuotas de Facturas (Cuentas por Cobrar/Pagar)
    cuotas_facturas = Cuota.objects.filter(
        cuenta__comprobante__empresa_id=emp_id, 
        pagada=False
    )
    
    # 2. Obtenemos todas las cuotas de Préstamos
    cuotas_prestamos = Cuota.objects.filter(
        prestamo__empresa_id=emp_id, 
        pagada=False
    )
    
    # 3. Mezclamos y ordenamos por fecha (el modelo ya ordena por fecha)
    # Combinamos ambas listas para el template
    todas_las_cuotas = list(cuotas_facturas) + list(cuotas_prestamos)
    todas_las_cuotas.sort(key=lambda x: x.fecha_vencimiento)

    return render(request, 'core/cronograma_general.html', {
        'cuotas': todas_las_cuotas,
        'hoy': hoy
    })

@login_required
@transaction.atomic
def registrar_pago_cuota(request, cuota_id):
    cuota = get_object_or_404(Cuota, id=cuota_id)
    empresa = Empresa.objects.get(id=request.session['empresa_id'])
    
    # Determinar moneda y origen para filtrar bancos/cajas
    moneda = cuota.cuenta.comprobante.moneda if cuota.cuenta else cuota.prestamo.moneda
    origen_nombre = cuota.cuenta.comprobante.codigo_factura if cuota.cuenta else f"Préstamo {cuota.prestamo.prestamista}"

    if request.method == 'POST':
        caja_id = request.POST.get('caja_id')
        banco_id = request.POST.get('banco_id')
        
        # 1. Crear el Movimiento Financiero
        MovimientoFinanciero.objects.create(
            empresa=empresa,
            tipo='Egreso' if (cuota.prestamo or (cuota.cuenta and cuota.cuenta.comprobante.operacion == 'Compra')) else 'Ingreso',
            monto=cuota.monto,
            moneda=moneda,
            referencia=f"Pago Cuota {cuota.numero_cuota} de {origen_nombre}",
            caja_id=caja_id if caja_id else None,
            cuenta_bancaria_id=banco_id if banco_id else None
        )

        # 2. Marcar Cuota como Pagada
        cuota.pagada = True
        cuota.fecha_pago = datetime.date.today()
        cuota.save()

        # 3. Actualizar el saldo pendiente del "Padre" (Factura o Préstamo)
        if cuota.cuenta:
            cuenta = cuota.cuenta
            cuenta.saldo_pendiente -= cuota.monto
            if cuenta.saldo_pendiente <= 0:
                cuenta.estado = 'Cancelado'
            cuenta.save()
        elif cuota.prestamo:
            prestamo = cuota.prestamo
            # Nota: Los préstamos no tienen campo 'saldo_pendiente' en el modelo original, 
            # pero el estado sí cambia cuando se pagan todas las cuotas.
            if not prestamo.cuotas.filter(pagada=False).exists():
                prestamo.estado = 'Pagado'
                prestamo.save()

        return redirect('cronograma_vencimientos')

    cajas = Caja.objects.filter(empresa=empresa, moneda=moneda)
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa, moneda=moneda)

    return render(request, 'core/pagar_cuota_form.html', {
        'cuota': cuota,
        'cajas': cajas,
        'bancos': bancos,
        'origen': origen_nombre
    })

@login_required
@transaction.atomic
def registrar_gasto_manual(request):
    emp_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=emp_id)

    if request.method == 'POST':
        categoria_id = request.POST.get('categoria_id')
        descripcion = request.POST.get('descripcion')
        monto = decimal.Decimal(request.POST.get('monto'))
        moneda = request.POST.get('moneda')
        fecha = request.POST.get('fecha')
        caja_id = request.POST.get('caja_id')
        banco_id = request.POST.get('banco_id')

        # 1. Registrar el Gasto Operativo
        gasto = GastoOperativo.objects.create(
            empresa=empresa,
            categoria_gasto_id=categoria_id,
            descripcion=descripcion,
            monto=monto,
            moneda=moneda,
            fecha=fecha
        )

        # 2. Registrar el Movimiento Financiero (Salida de dinero)
        # Esto activará los Signals para bajar el saldo de tu cuenta
        MovimientoFinanciero.objects.create(
            empresa=empresa,
            tipo='Egreso',
            monto=monto,
            moneda=moneda,
            referencia=f"Gasto: {descripcion}",
            caja_id=caja_id if caja_id else None,
            cuenta_bancaria_id=banco_id if banco_id else None
        )

        return redirect('lista_gastos')

    categorias = CategoriaGasto.objects.all()
    cajas = Caja.objects.filter(empresa=empresa)
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)

    return render(request, 'core/gasto_form.html', {
        'categorias': categorias,
        'cajas': cajas,
        'bancos': bancos,
        'hoy': datetime.date.today()
    })

@login_required
def trazabilidad_igv(request):
    emp_id = int(request.session.get('empresa_id'))
    
    # Traemos todos los comprobantes que tienen IGV (Ventas y Compras)
    comprobantes = Comprobante.objects.filter(
        empresa_id=emp_id
    ).exclude(
        tipo_documento='Recibo'
    ).exclude(
        estado_sunat='INTERNO'
    ).order_by('-fecha_emision')
    
    reporte = []
    total_debito = 0  # IGV Ventas
    total_credito = 0 # IGV Compras

    for c in comprobantes:
        # Calculamos el IGV en soles usando el TC de la factura
        igv_pen = float(c.igv * c.tipo_cambio)
        
        if c.operacion == 'Venta':
            total_debito += igv_pen
        else:
            total_credito += igv_pen
            
        reporte.append({
            'id': c.id,
            'fecha': c.fecha_emision,
            'documento': c.codigo_factura,
            'entidad': c.entidad.nombre_razon_social,
            'tipo': c.operacion,
            'moneda': c.moneda,
            'igv_original': float(c.igv),
            'tc': float(c.tipo_cambio),
            'igv_pen': igv_pen,
            'es_escudo': c.es_escudo_tributario
        })

    return render(request, 'core/trazabilidad_igv.html', {
        'reporte': reporte,
        'total_debito': total_debito,
        'total_credito': total_credito,
        'igv_neto': total_debito - total_credito
    })

@login_required
def cargar_documento_sunat(request):
    empresa_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=empresa_id)
    
    if request.method == 'POST' and request.FILES.get('documento'):
        archivo = request.FILES['documento']
        try:
            datos = procesar_pdf_impuestos(archivo)
            
            if not datos['tipo']:
                return render(request, 'core/cargar_impuesto.html', {'error': 'No se reconoció el formato de SUNAT.'})

            # --- FILTRO DE DUPLICADOS (Seguridad) ---
            ya_existe = False
            if datos['tipo'] == 'PDT_0621':
                ya_existe = DeclaracionMensual.objects.filter(
                    empresa=empresa, 
                    numero_orden=datos['nro_orden']
                ).exists()
            elif datos['tipo'] == 'PAGO_1662':
                # Buscamos la combinación de los 3 datos clave
                tributo_cod = datos['tributos'][0]['codigo']
                ya_existe = PagoImpuesto.objects.filter(
                    empresa=empresa,
                    numero_operacion=datos['nro_orden'],
                    periodo=datos['periodo'],
                    tributo_codigo=tributo_cod
                ).exists()
            # ----------------------------------------

            request.session['temp_impuesto'] = datos
            
            # Necesitamos los bancos para que el usuario elija de dónde pagó (si es 1662)
            bancos = Cuenta_Bancaria.objects.filter(empresa=empresa, moneda='PEN')

            return render(request, 'core/confirmar_impuesto.html', {
                'datos': datos,
                'ya_existe': ya_existe,
                'bancos': bancos
            })
        except Exception as e:
            return render(request, 'core/cargar_impuesto.html', {'error': str(e)})

    return render(request, 'core/cargar_impuesto.html')

@login_required
@transaction.atomic
def guardar_documento_sunat(request):
    if request.method == 'POST':
        datos = request.session.get('temp_impuesto')
        empresa = Empresa.objects.get(id=request.session['empresa_id'])
        
        if datos['tipo'] == 'PDT_0621':
            # Guardamos cada tributo declarado (IGV y Renta)
            for trib in datos['tributos']:
                DeclaracionMensual.objects.create(
                    empresa=empresa,
                    periodo=datos['periodo'],
                    tributo=trib['codigo'],
                    monto_declarado=trib['monto'],
                    numero_orden=datos['nro_orden'],
                    fecha_presentacion=datetime.date.today() # Fecha de carga
                )
        
        elif datos['tipo'] == 'PAGO_1662':
            banco_id = request.POST.get('banco_id')
            banco = Cuenta_Bancaria.objects.get(id=banco_id)
            monto_pago = decimal.Decimal(str(datos['tributos'][0]['monto']))

            # 1. Crear el movimiento de dinero (Baja el banco)
            mov = MovimientoFinanciero.objects.create(
                empresa=empresa,
                tipo='Egreso',
                monto=monto_pago,
                moneda='PEN',
                referencia=f"PAGO SUNAT: {datos['tributos'][0]['codigo']} - Período {datos['periodo']}",
                cuenta_bancaria=banco
            )

            # 2. Registrar el pago de impuesto
            PagoImpuesto.objects.create(
                empresa=empresa,
                monto_pagado=monto_pago,
                fecha_pago=datetime.date.today(),
                numero_operacion=datos['nro_orden'],
                periodo=datos['periodo'], # <-- NUEVO
                tributo_codigo=datos['tributos'][0]['codigo'], # <-- NUEVO
                movimiento=mov
            )

        del request.session['temp_impuesto']
        return redirect('dashboard')

    return redirect('cargar_documento_sunat')

# core/views.py

@login_required
@transaction.atomic
def cerrar_mes_tributario(request):
    emp_id = request.session.get('empresa_id')
    hoy = datetime.date.today()
    periodo_actual = hoy.strftime("%Y-%m")

    if request.method == 'POST':
        # Capturamos el monto calculado que viene del campo hidden del formulario
        monto_resultado = decimal.Decimal(request.POST.get('monto_resultado', '0.00'))
        
        saldo_favor = 0
        if monto_resultado < 0:
            saldo_favor = abs(monto_resultado)

        # Guardamos o actualizamos el cierre de este mes
        CierreMensual.objects.update_or_create(
            empresa_id=emp_id,
            periodo=periodo_actual,
            defaults={
                'igv_final_calculado': monto_resultado,
                'saldo_a_favor_generado': saldo_favor,
                'cerrado': True
            }
        )
        return redirect('dashboard')
    
    # Si entramos por GET, mostramos la pantalla de confirmación
    # Pasamos el monto que viene en la URL o lo recalculamos (aquí lo pasaremos simple)
    return render(request, 'core/confirmar_cierre.html', {
        'periodo': periodo_actual,
        # Importante: recalcula o jala el monto para el campo hidden del form
    })

@login_required
def trazabilidad_retenciones(request):
    emp_id = int(request.session.get('empresa_id'))
    
    # Traemos todos los detalles de retenciones procesados
    detalles = RetencionDetalle.objects.filter(
        certificado__empresa_id=emp_id
    ).order_by('-certificado__fecha_emision')
    
    # Calculamos el total acumulado para el resumen
    total_soles = detalles.aggregate(Sum('monto_retencion_pen'))['monto_retencion_pen__sum'] or 0

    return render(request, 'core/trazabilidad_retenciones.html', {
        'detalles': detalles,
        'total_soles': total_soles
    })

# core/views.py

@login_required
def lista_pagos_sunat(request):
    emp_id = int(request.session.get('empresa_id'))
    
    # Traemos todos los pagos 1662 registrados
    pagos = PagoImpuesto.objects.filter(empresa_id=emp_id).order_by('-fecha_pago')
    
    # Calculamos totales por tipo para un resumen rápido
    total_igv = pagos.filter(tributo_codigo='1011').aggregate(Sum('monto_pagado'))['monto_pagado__sum'] or 0
    total_renta = pagos.filter(tributo_codigo='3111').aggregate(Sum('monto_pagado'))['monto_pagado__sum'] or 0

    return render(request, 'core/pagos_sunat_list.html', {
        'pagos': pagos,
        'total_igv': total_igv,
        'total_renta': total_renta,
        'total_general': total_igv + total_renta
    })

# core/views.py

@login_required
def lista_notificaciones(request):
    emp_id = request.session.get('empresa_id')
    notificaciones = Notificacion.objects.filter(empresa_id=emp_id).order_by('-fecha')
    return render(request, 'core/notificaciones_list.html', {'notificaciones': notificaciones})

@login_required
def marcar_notificaciones_leidas(request):
    emp_id = request.session.get('empresa_id')
    # Actualizamos todas las no leídas a leídas
    Notificacion.objects.filter(empresa_id=emp_id, leida=False).update(leida=True)
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

# core/views.py

@login_required
def registrar_cotizacion(request):
    # --- PASO 4: LIMPIEZA DE SESIÓN (ESTO VA AQUÍ AL INICIO) ---
    # Si el usuario entra por la URL de 'nueva', borramos cualquier rastro 
    # de una edición anterior para que el formulario salga limpio.
    if request.GET.get('limpiar'):
        if 'temp_cotizacion' in request.session:
            del request.session['temp_cotizacion']
    # ----------------------------------------------------------

    emp_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=emp_id)

    # 1. Recuperar datos si venimos de "Editar" (Si se borró arriba, esto será {})
    temp_cot = request.session.get('temp_cotizacion', {})

    # 2. Lógica de Correlativo (Solo si NO estamos editando)
    if 'numero' in temp_cot:
        sugerencia = temp_cot['numero'] # Usamos el número que ya tiene
    else:
        ultima_cot = Cotizacion.objects.filter(empresa=empresa).order_by('-id').first()
        sugerencia = "COT-0001"
        if ultima_cot:
            match = re.search(r'(\d+)', ultima_cot.numero)
            if match:
                sugerencia = f"COT-{(int(match.group(1)) + 1):04d}"

    # 2. Lógica cuando se presiona "Guardar y Preparar PDF" (POST)
    if request.method == 'POST':
        prod_ids = request.POST.getlist('prod_id[]')
        descs = request.POST.getlist('desc[]')
        cants = request.POST.getlist('cant[]')
        precs = request.POST.getlist('prec[]')

        items_preview = []
        total_acumulado = decimal.Decimal('0.00')

        for i in range(len(descs)):
            try:
                c = decimal.Decimal(cants[i] if cants[i] else 0)
                p = decimal.Decimal(precs[i] if precs[i] else 0)
            except (decimal.InvalidOperation, ValueError):
                c = decimal.Decimal('0.00')
                p = decimal.Decimal('0.00')
                
            sub_fila = c * p
            total_acumulado += sub_fila
            
            items_preview.append({
                'prod_id': prod_ids[i],
                'descripcion': descs[i],
                'cantidad': float(c),
                'precio': float(p),
                'subtotal': float(sub_fila)
            })

        subtotal_fin = total_acumulado / decimal.Decimal('1.18')
        igv_fin = total_acumulado - subtotal_fin

        # Guardamos en sesión. 
        # Si veníamos editando, mantenemos el cot_id original.
        request.session['temp_cotizacion'] = {
            'cot_id': temp_cot.get('cot_id'), # IMPORTANTE: No perder el ID al editar
            'numero': request.POST.get('numero_cotizacion'),
            'ruc_dni': request.POST.get('ruc_dni'),
            'razon_social': request.POST.get('razon_social'),
            'direccion': request.POST.get('direccion_cliente'),
            'atencion': request.POST.get('atencion_a'),
            'moneda': request.POST.get('moneda'),
            'garantia': request.POST.get('garantia'),
            'tiempo': request.POST.get('tiempo_entrega'),
            'validez': request.POST.get('validez'),
            'notas': request.POST.get('notas'),
            'subtotal': float(subtotal_fin),
            'igv': float(igv_fin),
            'total': float(total_acumulado),
            'items': items_preview
        }
        return redirect('preview_antes_de_guardar')

    # 3. Lógica cuando solo se abre la página (GET)
    mis_productos = Producto.objects.filter(empresa=empresa).order_by('nombre_interno')
    return render(request, 'core/cotizacion_form.html', {
        'sugerencia': sugerencia, 
        'mis_productos': mis_productos,
        'hoy': datetime.date.today(),
        'editando': 'cot_id' in temp_cot
    })

def preview_antes_de_guardar(request):
    # 1. Sacamos los datos que guardamos en la sesión en el paso anterior
    datos = request.session.get('temp_cotizacion')
    
    # Si no hay datos (porque refrescaron la página), regresamos al formulario
    if not datos:
        return redirect('registrar_cotizacion')

    # 2. Obtenemos la empresa y bancos para mostrar en el PDF/Vista previa
    emp_id = request.session.get('empresa_id')
    empresa = get_object_or_404(Empresa, id=emp_id)
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)

    # 3. Preparamos el contexto usando las llaves que guardaste en la sesión
    context = {
        'datos': datos,           # Aquí están ruc_dni, razon_social, etc.
        'items': datos['items'],  # Aquí están los productos
        'subtotal': datos['subtotal'],
        'igv': datos['igv'],
        'total_gral': datos['total'],
        'empresa': empresa,
        'bancos': bancos,
        'notas': datos.get('notas', '')
    }
    
    return render(request, 'core/cotizacion_preview_confirm.html', context)

@login_required
@transaction.atomic
def guardar_cotizacion_final(request):
    datos = request.session.get('temp_cotizacion')
    if not datos or request.method != 'POST': 
        return redirect('registrar_cotizacion')

    empresa = get_object_or_404(Empresa, id=request.session['empresa_id'])
    
    # AHORA SÍ GUARDAMOS EN LA BASE DE DATOS
    cot = Cotizacion.objects.create(
        empresa=empresa,
        numero=datos['numero'],
        ruc_dni_cliente=datos['ruc_dni'],
        nombre_cliente=datos['razon_social'],
        direccion_cliente=datos['direccion'],
        atencion_a=datos['atencion'],
        moneda=datos['moneda'],
        total=datos['total'],
        garantia=datos['garantia'],
        tiempo_entrega=datos['tiempo'],
        validez_dias=datos['validez'],
        notas=datos['notas']
    )

    for item in datos['items']:
        CotizacionDetalle.objects.create(
            cotizacion=cot,
            producto_id=item['prod_id'] if item['prod_id'] else None,
            descripcion_libre=item['descripcion'],
            cantidad=item['cantidad'],
            precio_unitario=item['precio']
        )

    del request.session['temp_cotizacion']
    return redirect('lista_cotizaciones')

@login_required
def lista_cotizaciones(request):
    emp_id = request.session.get('empresa_id')
    cotizaciones = Cotizacion.objects.filter(empresa_id=emp_id).order_by('-id')
    return render(request, 'core/cotizaciones_list.html', {'cotizaciones': cotizaciones})

@login_required
def ver_cotizacion_guardada(request, pk):
    cot = get_object_or_404(Cotizacion, id=pk, empresa_id=request.session['empresa_id'])
    empresa = cot.empresa
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)
    
    # CALCULAMOS AQUÍ PARA ASEGURAR QUE LLEGUE AL PDF
    subtotal = cot.total / decimal.Decimal('1.18')
    igv = cot.total - subtotal
    
    return render(request, 'core/cotizacion_ver_final.html', {
        'c': cot,
        'empresa': empresa,
        'bancos': bancos,
        'subtotal': subtotal,
        'igv': igv,
    })

@login_required
def editar_cotizacion(request, pk):
    emp_id = request.session.get('empresa_id')
    cot = get_object_or_404(Cotizacion, id=pk, empresa_id=emp_id)

    
    # Pasamos los datos a la sesión usando los mismos nombres que el formulario POST
    items_session = []
    for item in cot.detalles.all():
        items_session.append({
            'prod_id': item.producto.id if item.producto else '',
            'descripcion': item.descripcion_libre,
            'cantidad': float(item.cantidad),
            'precio': float(item.precio_unitario),
            'subtotal': float(item.cantidad * item.precio_unitario)
        })

    request.session['temp_cotizacion'] = {
        'cot_id': cot.id,
        'numero': cot.numero,
        'ruc_dni': cot.ruc_dni_cliente,
        'razon_social': cot.nombre_cliente,
        'direccion_cliente': cot.direccion_cliente, # Nombre corregido
        'atencion_a': cot.atencion_a,               # Nombre corregido
        'moneda': cot.moneda,
        'garantia': cot.garantia,
        'tiempo_entrega': cot.tiempo_entrega,       # Nombre corregido
        'validez': cot.validez_dias,
        'notas': cot.notas,
        'total': float(cot.total),
        'items': items_session
    }

    return redirect('registrar_cotizacion')

# core/views.py

@login_required
def ver_pdf_venta_manual(request, pk):
    # Buscamos el comprobante (Boleta o Recibo)
    venta = get_object_or_404(Comprobante, id=pk, empresa_id=request.session['empresa_id'])
    empresa = venta.empresa
    bancos = Cuenta_Bancaria.objects.filter(empresa=empresa)

    # Determinamos si mostramos IGV o no (según lo que elegiste al crearla)
    mostrar_igv = (venta.igv > 0)

    return render(request, 'core/venta_pdf_final.html', {
        'c': venta,
        'empresa': empresa,
        'bancos': bancos,
        'mostrar_igv': mostrar_igv
    })

@login_required
def salir(request):
    if request.method == 'POST':
        auth_logout(request) # Destruye la sesión
        return redirect('login') # Redirige al login
    
    # Si entra por GET (hace clic en el menú), muestra la confirmación
    return render(request, 'core/confirmar_salida.html')