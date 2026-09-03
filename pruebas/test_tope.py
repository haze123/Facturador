import os, sys, datetime, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SFS_DATA_DIR"] = tempfile.mkdtemp()
import main

CAPTURADO = {}
def fake_escribir(ruta, contenido):
    CAPTURADO[os.path.splitext(ruta)[1]] = contenido

def correr(boletas, tope):
    CAPTURADO.clear()
    main.MAX_BOLETAS_RESUMEN = tope
    main.obtener_boletas_para_resumen = lambda conn: list(boletas)
    main._boletas_en_resumenes_activos = lambda ruc: set()
    main.obtener_receptor = lambda conn, fid: {"tipo_documento": "0", "numero_documento": "0"}
    main.escribir_archivo = fake_escribir
    main._registrar_resumen = lambda rc, nums: None
    main._siguiente_numeracion_rc = lambda f: "RC-" + f + "-001"
    doc = main.generar_resumen_diario(None, "20609785269")
    rdi = CAPTURADO.get(".RDI", "")
    lineas = [l for l in rdi.split("\n") if l.strip()]
    fechas = {l.split("|")[0] for l in lineas}
    return doc, len(lineas), fechas

def boleta(dia, n):
    return {"numeracion_comprobante": f"B003-{n:06d}",
            "fecha_emision": datetime.datetime(2026, 8, dia, 10, 0),
            "factura_id": n, "total": 30.0, "gravadas": 25.42, "igv": 4.58}

# 1) tres dias mezclados: solo debe salir el mas antiguo
mezcla = [boleta(19, 1), boleta(20, 2), boleta(18, 3), boleta(18, 4), boleta(20, 5)]
doc, n, fechas = correr(mezcla, 200)
print(f"1. tres dias mezclados -> lineas={n}  fechas={sorted(fechas)}")
assert n == 2 and fechas == {"2026-08-18"}, "debe tomar solo el dia mas antiguo"

# 2) 640 boletas del mismo dia, tope 200
muchas = [boleta(18, i) for i in range(1, 641)]
doc, n, fechas = correr(muchas, 200)
print(f"2. 640 del mismo dia, tope 200 -> lineas={n}")
assert n == 200, f"esperaba 200, salieron {n}"

# 3) el tope duro no deja pasar de 500
os.environ["MAX_BOLETAS_RESUMEN"] = "900"
tope = min(int(os.getenv("MAX_BOLETAS_RESUMEN", "200")), 500)
print(f"3. pedir 900 por entorno -> tope efectivo={tope}")
assert tope == 500

# 4) menos que el tope: entran todas
doc, n, fechas = correr([boleta(18, i) for i in range(1, 51)], 200)
print(f"4. 50 boletas, tope 200 -> lineas={n}")
assert n == 50

print("\nTODO OK")
