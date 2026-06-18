import os
import json
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from google.oauth2 import service_account

app = Flask(__name__)
CORS(app)

def run_with_retry(func, *args, **kwargs):
    """
    Ejecuta una función de la API de Google Sheets con un mecanismo de 
    reintentos automáticos y retroceso exponencial ante bloqueos de cuota (HTTP 429 o 5xx).
    """
    retries = 5
    delay = 1.0
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            code = e.response.status_code if hasattr(e, 'response') and e.response else None
            # Reintentar en caso de límite de cuota (429) o errores de servidor de Google (500, 502, 503, 504)
            if code in [429, 500, 502, 503, 504] and i < retries - 1:
                time.sleep(delay)
                delay *= 2.0
                continue
            raise e
        except Exception as e:
            raise e

def get_gspread_client():
    """Autentica y obtiene el cliente de Google Sheets usando google-auth."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("La variable de entorno GOOGLE_CREDENTIALS no está configurada.")
    
    info = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def get_spreadsheet():
    """Conecta con la hoja de cálculo específica utilizando su ID con reintentos."""
    client = get_gspread_client()
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("La variable de entorno SPREADSHEET_ID no está configurada.")
    return run_with_retry(client.open_by_key, spreadsheet_id)

def init_sheets_if_needed(spreadsheet):
    """Crea e inicializa las pestañas requeridas en la hoja de cálculo de Google si no existen."""
    sheets_definition = {
        "Inventario": ["Codigo", "Nombre", "Costo", "Precio", "Stock"],
        "Ventas": ["ID_Venta", "Fecha", "Codigo", "Nombre", "Cantidad", "PrecioVenta", "Total"],
        "Cajas": ["ID_Caja", "FechaApertura", "FechaCierre", "MontoInicial", "MontoVentas", "MontoFinalReal", "Diferencia", "Estado"]
    }
    
    existing_sheets = [ws.title for ws in run_with_retry(spreadsheet.worksheets)]
    
    for sheet_name, headers in sheets_definition.items():
        if sheet_name not in existing_sheets:
            worksheet = run_with_retry(spreadsheet.add_worksheet, title=sheet_name, rows="100", cols=str(len(headers)))
            run_with_retry(worksheet.append_row, headers)
        else:
            worksheet = run_with_retry(spreadsheet.worksheet, sheet_name)
            row1 = run_with_retry(worksheet.row_values, 1)
            if not row1:
                run_with_retry(worksheet.append_row, headers)

def get_worksheet_safely(spreadsheet, name):
    """
    Obtiene una pestaña de forma optimizada. Intenta leer directamente.
    Si no existe (WorksheetNotFound), inicializa el esquema y vuelve a intentar.
    Esto ahorra el 80% de llamadas API innecesarias en flujos normales.
    """
    try:
        return run_with_retry(spreadsheet.worksheet, name)
    except WorksheetNotFound:
        init_sheets_if_needed(spreadsheet)
        return run_with_retry(spreadsheet.worksheet, name)

# --- ENDPOINTS API ---

@app.route('/api/health', methods=['GET'])
def health_check():
    """Valida la conexión y fuerza la inicialización de las tablas."""
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
        sheet = get_worksheet_safely(sh, "Inventario")
        records = run_with_retry(sheet.get_all_records)
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
        sheet = get_worksheet_safely(sh, "Inventario")
        records = run_with_retry(sheet.get_all_records)
        
        row_index = -1
        for idx, rec in enumerate(records):
            if str(rec["Codigo"]).strip() == code:
                row_index = idx + 2
                break
                
        new_row = [code, name, cost, price, stock]
        
        if row_index != -1:
            run_with_retry(sheet.update, range_name=f"A{row_index}:E{row_index}", values=[new_row])
            action = "updated"
        else:
            run_with_retry(sheet.append_row, new_row)
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
        sheet = get_worksheet_safely(sh, "Inventario")
        records = run_with_retry(sheet.get_all_records)
        
        row_index = -1
        for idx, rec in enumerate(records):
            if str(rec["Codigo"]).strip() == code:
                row_index = idx + 2
                break
                
        if row_index == -1:
            return jsonify({"error": "Producto no encontrado."}), 404
            
        run_with_retry(sheet.delete_rows, row_index)
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
        custom_price = data.get("PrecioVenta")
        
        if not code or quantity <= 0:
            return jsonify({"error": "Código válido y cantidad mayor a cero requeridos."}), 400
            
        sh = get_spreadsheet()
        inv_sheet = get_worksheet_safely(sh, "Inventario")
        records = run_with_retry(inv_sheet.get_all_records)
        
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
            
        sale_price = float(custom_price) if custom_price is not None else float(product["Precio"])
        total_sale = sale_price * quantity
        
        new_stock = current_stock - quantity
        run_with_retry(inv_sheet.update_cell, row_index, 5, new_stock)
        
        sales_sheet = get_worksheet_safely(sh, "Ventas")
        sale_id = f"V-{int(datetime.now().timestamp())}"
        sale_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        run_with_retry(sales_sheet.append_row, [
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
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")
        
        sh = get_spreadsheet()
        sheet = get_worksheet_safely(sh, "Ventas")
        records = run_with_retry(sheet.get_all_records)
        
        if not start_date_str or not end_date_str:
            return jsonify(records), 200
            
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        
        filtered_records = []
        for rec in records:
            rec_date_str = rec["Fecha"].split(" ")[0]
            try:
                rec_date = datetime.strptime(rec_date_str, "%Y-%m-%d").date()
                if start_date <= rec_date <= end_date:
                    filtered_records.append(rec)
            except ValueError:
                continue
                
        return jsonify(filtered_records), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- CUADRE DE CAJA ---

@app.route('/api/cash', methods=['GET'])
def get_cash_status():
    """Obtiene el estado de la caja actual."""
    try:
        sh = get_spreadsheet()
        sheet = get_worksheet_safely(sh, "Cajas")
        records = run_with_retry(sheet.get_all_records)
        
        if not records:
            return jsonify({"status": "no_history", "active_box": None}), 200
            
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
        sheet = get_worksheet_safely(sh, "Cajas")
        records = run_with_retry(sheet.get_all_records)
        
        if records and records[-1]["Estado"] == "Abierta":
            return jsonify({"error": "Ya existe una caja abierta actualmente."}), 400
            
        box_id = f"CAJA-{int(datetime.now().timestamp())}"
        open_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        run_with_retry(sheet.append_row, [
            box_id,
            open_date,
            "-",
            initial_amount,
            0,
            "-",
            "-",
            "Abierta"
        ])
        
        return jsonify({"status": "success", "message": "Caja abierta con éxito.", "box_id": box_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cash/close', methods=['POST'])
def close_cash():
    """Realiza el cuadre de caja calculando diferencias."""
    try:
        data = request.json
        final_real_cash = float(data.get("MontoFinalReal", 0))
        
        sh = get_spreadsheet()
        cajas_sheet = get_worksheet_safely(sh, "Cajas")
        cajas_records = run_with_retry(cajas_sheet.get_all_records)
        
        if not cajas_records or cajas_records[-1]["Estado"] != "Abierta":
            return jsonify({"error": "No hay ninguna caja abierta para cerrar."}), 400
            
        active_box = cajas_records[-1]
        row_index = len(cajas_records) + 1
        
        sales_sheet = get_worksheet_safely(sh, "Ventas")
        sales_records = run_with_retry(sales_sheet.get_all_records)
        
        opening_time = datetime.strptime(active_box["FechaApertura"], "%Y-%m-%d %H:%M:%S")
        total_sales_in_period = 0.0
        
        for sale in sales_records:
            sale_time = datetime.strptime(sale["Fecha"], "%Y-%m-%d %H:%M:%S")
            if sale_time >= opening_time:
                total_sales_in_period += float(sale["Total"])
                
        expected_cash = float(active_box["MontoInicial"]) + total_sales_in_period
        difference = final_real_cash - expected_cash
        close_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        run_with_retry(cajas_sheet.update_cell, row_index, 3, close_date)
        run_with_retry(cajas_sheet.update_cell, row_index, 5, total_sales_in_period)
        run_with_retry(cajas_sheet.update_cell, row_index, 6, final_real_cash)
        run_with_retry(cajas_sheet.update_cell, row_index, 7, difference)
        run_with_retry(cajas_sheet.update_cell, row_index, 8, "Cerrada")
        
        return jsonify({
            "status": "success",
            "message": "Caja cerrada exitosamente.",
            "monto_inicial": active_box["MontoInicial"],
            "ventas_acumuladas": total_sales_in_period,
            "balance_esperado": expected_cash,
            "monto_real": final_real_cash,
            "diferencia": difference
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/')
def index():
    return app.send_static_file('index.html')

if __name__ == '__main__':
    app.run(debug=True)
