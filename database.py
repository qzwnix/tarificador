import pyodbc
import os

class DatabaseConfig:
    def __init__(self):
        # Configuración para Docker
        self.server = os.getenv('DB_SERVER', 'db')  # Nombre del servicio en Docker Compose
        self.database = os.getenv('DB_NAME', 'tarificador_nicaragua')
        self.username = os.getenv('DB_USER', 'sa')
        self.password = os.getenv('DB_PASSWORD', 'YourStrong!Pass123')
        self.connection_string = f'DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={self.server};DATABASE={self.database};UID={self.username};PWD={self.password}'

class Database:
    def __init__(self):
        self.config = DatabaseConfig()
    
    def get_connection(self):
        try:
            conn = pyodbc.connect(self.config.connection_string)
            return conn
        except Exception as e:
            print(f"Error de conexión: {e}")
            return None
    
    def execute_query(self, query, params=None):
        conn = self.get_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            
            if query.strip().upper().startswith('SELECT'):
                result = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row)) for row in result]
            else:
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"Error en consulta: {e}")
            return None
        finally:
            conn.close()

# Instancia global de la base de datos
db = Database()