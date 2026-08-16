"""
Borra de la base del SFS los datos del contribuyente anterior.

Lo usa instalar.ps1 cuando reutiliza una instalacion existente para otro cliente.
Sin esto, el SFS conservaria el RUC, las credenciales SOL y el historial de
comprobantes del contribuyente anterior — y emitir asi seria facturar a su nombre.

Vive aparte y no incrustado en el .ps1 porque en un here-string de PowerShell las
comillas dobles no se escapan duplicandolas: el Python llegaria con '""SELECT""'
y reventaria recien en tiempo de ejecucion.

Uso:  python _limpiar_contribuyente.py <ruta_BDFacturador.db>
"""
import sqlite3
import sys

# Se vacian los valores pero NO se borran las filas: el SFS espera encontrarlas
# y las actualiza por COD_PARA cuando se le cargan los datos nuevos.
_CAMPOS = (
    "NUMRUC", "RAZON", "NOMCOM",
    "USUSOL", "CLASOL", "USUSOLPRINCIPAL", "CLASOLPRINCIPAL",
    "NOMCERT", "PRKCRT",
    "UBIGEO", "DIRECC", "DEPAR", "PROVIN", "DISTR", "URBANIZA",
    "CLIENT_ID", "CLIENT_SECRET",
)


def limpiar(ruta):
    con = sqlite3.connect(ruta)
    try:
        marcas = ",".join("?" * len(_CAMPOS))
        cur = con.execute(
            f"UPDATE PARAMETRO SET VAL_PARA='' WHERE COD_PARA IN ({marcas})", _CAMPOS
        )
        parametros = cur.rowcount
        # El historial de comprobantes es del contribuyente anterior: si quedara,
        # el daemon lo leeria como documentos propios ya emitidos.
        cur = con.execute("DELETE FROM DOCUMENTO")
        documentos = cur.rowcount
        con.commit()
        print(f"ok|{parametros}|{documentos}")
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("error|falta la ruta de BDFacturador.db")
        sys.exit(1)
    try:
        limpiar(sys.argv[1])
    except Exception as e:
        print(f"error|{type(e).__name__}: {e}")
        sys.exit(1)
