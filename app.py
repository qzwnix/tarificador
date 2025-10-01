from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session, send_file
from config.database import db
from datetime import datetime, timedelta
import hashlib
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import openpyxl
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'tarificador_secret_key_2025'

# Configuraciones para desarrollo
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.jinja_env.auto_reload = True

# Función para hashear passwords
def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

# Middleware de autenticación
def login_required(role=None):
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash('Debe iniciar sesión para acceder a esta página', 'danger')
                return redirect(url_for('login'))
            
            if role and session.get('user_role') != role:
                flash('No tiene permisos para acceder a esta página', 'danger')
                return redirect(url_for('dashboard'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# =============================================
# FUNCIONES AUXILIARES - SISTEMA DE PULSOS Y PERIODOS
# =============================================

# Función para determinar tipo de destino
def determinar_tipo_destino(numero_destino):
    if not numero_destino:
        return 'convencional'
    
    numero_limpio = str(numero_destino).strip()
    
    # Internacional
    if numero_limpio.startswith('+'):
        return 'internacional'
    
    # Celular (8 dígitos que empiezan con 5,7,8)
    if len(numero_limpio) == 8 and numero_limpio[0] in ['5', '7', '8']:
        return 'celular'
    
    # Convencional (8 dígitos que no empiezan con 5,7,8)
    if len(numero_limpio) == 8:
        return 'convencional'
    
    # Por defecto
    return 'convencional'

# Función simplificada para calcular costo (como fallback)
def calcular_costo_simplificado(numero_destino, duracion_minutos):
    tipo_destino = determinar_tipo_destino(numero_destino)
    
    # Tarifas simplificadas
    tarifas = {
        'convencional': 0.02,
        'celular': 0.08, 
        'internacional': 0.50
    }
    
    costo_por_minuto = tarifas.get(tipo_destino, 0.05)  # Default
    return costo_por_minuto * duracion_minutos

# NUEVA FUNCIÓN: Sistema de cálculo con pulsos
def calcular_costo_con_pulsos(numero_origen, numero_destino, duracion_segundos):
    try:
        # Obtener configuración de pulsos
        config_pulso = db.execute_query("SELECT TOP 1 * FROM configuracion_pulsos ORDER BY id DESC")
        
        if config_pulso:
            duracion_pulso = config_pulso[0]['duracion_pulso_segundos']
            redondeo = bool(config_pulso[0]['redondeo_pulso'])
        else:
            # Valores por defecto
            duracion_pulso = 60
            redondeo = True
        
        # Calcular número de pulsos
        if redondeo:
            # Redondear hacia arriba (ej: 61 segundos = 2 pulsos)
            pulsos = (duracion_segundos + duracion_pulso - 1) // duracion_pulso
        else:
            # Redondear hacia abajo
            pulsos = duracion_segundos // duracion_pulso
        
        # Obtener tarifa por pulso
        tipo_origen = determinar_tipo_destino(numero_origen)
        tipo_destino = determinar_tipo_destino(numero_destino)
        
        tarifa_query = """
            SELECT TOP 1 costo_minuto 
            FROM tarifas 
            WHERE tipo_origen = ? AND tipo_destino = ?
        """
        
        tarifas = db.execute_query(tarifa_query, (tipo_origen, tipo_destino))
        
        if tarifas:
            # Convertir costo por minuto a costo por pulso
            costo_por_pulso = float(tarifas[0]['costo_minuto'])
        else:
            costo_por_pulso = 0.05  # Default
        
        # Calcular costo total
        costo_total = pulsos * costo_por_pulso
        
        print(f"📊 Cálculo de Pulsos:")
        print(f"   Duración: {duracion_segundos} segundos")
        print(f"   Pulso config: {duracion_pulso} segundos")
        print(f"   Pulsos consumidos: {pulsos}")
        print(f"   Costo por pulso: ${costo_por_pulso:.4f}")
        print(f"   Costo total: ${costo_total:.2f}")
        
        return costo_total, pulsos
        
    except Exception as e:
        print(f"Error en cálculo de pulsos: {e}")
        # Fallback - cálculo simplificado por minutos
        costo_simplificado = calcular_costo_simplificado(numero_destino, duracion_segundos // 60)
        return costo_simplificado, 1

# NUEVA FUNCIÓN: Sistema automático de periodos
def obtener_o_crear_periodo_actual():
    """Obtiene el periodo actual o crea uno nuevo si no existe"""
    try:
        # Obtener mes y año actual
        ahora = datetime.now()
        # Nombres de meses en español
        meses_espanol = {
            1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril', 5: 'Mayo', 6: 'Junio',
            7: 'Julio', 8: 'Agosto', 9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'
        }
        mes_actual = f"{meses_espanol[ahora.month]} {ahora.year}"
        fecha_inicio = ahora.replace(day=1).date()
        
        # Calcular último día del mes
        if ahora.month == 12:
            fecha_fin = ahora.replace(year=ahora.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            fecha_fin = ahora.replace(month=ahora.month + 1, day=1) - timedelta(days=1)
        
        fecha_fin = fecha_fin.date()
        
        # Verificar si ya existe el periodo
        periodo_existente = db.execute_query(
            "SELECT * FROM periodos_facturacion WHERE nombre = ?", 
            (mes_actual,)
        )
        
        if periodo_existente:
            print(f"✅ Periodo actual encontrado: {mes_actual}")
            return periodo_existente[0]
        else:
            # Crear nuevo periodo
            result = db.execute_query("""
                INSERT INTO periodos_facturacion (nombre, fecha_inicio, fecha_fin, estado)
                VALUES (?, ?, ?, 'abierto')
            """, (mes_actual, fecha_inicio, fecha_fin))
            
            if result:
                # Obtener el periodo recién creado
                nuevo_periodo = db.execute_query(
                    "SELECT * FROM periodos_facturacion WHERE nombre = ?", 
                    (mes_actual,)
                )
                print(f"✅ Nuevo periodo creado: {mes_actual} ({fecha_inicio} a {fecha_fin})")
                return nuevo_periodo[0] if nuevo_periodo else None
        
        return None
        
    except Exception as e:
        print(f"❌ Error creando periodo actual: {e}")
        return None

# =============================================
# RUTAS DE LA APLICACIÓN
# =============================================

# Ruta principal
@app.route('/')
def index():
    return redirect(url_for('login'))

# Ruta de login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Buscar usuario
        user_query = "SELECT * FROM usuarios WHERE username = ? AND activo = 1"
        users = db.execute_query(user_query, (username,))
        
        if users:
            user = users[0]
            
            # Verificar contraseña
            if user['password_hash'] == hash_password(password):
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['user_role'] = user['rol']
                session['user_name'] = user['nombre_completo']
                
                flash(f'Bienvenido {user["nombre_completo"]}', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Contraseña incorrecta', 'danger')
        else:
            flash('Usuario no encontrado', 'danger')
    
    return render_template('login.html')

# Ruta de logout
@app.route('/logout')
def logout():
    session.clear()
    flash('Sesión cerrada correctamente', 'info')
    return redirect(url_for('login'))

# Dashboard principal
@app.route('/dashboard')
@login_required()
def dashboard():
    try:
        # Crear periodo actual automáticamente
        periodo_actual = obtener_o_crear_periodo_actual()
        
        # Estadísticas
        total_contactos_result = db.execute_query("SELECT COUNT(*) as count FROM contactos")
        total_contactos = total_contactos_result[0]['count'] if total_contactos_result else 0
        
        total_llamadas_result = db.execute_query("SELECT COUNT(*) as count FROM llamadas")
        total_llamadas = total_llamadas_result[0]['count'] if total_llamadas_result else 0
        
        total_facturas_result = db.execute_query("SELECT COUNT(*) as count FROM facturas")
        total_facturas = total_facturas_result[0]['count'] if total_facturas_result else 0
        
        # Llamadas recientes
        llamadas_recientes = db.execute_query("""
            SELECT TOP 5 l.*, c.nombre as contacto_nombre 
            FROM llamadas l 
            JOIN contactos c ON l.contacto_origen_id = c.id 
            ORDER BY l.fecha_llamada DESC
        """) or []
        
        # Cargar contactos para el modal
        contactos = db.execute_query("""
            SELECT c.id, c.nombre, c.numero, t.tipo as tipo_numero
            FROM contactos c
            LEFT JOIN tipos_numero t ON c.tipo_numero_id = t.id
            ORDER BY c.nombre
        """) or []
        
        return render_template('dashboard.html',
                            total_contactos=total_contactos,
                            total_llamadas=total_llamadas,
                            total_facturas=total_facturas,
                            llamadas_recientes=llamadas_recientes,
                            contactos=contactos,
                            user=session)
    except Exception as e:
        flash(f'Error al cargar dashboard: {str(e)}', 'danger')
        return render_template('dashboard.html', 
                            total_contactos=0,
                            total_llamadas=0, 
                            total_facturas=0,
                            llamadas_recientes=[],
                            contactos=[],
                            user=session)

@app.route('/llamadas/simular', methods=['POST'])
@login_required()
def simular_llamada():
    try:
        contacto_origen_id = request.form.get('contacto_origen_id')
        numero_destino = request.form.get('numero_destino')
        duracion = request.form.get('duracion', 5)
        
        print(f"📞 Datos recibidos: contacto={contacto_origen_id}, destino={numero_destino}, duracion={duracion}")
        
        if not contacto_origen_id or not numero_destino:
            flash("Contacto origen y número destino son obligatorios", "danger")
            return redirect(url_for('dashboard'))
        
        # Convertir a enteros
        contacto_origen_id = int(contacto_origen_id)
        duracion = int(duracion)
        
        # Obtener información del contacto origen
        contacto_result = db.execute_query(
            "SELECT * FROM contactos WHERE id = ?", 
            (contacto_origen_id,)
        )
        if not contacto_result:
            flash("Contacto origen no encontrado", "danger")
            return redirect(url_for('dashboard'))
        
        contacto = contacto_result[0]
        print(f"✅ Contacto encontrado: {contacto['nombre']}")
        
        # ==== SISTEMA DE PULSOS - CÁLCULO MEJORADO ====
        duracion_segundos = duracion * 60
        costo_total, pulsos_consumidos = calcular_costo_con_pulsos(
            contacto['numero'], numero_destino, duracion_segundos
        )
        print(f"💰 Costo calculado: ${costo_total:.2f} ({pulsos_consumidos} pulsos)")
        
        # Determinar tipo de destino
        tipo_destino = determinar_tipo_destino(numero_destino)
        
        # INSERT con sistema de pulsos
        insert_query = """
        INSERT INTO llamadas (
            contacto_origen_id, numero_destino, tipo_destino, 
            duracion_segundos, costo_total
        ) VALUES (?, ?, ?, ?, ?)
        """
        
        print(f"🚀 Ejecutando INSERT: {contacto_origen_id}, {numero_destino}, {tipo_destino}, {duracion_segundos}, {costo_total}")
        
        result = db.execute_query(insert_query, (
            contacto_origen_id, numero_destino, tipo_destino,
            duracion_segundos, costo_total
        ))
        
        print(f"📊 Resultado de inserción: {result}")
        
        if result is not None and result > 0:
            flash(f"✅ Llamada registrada exitosamente! {pulsos_consumidos} pulsos, Costo: ${costo_total:.2f}", "success")
        else:
            flash("❌ Error: No se pudo insertar en la base de datos", "danger")
            
    except Exception as e:
        print(f"🔥 Error completo: {str(e)}")
        import traceback
        print(f"📝 Traceback: {traceback.format_exc()}")
        flash(f"❌ Error: {str(e)}", "danger")
    
    return redirect(url_for('dashboard'))

# Gestión de contactos
@app.route('/contactos')
@login_required()
def gestion_contactos():
    contactos = db.execute_query("""
        SELECT c.*, t.tipo as tipo_numero, o.nombre as operadora, d.nombre as departamento
        FROM contactos c
        LEFT JOIN tipos_numero t ON c.tipo_numero_id = t.id
        LEFT JOIN operadoras o ON c.operadora_id = o.id
        LEFT JOIN departamentos d ON c.departamento_id = d.id
        ORDER BY c.nombre
    """) or []
    
    departamentos = db.execute_query("SELECT * FROM departamentos ORDER BY nombre") or []
    
    return render_template('contactos.html', 
                         contactos=contactos, 
                         departamentos=departamentos,
                         user=session)

@app.route('/contactos/guardar', methods=['POST'])
@login_required()
def guardar_contacto():
    try:
        nombre = request.form.get('nombre')
        numero = request.form.get('numero')
        tipo_numero_id = request.form.get('tipo_numero_id')
        operadora_id = request.form.get('operadora_id')
        departamento_id = request.form.get('departamento_id')
        
        if not nombre or not numero or not tipo_numero_id:
            flash("Nombre, número y tipo de número son obligatorios", "danger")
            return redirect(url_for('gestion_contactos'))
        
        # Verificar si el número ya existe
        existing = db.execute_query(
            "SELECT id FROM contactos WHERE numero = ?", 
            (numero,)
        )
        
        if existing:
            flash("El número de teléfono ya existe", "danger")
            return redirect(url_for('gestion_contactos'))
        
        # Insertar contacto
        result = db.execute_query("""
            INSERT INTO contactos (nombre, numero, tipo_numero_id, operadora_id, departamento_id)
            VALUES (?, ?, ?, ?, ?)
        """, (nombre, numero, tipo_numero_id, operadora_id, departamento_id))
        
        if result:
            flash("Contacto guardado exitosamente", "success")
        else:
            flash("Error al guardar contacto", "danger")
            
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('gestion_contactos'))

@app.route('/contactos/eliminar/<int:contacto_id>')
@login_required()
def eliminar_contacto(contacto_id):
    try:
        result = db.execute_query("DELETE FROM contactos WHERE id = ?", (contacto_id,))
        if result:
            flash("Contacto eliminado exitosamente", "success")
        else:
            flash("Error al eliminar contacto", "danger")
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('gestion_contactos'))

# Gestión de facturación
@app.route('/facturacion')
@login_required()
def gestion_facturacion():
    # Crear periodo actual automáticamente
    periodo_actual = obtener_o_crear_periodo_actual()
    
    periodos = db.execute_query("SELECT * FROM periodos_facturacion ORDER BY fecha_inicio DESC") or []
    facturas = db.execute_query("""
        SELECT f.*, c.nombre as contacto_nombre, p.nombre as periodo_nombre
        FROM facturas f
        JOIN contactos c ON f.contacto_id = c.id
        JOIN periodos_facturacion p ON f.periodo_id = p.id
        ORDER BY f.fecha_generacion DESC
    """) or []
    
    return render_template('facturacion.html',
                         periodos=periodos,
                         facturas=facturas,
                         user=session)

@app.route('/facturacion/generar', methods=['POST'])
@login_required(role='admin')
def generar_facturacion():
    try:
        periodo_id = request.form.get('periodo_id')
        
        if not periodo_id:
            flash("Seleccione un periodo", "danger")
            return redirect(url_for('gestion_facturacion'))
        
        # Obtener periodo
        periodo_result = db.execute_query(
            "SELECT * FROM periodos_facturacion WHERE id = ?", 
            (periodo_id,)
        )
        if not periodo_result:
            flash("Periodo no encontrado", "danger")
            return redirect(url_for('gestion_facturacion'))
        
        periodo = periodo_result[0]
        
        print(f"📅 Periodo seleccionado: {periodo['nombre']} ({periodo['fecha_inicio']} a {periodo['fecha_fin']})")
        
        # CONSULTA SIMPLE Y EFECTIVA (como en tu versión que funciona)
        llamadas_periodo = db.execute_query("""
            SELECT contacto_origen_id, SUM(costo_total) as total
            FROM llamadas 
            WHERE fecha_llamada >= ? AND fecha_llamada <= ?
            GROUP BY contacto_origen_id
            HAVING SUM(costo_total) > 0
        """, (periodo['fecha_inicio'], periodo['fecha_fin']))
        
        print(f"📊 Llamadas encontradas en el periodo: {len(llamadas_periodo or [])}")
        
        # DEBUG: Ver qué contactos se van a facturar
        if llamadas_periodo:
            for llamada in llamadas_periodo:
                print(f"💰 Contacto {llamada['contacto_origen_id']}: ${llamada['total']:.2f}")
        
        # Eliminar facturas existentes para este periodo
        db.execute_query("DELETE FROM facturas WHERE periodo_id = ?", (periodo_id,))
        
        # Generar facturas
        facturas_generadas = 0
        total_recaudado = 0
        
        if llamadas_periodo:
            for llamada in llamadas_periodo:
                if llamada['total'] and llamada['total'] > 0:
                    print(f"✅ Generando factura para contacto {llamada['contacto_origen_id']}: ${llamada['total']:.2f}")
                    
                    db.execute_query("""
                        INSERT INTO facturas (contacto_id, periodo_id, total, fecha_generacion, estado)
                        VALUES (?, ?, ?, GETDATE(), 'pendiente')
                    """, (llamada['contacto_origen_id'], periodo_id, llamada['total']))
                    
                    facturas_generadas += 1
                    total_recaudado += llamada['total']
        
        if facturas_generadas > 0:
            flash(f"✅ Facturación generada: {facturas_generadas} facturas creadas. Total: ${total_recaudado:.2f}", "success")
        else:
            flash("ℹ️ No se encontraron llamadas con costo en el periodo seleccionado", "info")
        
    except Exception as e:
        print(f"🔥 Error al generar facturación: {str(e)}")
        flash(f"❌ Error al generar facturación: {str(e)}", "danger")
    
    return redirect(url_for('gestion_facturacion'))

# Ruta para forzar creación del periodo actual
@app.route('/facturacion/periodo/actual')
@login_required(role='admin')
def crear_periodo_actual():
    """Forzar la creación del periodo actual (para testing)"""
    periodo = obtener_o_crear_periodo_actual()
    if periodo:
        flash(f"✅ Periodo actual creado: {periodo['nombre']}", "success")
    else:
        flash("❌ Error creando periodo actual", "danger")
    return redirect(url_for('gestion_facturacion'))

# Gestión de tarifas
@app.route('/tarifas')
@login_required()
def gestion_tarifas():
    tarifas = db.execute_query("SELECT * FROM tarifas ORDER BY tipo_origen, tipo_destino") or []
    return render_template('tarifas.html', tarifas=tarifas, user=session)

@app.route('/tarifas/guardar', methods=['POST'])
@login_required(role='admin')
def guardar_tarifa():
    try:
        tipo_origen = request.form.get('tipo_origen')
        operadora_origen = request.form.get('operadora_origen') or None
        tipo_destino = request.form.get('tipo_destino')
        operadora_destino = request.form.get('operadora_destino') or None
        misma_region = request.form.get('misma_region', 0)
        costo_minuto = request.form.get('costo_minuto')
        descripcion = request.form.get('descripcion') or None
        
        if not tipo_origen or not tipo_destino or not costo_minuto:
            flash("Tipo origen, tipo destino y costo son obligatorios", "danger")
            return redirect(url_for('gestion_tarifas'))
        
        # Verificar si ya existe una tarifa similar
        existing_query = """
            SELECT id FROM tarifas 
            WHERE tipo_origen = ? AND tipo_destino = ? 
            AND (operadora_origen = ? OR operadora_origen IS NULL)
            AND (operadora_destino = ? OR operadora_destino IS NULL)
            AND misma_region = ?
        """
        existing = db.execute_query(existing_query, 
            (tipo_origen, tipo_destino, operadora_origen, operadora_destino, misma_region))
        
        if existing:
            flash("Ya existe una tarifa con estas características", "danger")
            return redirect(url_for('gestion_tarifas'))
        
        # Insertar tarifa
        result = db.execute_query("""
            INSERT INTO tarifas (tipo_origen, operadora_origen, tipo_destino, 
                               operadora_destino, misma_region, costo_minuto, descripcion)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tipo_origen, operadora_origen, tipo_destino, 
              operadora_destino, misma_region, costo_minuto, descripcion))
        
        if result:
            flash("Tarifa guardada exitosamente", "success")
        else:
            flash("Error al guardar tarifa", "danger")
            
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('gestion_tarifas'))

@app.route('/tarifas/actualizar', methods=['POST'])
@login_required(role='admin')
def actualizar_tarifa():
    try:
        tarifa_id = request.form.get('tarifa_id')
        tipo_origen = request.form.get('tipo_origen')
        operadora_origen = request.form.get('operadora_origen') or None
        tipo_destino = request.form.get('tipo_destino')
        operadora_destino = request.form.get('operadora_destino') or None
        misma_region = request.form.get('misma_region', 0)
        costo_minuto = request.form.get('costo_minuto')
        descripcion = request.form.get('descripcion') or None
        
        if not tarifa_id or not tipo_origen or not tipo_destino or not costo_minuto:
            flash("Datos incompletos", "danger")
            return redirect(url_for('gestion_tarifas'))
        
        # Actualizar tarifa
        result = db.execute_query("""
            UPDATE tarifas 
            SET tipo_origen = ?, operadora_origen = ?, tipo_destino = ?,
                operadora_destino = ?, misma_region = ?, costo_minuto = ?, descripcion = ?
            WHERE id = ?
        """, (tipo_origen, operadora_origen, tipo_destino, 
              operadora_destino, misma_region, costo_minuto, descripcion, tarifa_id))
        
        if result:
            flash("Tarifa actualizada exitosamente", "success")
        else:
            flash("Error al actualizar tarifa", "danger")
            
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('gestion_tarifas'))

@app.route('/tarifas/eliminar/<int:tarifa_id>')
@login_required(role='admin')
def eliminar_tarifa(tarifa_id):
    try:
        result = db.execute_query("DELETE FROM tarifas WHERE id = ?", (tarifa_id,))
        if result:
            flash("Tarifa eliminada exitosamente", "success")
        else:
            flash("Error al eliminar tarifa", "danger")
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('gestion_tarifas'))

# Reportes y estadísticas
@app.route('/reportes')
@login_required()
def reportes():
    try:
        print("📊 Generando reportes...")
        
        # Estadísticas por departamento
        stats_departamentos = db.execute_query("""
            SELECT 
                d.nombre,
                COUNT(l.id) as total_llamadas,
                ISNULL(SUM(l.costo_total), 0) as total_ingresos
            FROM departamentos d
            LEFT JOIN contactos c ON d.id = c.departamento_id
            LEFT JOIN llamadas l ON c.id = l.contacto_origen_id
            GROUP BY d.id, d.nombre
            ORDER BY total_ingresos DESC
        """) or []
        
        print(f"🏢 Departamentos con datos: {len(stats_departamentos)}")
        
        # Llamadas por tipo
        stats_tipos = db.execute_query("""
            SELECT 
                tipo_destino,
                COUNT(*) as cantidad,
                ISNULL(SUM(costo_total), 0) as ingresos
            FROM llamadas
            GROUP BY tipo_destino
            ORDER BY ingresos DESC
        """) or []
        
        print(f"📞 Tipos de llamada: {len(stats_tipos)}")
        
        # Estadísticas generales
        total_llamadas = db.execute_query("SELECT COUNT(*) as total FROM llamadas")
        total_ingresos = db.execute_query("SELECT ISNULL(SUM(costo_total), 0) as total FROM llamadas")
        
        total_llamadas_count = total_llamadas[0]['total'] if total_llamadas else 0
        total_ingresos_count = total_ingresos[0]['total'] if total_ingresos else 0
        
        print(f"📈 Totales: {total_llamadas_count} llamadas, ${total_ingresos_count:.2f} ingresos")
        
        return render_template('reportes.html',
                             stats_departamentos=stats_departamentos,
                             stats_tipos=stats_tipos,
                             total_llamadas=total_llamadas_count,
                             total_ingresos=total_ingresos_count,
                             user=session)
                             
    except Exception as e:
        print(f"🔥 Error en reportes: {str(e)}")
        import traceback
        print(f"📝 Traceback: {traceback.format_exc()}")
        
        return render_template('reportes.html',
                             stats_departamentos=[],
                             stats_tipos=[],
                             total_llamadas=0,
                             total_ingresos=0,
                             user=session)

# Exportación de reportes (mantener las funciones de exportación si las necesitas)
@app.route('/reportes/exportar/<tipo>')
@login_required()
def exportar_reportes(tipo):
    flash("Función de exportación en desarrollo", "info")
    return redirect(url_for('reportes'))

# Configuración del sistema (solo admin)
@app.route('/configuracion')
@login_required(role='admin')
def configuracion():
    troncales = db.execute_query("SELECT * FROM troncales") or []
    centrales = db.execute_query("SELECT * FROM centrales") or []
    servidores = db.execute_query("SELECT * FROM servidores") or []
    
    return render_template('configuracion.html',
                         troncales=troncales,
                         centrales=centrales,
                         servidores=servidores,
                         user=session)

# Ruta de configuración de pulsos
@app.route('/configuracion/pulsos')
@login_required(role='admin')
def configuracion_pulsos():
    config = db.execute_query("SELECT TOP 1 * FROM configuracion_pulsos ORDER BY id DESC") or []
    return render_template('config_pulsos.html', config=config[0] if config else None, user=session)

# Ruta de debug para ver usuarios
@app.route('/debug/users')
def debug_users():
    users = db.execute_query("SELECT id, username, rol FROM usuarios")
    return jsonify(users or [])

if __name__ == '__main__':
    app.run(debug=True, port=5000)
    