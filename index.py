import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)
# Habilitamos CORS para desarrollo local y producción fluida
CORS(app)

def get_gspread_client():
    """Autentica y obtiene el cliente de Google Sheets usando variables de entorno."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("La variable de entorno GOOGLE_CREDENTIALS no está configurada.")
    
    # Cargar las credenciales JSON desde la variable de entorno
    info = json.loads(creds_json)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    return gspread.authorize(creds)

def get_spreadsheet():
    """Conecta con la hoja de cálculo específica utilizando su ID."""
    client = get_gspread_client()
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("La variable de entorno SPREADSHEET_ID no está configurada.")
    return client.open_by_key(spreadsheet_id)

def init_sheets_if_needed(spreadsheet):
    """Inicializa las pestañas requeridas en el archivo de Sheets si no existen."""
    # Estructura inicial de pestañas y encabezados
    sheets_definition = {
        "Inventario": ["Codigo", "Nombre", "Costo", "Precio", "Stock"],
        "Ventas": ["ID_Venta", "Fecha", "Codigo", "Nombre", "Cantidad", "PrecioVenta", "Total"],
        "Cajas": ["ID_Caja", "FechaApertura", "FechaCierre", "MontoInicial", "MontoVentas", "MontoFinalReal", "Diferencia", "Estado"]
    }
    
    existing_sheets = [ws.title for ws in spreadsheet.worksheets()]
    
    for sheet_name, headers in sheets_definition.items():
        if sheet_name not in existing_sheets:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols=str(len(headers)))
            worksheet.append_row(headers)
        else:
            # Asegurar que tenga al menos los encabezados
            worksheet = spreadsheet.worksheet(sheet_name)
            if not worksheet.row_values(1):
                worksheet.append_row(headers)

# --- ENDPOINTS API ---

@app.route('/api/health', methods=['GET'])
def health_check():
    """Valida la conexión exitosa con Google Sheets."""
    try:
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        return jsonify({
            "status": "connected", 
            "message": "Conectado exitosamente a Google Sheets",
            "spreadsheet_title": sh.title
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": str(e)
        }), 500

# --- INVENTARIO ---

@app.route('/api/inventory', methods=['GET'])
def get_inventory():
    """Obtiene todos los artículos del inventario."""
    try:
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        sheet = sh.worksheet("Inventario")
        records = sheet.get_all_records()
        return jsonify(records), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/inventory', methods=['POST'])
def save_product():
    """Crea o actualiza un producto en el inventario."""
    try:
        data = request.json
        code = str(data.get("Codigo", "")).strip()
        name = data.get("Nombre", "").strip()
        cost = float(data.get("Costo", 0))
        price = float(data.get("Precio", 0))
        stock = int(data.get("Stock", 0))
        
        if not code or not name:
            return jsonify({"error": "Código y Nombre son campos obligatorios."}), 400

        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        sheet = sh.worksheet("Inventario")
        records = sheet.get_all_records()
        
        # Buscar si el código ya existe
        row_index = -1
        for idx, rec in enumerate(records):
            if str(rec["Codigo"]).strip() == code:
                # El índice en Sheets es base 1, más el encabezado (+2)
                row_index = idx + 2
                break
                
        new_row = [code, name, cost, price, stock]
        
        if row_index != -1:
            # Actualizar fila existente
            sheet.update(range_name=f"A{row_index}:E{row_index}", values=[new_row])
            action = "updated"
        else:
            # Insertar nueva fila
            sheet.append_row(new_row)
            action = "created"
            
        return jsonify({"status": "success", "action": action, "product": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/inventory/delete', methods=['POST'])
def delete_product():
    """Elimina un producto del inventario por su código."""
    try:
        data = request.json
        code = str(data.get("Codigo", "")).strip()
        
        if not code:
            return jsonify({"error": "Se requiere el código del producto."}), 400
            
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        sheet = sh.worksheet("Inventario")
        records = sheet.get_all_records()
        
        row_index = -1
        for idx, rec in enumerate(records):
            if str(rec["Codigo"]).strip() == code:
                row_index = idx + 2
                break
                
        if row_index == -1:
            return jsonify({"error": "Producto no encontrado."}), 404
            
        sheet.delete_rows(row_index)
        return jsonify({"status": "success", "message": f"Producto con código {code} eliminado."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- VENTAS ---

@app.route('/api/sales', methods=['POST'])
def register_sale():
    """Realiza una venta de producto, descuenta inventario y registra en Ventas."""
    try:
        data = request.json
        code = str(data.get("Codigo", "")).strip()
        quantity = int(data.get("Cantidad", 1))
        custom_price = data.get("PrecioVenta") # Precio customizado de venta si se desea
        
        if not code or quantity <= 0:
            return jsonify({"error": "Código válido y cantidad mayor a cero requeridos."}), 400
            
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        
        # 1. Validar y descontar del inventario
        inv_sheet = sh.worksheet("Inventario")
        records = inv_sheet.get_all_records()
        
        product = None
        row_index = -1
        for idx, rec in enumerate(records):
            if str(rec["Codigo"]).strip() == code:
                product = rec
                row_index = idx + 2
                break
                
        if not product:
            return jsonify({"error": "El producto no existe en el inventario."}), 404
            
        current_stock = int(product["Stock"])
        if current_stock < quantity:
            return jsonify({"error": f"Stock insuficiente. Disponible: {current_stock}"}), 400
            
        # Determinar precio de venta (usar el especificado o el por defecto)
        sale_price = float(custom_price) if custom_price is not None else float(product["Precio"])
        total_sale = sale_price * quantity
        
        # Descontar stock
        new_stock = current_stock - quantity
        inv_sheet.update_cell(row_index, 5, new_stock) # Columna 5 es Stock
        
        # 2. Registrar venta en pestaña de Ventas
        sales_sheet = sh.worksheet("Ventas")
        sale_id = f"V-{int(datetime.now().timestamp())}"
        sale_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        sales_sheet.append_row([
            sale_id,
            sale_date,
            code,
            product["Nombre"],
            quantity,
            sale_price,
            total_sale
        ])
        
        return jsonify({
            "status": "success",
            "sale_id": sale_id,
            "product": product["Nombre"],
            "quantity": quantity,
            "price_applied": sale_price,
            "total": total_sale,
            "new_stock": new_stock
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/sales', methods=['GET'])
def get_sales_report():
    """Obtiene el reporte de ventas dentro de un rango de fechas."""
    try:
        start_date_str = request.args.get("start_date") # Formato YYYY-MM-DD
        end_date_str = request.args.get("end_date")     # Formato YYYY-MM-DD
        
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        sheet = sh.worksheet("Ventas")
        records = sheet.get_all_records()
        
        if not start_date_str or not end_date_str:
            return jsonify(records), 200
            
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        
        filtered_records = []
        for rec in records:
            # Extraer solo la parte de fecha "YYYY-MM-DD" del timestamp "YYYY-MM-DD HH:MM:SS"
            rec_date_str = rec["Fecha"].split(" ")[0]
            try:
                rec_date = datetime.strptime(rec_date_str, "%Y-%m-%d").date()
                if start_date <= rec_date <= end_date:
                    filtered_records.append(rec)
            except ValueError:
                continue # Omitir filas con formato de fecha inválido
                
        return jsonify(filtered_records), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- CUADRE DE CAJA ---

@app.route('/api/cash', methods=['GET'])
def get_cash_status():
    """Obtiene el estado de la caja actual (Abierta/Cerrada)."""
    try:
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        sheet = sh.worksheet("Cajas")
        records = sheet.get_all_records()
        
        if not records:
            return jsonify({"status": "no_history", "active_box": None}), 200
            
        # Revisar la última caja registrada
        last_box = records[-1]
        if last_box["Estado"] == "Abierta":
            return jsonify({"status": "open", "active_box": last_box}), 200
        else:
            return jsonify({"status": "closed", "active_box": None, "last_box": last_box}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cash/open', methods=['POST'])
def open_cash():
    """Abre un nuevo periodo de caja."""
    try:
        data = request.json
        initial_amount = float(data.get("MontoInicial", 0))
        
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        sheet = sh.worksheet("Cajas")
        records = sheet.get_all_records()
        
        if records and records[-1]["Estado"] == "Abierta":
            return jsonify({"error": "Ya existe una caja abierta actualmente. Debes cerrarla primero."}), 400
            
        box_id = f"CAJA-{int(datetime.now().timestamp())}"
        open_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Columnas: ID_Caja, FechaApertura, FechaCierre, MontoInicial, MontoVentas, MontoFinalReal, Diferencia, Estado
        sheet.append_row([
            box_id,
            open_date,
            "-", # Fecha Cierre
            initial_amount,
            0, # Ventas se inician en 0
            "-", # Monto final real
            "-", # Diferencia
            "Abierta"
        ])
        
        return jsonify({"status": "success", "message": "Caja abierta con éxito.", "box_id": box_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cash/close', methods=['POST'])
def close_cash():
    """Realiza el cuadre de caja calculando ventas acumuladas y comparando con efectivo real."""
    try:
        data = request.json
        final_real_cash = float(data.get("MontoFinalReal", 0))
        
        sh = get_spreadsheet()
        init_sheets_if_needed(sh)
        cajas_sheet = sh.worksheet("Cajas")
        cajas_records = cajas_sheet.get_all_records()
        
        if not cajas_records or cajas_records[-1]["Estado"] != "Abierta":
            return jsonify({"error": "No hay ninguna caja abierta para cerrar."}), 400
            
        active_box = cajas_records[-1]
        row_index = len(cajas_records) + 1 # Fila en Sheets
        
        # Calcular las ventas realizadas desde la apertura de caja
        sales_sheet = sh.worksheet("Ventas")
        sales_records = sales_sheet.get_all_records()
        
        opening_time = datetime.strptime(active_box["FechaApertura"], "%Y-%m-%d %H:%M:%S")
        total_sales_in_period = 0.0
        
        for sale in sales_records:
            sale_time = datetime.strptime(sale["Fecha"], "%Y-%m-%d %H:%M:%S")
            if sale_time >= opening_time:
                total_sales_in_period += float(sale["Total"])
                
        # Calcular el balance esperado
        expected_cash = float(active_box["MontoInicial"]) + total_sales_in_period
        difference = final_real_cash - expected_cash
        close_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Actualizar la fila en Sheets
        # Columnas: ID_Caja, FechaApertura, FechaCierre, MontoInicial, MontoVentas, MontoFinalReal, Diferencia, Estado
        cajas_sheet.update_cell(row_index, 3, close_date)           # Fecha Cierre
        cajas_sheet.update_cell(row_index, 5, total_sales_in_period) # Monto Ventas
        cajas_sheet.update_cell(row_index, 6, final_real_cash)       # Monto Final Real
        cajas_sheet.update_cell(row_index, 7, difference)            # Diferencia
        cajas_sheet.update_cell(row_index, 8, "Cerrada")             # Estado
        
        return jsonify({
            "status": "success",
            "message": "Caja cerrada y guardada exitosamente.",
            "monto_inicial": active_box["MontoInicial"],
            "ventas_acumuladas": total_sales_in_period,
            "balance_esperado": expected_cash,
            "monto_real": final_real_cash,
            "diferencia": difference
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Redirigir raíz al index.html
@app.route('/')
def index():
    return app.send_static_file('index.html')

if __name__ == '__main__':
    app.run(debug=True)
