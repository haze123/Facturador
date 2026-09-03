import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SFS_DATA_DIR"] = tempfile.mkdtemp()
import main, logging
logging.disable(logging.CRITICAL)

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)

NOTA_SIN_MOTIVO = {
    "tipo_nota": None,
    "tipo_documento_afectado": "01",
    "numeracion_documento_afectado": "FC03-000018",
    "motivo_documento_afectado": None,
}
NOTA_CON_MOTIVO = dict(NOTA_SIN_MOTIVO, tipo_nota="06")

# 1) comportamiento actual (sin configurar): NO se emite
main.MOTIVO_NOTA_POR_DEFECTO = ""
r = main._referencia_nota(dict(NOTA_SIN_MOTIVO), "07", "FC03-000019")
check(r is None, "sin configurar, la nota sin motivo NO se emite (comportamiento previo intacto)")

# 2) con el defecto en 01: se emite con motivo 01 y su descripcion
main.MOTIVO_NOTA_POR_DEFECTO = "01"
r = main._referencia_nota(dict(NOTA_SIN_MOTIVO), "07", "FC03-000019")
check(r is not None, "con MOTIVO_NOTA_POR_DEFECTO=01, la nota SI se emite")
if r:
    cod, des, tip, num = r
    check(cod == "01", f"codigo de motivo = 01 ({cod})")
    check(des == "ANULACION DE LA OPERACION", f"descripcion correcta ({des})")
    check(num == "FC03-000018", f"conserva el documento afectado ({num})")

# 3) si la nota YA trae motivo, el defecto no lo pisa
r = main._referencia_nota(dict(NOTA_CON_MOTIVO), "07", "FC03-000020")
check(r and r[0] == "06", f"un motivo existente no se pisa ({r[0] if r else None})")
check(r and r[1] == "DEVOLUCION TOTAL", f"y usa su propia descripcion ({r[1] if r else None})")

# 4) faltando OTRO dato, el defecto no lo tapa
sin_afectado = dict(NOTA_SIN_MOTIVO, numeracion_documento_afectado=None)
r = main._referencia_nota(sin_afectado, "07", "FC03-000021")
check(r is None, "si falta el documento afectado, sigue sin emitirse")

# 5) normalizacion: un "1" suelto se convierte en "01"
main.MOTIVO_NOTA_POR_DEFECTO = "1"
r = main._referencia_nota(dict(NOTA_SIN_MOTIVO), "07", "FC03-000022")
check(r and r[0] == "01", f"'1' se normaliza a '01' ({r[0] if r else None})")

# 6) nota de debito (08) con su propio catalogo
main.MOTIVO_NOTA_POR_DEFECTO = "01"
r = main._referencia_nota(dict(NOTA_SIN_MOTIVO), "08", "ND03-000001")
check(r and r[1] == "INTERES POR MORA", f"catalogo 10 para ND ({r[1] if r else None})")

# 7) codigo que NO existe en el catalogo: se emite igual?
main.MOTIVO_NOTA_POR_DEFECTO = "99"
r = main._referencia_nota(dict(NOTA_SIN_MOTIVO), "07", "FC03-000023")
print(f"  -> con motivo invalido '99': {'SE EMITE' if r else 'no se emite'}, descripcion={r[1] if r else None}")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
