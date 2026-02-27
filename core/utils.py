# core/utils.py
from core.models import TipoCambioDia
from lxml import etree
import datetime
import requests # Necesitarás: pip install requests
import json
from django.forms.models import model_to_dict
from .models import LogAuditoria
import pdfplumber
import re
import decimal


def procesar_xml_sunat(archivo_xml):
    """
    Extrae datos básicos de un XML de Factura SUNAT.
    """
    tree = etree.parse(archivo_xml)
    root = tree.getroot()
    
    # Namespaces estándar de SUNAT
    ns = {
        'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
        'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    }

    def get_tag(xpath, root_node=root):
        result = root_node.xpath(xpath, namespaces=ns)
        return result[0].text if result else None

    datos = {
        'serie_numero': get_tag('//cbc:ID'),
        'fecha_emision': get_tag('//cbc:IssueDate'),
        'ruc_proveedor': get_tag('//cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID'),
        'razon_social_proveedor': get_tag('//cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName'),
        'moneda': get_tag('//cbc:DocumentCurrencyCode'),
        'total': float(get_tag('//cac:LegalMonetaryTotal/cbc:PayableAmount') or 0),
        'items': []
    }

    # Extraer ítems
    lines = root.xpath('//cac:InvoiceLine', namespaces=ns)
    for line in lines:
        item = {
            'descripcion': get_tag('.//cac:Item/cbc:Description', line),
            'cantidad': float(get_tag('.//cbc:InvoicedQuantity', line) or 0),
            'precio_unitario': float(get_tag('.//cac:Price/cbc:PriceAmount', line) or 0),
        }
        datos['items'].append(item)

    return datos

def sincronizar_tipo_cambio():
    hoy = datetime.date.today()
    # Aquí iría la lógica para consultar una API de SUNAT. 
    # Por ahora, simulamos una respuesta exitosa:
    tc, created = TipoCambioDia.objects.get_or_create(
        fecha=hoy,
        defaults={'compra': 3.750, 'venta': 3.780, 'fuente': 'SUNAT'}
    )
    return tc

def registrar_auditoria_update(usuario, instancia_vieja, instancia_nueva, motivo):
    from .models import LogAuditoria
    cambios = []
    
    # Comprobación de seguridad para el campo TOTAL
    if hasattr(instancia_vieja, 'total') and instancia_vieja.total != instancia_nueva.total:
        cambios.append(f"Monto: {instancia_vieja.total} -> {instancia_nueva.total}")
    
    # Comprobación para la ENTIDAD
    if hasattr(instancia_vieja, 'entidad') and instancia_vieja.entidad != instancia_nueva.entidad:
        cambios.append(f"Entidad: {instancia_vieja.entidad} -> {instancia_nueva.entidad}")

    if cambios:
        resumen = " | ".join(cambios)
        LogAuditoria.objects.create(
            usuario=usuario,
            empresa=instancia_nueva.empresa,
            accion='UPDATE',
            tabla_afectada=instancia_nueva.__class__.__name__,
            referencia_id=instancia_nueva.id,
            motivo_cambio=f"{resumen} | Motivo: {motivo}"
        )

def consultar_validez_sunat(serie, numero, ruc_emisor, total):
    """
    RF-23: Simula la consulta al Web Service de SUNAT.
    En producción, aquí se haría un POST a la API de SUNAT/OSE.
    """
    # Simulamos un retraso de red y respuesta positiva
    import time
    # time.sleep(1) # Opcional: para simular realismo
    return "ACEPTADO"

def procesar_pdf_sunat(archivo_pdf):
    datos = {
        'serie_numero': None,
        'fecha_emision': None,
        'ruc_proveedor': None,
        'razon_social_proveedor': None,
        'ruc_cliente': None,
        'razon_social_cliente': None,
        'moneda': 'PEN',
        'total': 0.0,
        'items': []
    }

    with pdfplumber.open(archivo_pdf) as pdf:
        pagina = pdf.pages[0]
        texto = pagina.extract_text()
        lineas = texto.split('\n')

        # --- 1. DETECCIÓN DE MONEDA ---
        if any(x in texto.upper() for x in ["DOLAR", "USD", "AMERICANO", "$"]):
            datos['moneda'] = 'USD'

        # --- 2. EXTRAER CABECERA ---
        match_sn = re.search(r"([A-Z]{1,2}[A-Z0-9]{2,3}-\d+)", texto)
        if match_sn: datos['serie_numero'] = match_sn.group(1)

        rucs = re.findall(r"RUC\s*[:]?\s*(\d{11})", texto)
        if len(rucs) >= 1: datos['ruc_proveedor'] = rucs[0]
        if len(rucs) >= 2: datos['ruc_cliente'] = rucs[1]

        match_rs_cli = re.search(r"Señor\(es\)\s*:\s*([^\n\r]+)", texto)
        if match_rs_cli: datos['razon_social_cliente'] = match_rs_cli.group(1).strip()

        match_fecha = re.search(r"([\d]{1,2}/[\d]{1,2}/[\d]{4})|([\d]{4}-[\d]{2}-[\d]{2})", texto)
        if match_fecha:
            f_raw = match_fecha.group(0)
            if '/' in f_raw:
                p = f_raw.split('/')
                datos['fecha_emision'] = f"{p[2]}-{p[1]}-{p[0]}"
            else:
                datos['fecha_emision'] = f_raw

        # --- 3. EXTRACCIÓN DEL TOTAL ---
        match_total = re.search(r"Importe\s*[Tt]otal.*?([\d,]+\.\d{2})", texto, re.IGNORECASE | re.DOTALL)
        if match_total:
            datos['total'] = float(match_total.group(1).replace(',', ''))
        else:
            cifras = re.findall(r"[\d,]+\.\d{2}", texto)
            if cifras: datos['total'] = float(cifras[-1].replace(',', ''))

        # --- 4. ESCÁNER DE PRODUCTOS (CORREGIDO) ---
        for linea in lineas:
            linea = linea.strip()
            # Buscamos líneas que empiecen con Cantidad (Ej: 1.00)
            match_row = re.search(r"^(\d+\.\d+)\s+(.*)", linea)
            
            if match_row:
                try:
                    cant = float(match_row.group(1))
                    texto_completo_linea = match_row.group(2) # Todo lo que sigue después del número
                    
                    # Buscamos todos los precios al final de la línea
                    precios = re.findall(r"[\d,]+\.\d{2}", texto_completo_linea)
                    
                    if precios:
                        # 4.1 Identificamos Precio Unitario (Prorrateo inteligente)
                        if len(precios) >= 3:
                            p_unit = float(precios[-3].replace(',', '')) # Caso Green Data / Grifos
                        else:
                            p_unit = float(precios[-1].replace(',', '')) # Caso CERTIMET / Ventas propias

                        # 4.2 Limpiamos la descripción de forma agresiva
                        desc_limpia = texto_completo_linea
                        
                        # Quitamos los precios del texto de la descripción
                        for p in precios:
                            desc_limpia = desc_limpia.replace(p, "")
                        
                        # Quitamos unidades y códigos comunes
                        for word in ["UNIDAD", "NIU", "US GALON", "(3,7843 L)", "0000008"]:
                            desc_limpia = desc_limpia.replace(word, "")
                        
                        # Quitamos códigos tipo SKU/Internos al inicio (Ej: COD_Z_UN o CF302AC)
                        desc_limpia = re.sub(r"^[A-Z0-9_.-]+\s+", "", desc_limpia).strip()
                        
                        # Si quedó vacío, usamos el texto original como respaldo
                        if not desc_limpia: desc_limpia = texto_completo_linea

                        datos['items'].append({
                            'cantidad': cant,
                            'descripcion': desc_limpia.strip(),
                            'precio_unitario': p_unit
                        })
                except (ValueError, IndexError):
                    continue
                    
    # Fallback Razón Social Emisor
    if not datos['razon_social_proveedor']:
        if datos['ruc_proveedor'] == "10712211917": datos['razon_social_proveedor'] = "FG SHOP"
        elif datos['ruc_proveedor'] == "20610111379": datos['razon_social_proveedor'] = "GREEN DATA S.A.C."
        elif datos['ruc_proveedor'] == "20536231669": datos['razon_social_proveedor'] = "PRISMACOMP S.A.C."
        elif datos['ruc_proveedor'] == "20605732861": datos['razon_social_proveedor'] = "CERTIMET S.A.C."
        else: datos['razon_social_proveedor'] = f"PROVEEDOR RUC {datos['ruc_proveedor']}"

    return datos


def procesar_xml_retencion(archivo_xml):
    """
    Lee un XML de Retención SUNAT y extrae la tabla de facturas afectadas.
    """
    tree = etree.parse(archivo_xml)
    root = tree.getroot()
    
    # Namespaces específicos para Retenciones
    ns = {
        'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
        'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        'sac': 'urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1',
    }

    def get_tag(xpath, node=root):
        res = node.xpath(xpath, namespaces=ns)
        return res[0].text if res else None

    datos = {
        'serie_numero': get_tag('//cbc:ID'),
        'fecha_emision': get_tag('//cbc:IssueDate'),
        'ruc_agente': get_tag('//cac:AgentParty/cac:PartyIdentification/cbc:ID'),
        'nombre_agente': get_tag('//cac:AgentParty/cac:PartyLegalEntity/cbc:RegistrationName'),
        'monto_total_retencion': float(get_tag('//cbc:TotalInvoiceAmount') or 0),
        'lineas': []
    }

    # Extraer cada factura retenida ( sac:SUNATRetentionDocumentReference )
    lineas_xml = root.xpath('//sac:SUNATRetentionDocumentReference', namespaces=ns)
    
    for linea in lineas_xml:
        # Extraer el Tipo de Cambio de CADA línea (CalculationRate)
        tc = float(get_tag('.//cac:ExchangeRate/cbc:CalculationRate', linea) or 1.0)
        monto_pen = float(get_tag('.//sac:SUNATRetentionAmount', linea) or 0)
        
        # El monto a descontar de la deuda es: Monto PEN / Tipo de Cambio
        monto_origen = monto_pen / tc if tc > 0 else monto_pen

        datos['lineas'].append({
            'factura_ref': get_tag('.//cbc:ID', linea), # Ej: E001-4
            'monto_pen': monto_pen,
            'monto_moneda_origen': round(monto_origen, 2),
            'tipo_cambio': tc
        })

    return datos


def procesar_pdf_impuestos(archivo_pdf):
    """
    Detecta si es un PDT 0621 o una Boleta 1662 y extrae los datos.
    """
    datos = {'tipo': None, 'periodo': None, 'tributos': [], 'nro_orden': None, 'fecha': None}
    
    with pdfplumber.open(archivo_pdf) as pdf:
        texto = pdf.pages[0].extract_text()
        
        # 1. IDENTIFICAR TIPO
        if "Formulario - 0621" in texto:
            datos['tipo'] = 'PDT_0621'
            # Extraer Periodo (Ej: 202510)
            match_per = re.search(r"Período\s*:\s*(\d{6})", texto)
            datos['periodo'] = match_per.group(1) if match_per else None
            
            # Extraer Nro Orden
            match_ord = re.search(r"Número de Orden\s*:\s*(\d+)", texto)
            datos['nro_orden'] = match_ord.group(1) if match_ord else None

            # Extraer Tributos y sus montos de la tabla
            # Buscamos las líneas que tengan 1011 o 3111
            lineas = texto.split('\n')
            for l in lineas:
                if "1011" in l: # IGV
                    monto = re.findall(r"S/.\s*([\d,]+)", l)
                    if monto: datos['tributos'].append({'codigo': '1011', 'nombre': 'IGV', 'monto': float(monto[0].replace(',', ''))})
                if "3111" in l: # RENTA
                    monto = re.findall(r"S/.\s*([\d,]+)", l)
                    if monto: datos['tributos'].append({'codigo': '3111', 'nombre': 'RENTA', 'monto': float(monto[0].replace(',', ''))})

        elif "Formulario - 1662" in texto:
            datos['tipo'] = 'PAGO_1662'
            match_per = re.search(r"Periodo\s*:\s*(\d{6})", texto)
            datos['periodo'] = match_per.group(1) if match_per else None
            
            match_monto = re.search(r"Importe Pagado\s*:\s*S/.\s*([\d,.]+)", texto)
            monto_final = float(match_monto.group(1).replace(',', '')) if match_monto else 0
            
            match_trib = re.search(r"Tributo\s*:\s*(\d{4})", texto)
            datos['tributos'].append({
                'codigo': match_trib.group(1) if match_trib else '0000',
                'monto': monto_final
            })
            
            match_op = re.search(r"Número de Operación\s*:\s*(\d+)", texto)
            datos['nro_orden'] = match_op.group(1) if match_op else None

    return datos

def aplicar_pago_en_cascada(monto_a_pagar, cuenta=None, prestamo=None):
    """
    Reparte el dinero entre las cuotas pendientes de forma cronológica.
    Funciona para Facturas (cuenta) o para Préstamos (prestamo).
    """
    monto_restante = decimal.Decimal(str(monto_a_pagar))
    
    # 1. Buscamos las cuotas pendientes ordenadas por fecha
    if cuenta:
        cuotas_pendientes = cuenta.cuotas.filter(pagada=False).order_by('fecha_vencimiento')
    else:
        cuotas_pendientes = prestamo.cuotas.filter(pagada=False).order_by('fecha_vencimiento')

    # 2. Empezamos el reparto (Efecto Dominó)
    for cuota in cuotas_pendientes:
        if monto_restante <= 0:
            break
        
        if monto_restante >= cuota.saldo_cuota:
            # El dinero alcanza para matar esta cuota completa
            monto_restante -= cuota.saldo_cuota
            cuota.saldo_cuota = 0
            cuota.pagada = True
            cuota.save()
        else:
            # El dinero solo alcanza para un abono parcial de la cuota
            cuota.saldo_cuota -= monto_restante
            monto_restante = 0
            cuota.save()
            
    return monto_restante # Devuelve si sobró dinero por algún motivo