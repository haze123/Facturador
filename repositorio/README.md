# Qué necesita el facturador de tu base de datos

El daemon no sabe con qué base está hablando. Pide **diez operaciones** y espera que
cada una devuelva siempre las mismas claves, vengan de PostgreSQL, de SQL Server o
—si algún día se implementa— de una API.

Este documento es el contrato. Sirve para dos cosas: escribir el adaptador de un
cliente con su propio esquema, y saber qué hay que entregarle al facturador si la
integración va a ser por otro medio.

Los dos adaptadores que ya existen son ejemplos completos y muy distintos entre sí:

| | Esquema | Tablas |
|---|---|---|
| [`postgres.py`](postgres.py) | Prisma, camelCase | `Factura`, `FacturaItem`, `Cliente`, `Configuracion` |
| [`sqlserver.py`](sqlserver.py) | snake_case | `Comprobantes`, `Items`, `Receptores`, `Emisores` |

Para un cliente nuevo se copia el más parecido y se le cambian las consultas. **No
hay un mapeador genérico configurable, y es deliberado**: un archivo de sesenta
líneas con su SQL a la vista se depura leyéndolo; un mapeo indirecto hay que
descifrarlo justo cuando algo está fallando.

---

## Las diez operaciones

```python
conectar(url, timeout)                          -> conexión
reloj(conn)                                     -> [{"utc":…, "con_zona":…}]
emisor(conn)                                    -> str   (razón social)
receptor(conn, comprobante_id)                  -> dict
items(conn, comprobante_id)                     -> [dict]
pendientes(conn)                                -> [dict]
marcar_enviados(conn, numeraciones, limpiar_error=True)   -> filas afectadas
marcar_enviado(conn, numeracion, enviado=True, limpiar_error=True)
guardar_error(conn, numeracion, detalle)        -> filas afectadas
guardar_error_varios(conn, numeraciones, detalle)
```

## `pendientes(conn)` — el corazón del contrato

Todo lo que todavía no se envió, **sin distinguir tipo**: el daemon separa después
los que van de a uno de las boletas que van en el resumen diario.

Cada fila es un diccionario con estas catorce claves. Las que faltan tienen que venir
en `None`, no ausentes: si falta la clave, el daemon revienta con `KeyError` recién
en producción.

| Clave | Qué es | ¿Obligatoria? |
|---|---|---|
| `id` | Identificador de la fila. Se usa para pedir sus ítems y su receptor | **sí** |
| `numeracion_comprobante` | Serie y número: `F001-000123` | **sí** |
| `fecha_emision` | Fecha, o fecha y hora | **sí** |
| `total` | Importe total con IGV | **sí** |
| `tipo_comprobante` | Código de SUNAT: `01` factura, `03` boleta, `07` NC, `08` ND | sí\* |
| `tipo_enum` | Alternativa al anterior: el texto `FACTURA`, `BOLETA`… | sí\* |
| `gravadas` | Base imponible. Si viene `None` se calcula desde el total | no |
| `igv` | IGV. Si viene `None` se calcula desde el total | no |
| `tipo_moneda` | `PEN` por defecto | no |
| `monto_letras` | El total en palabras. Si falta, se genera | no |
| `tipo_nota` | Motivo de la nota (catálogo 09 para NC, 10 para ND) | solo notas |
| `tipo_documento_afectado` | Tipo del comprobante que corrige la nota | solo notas |
| `numeracion_documento_afectado` | Numeración de ese comprobante | solo notas |
| `motivo_documento_afectado` | Descripción del motivo | no |

\* Hace falta uno de los dos. El daemon usa `tipo_comprobante` y, si viene vacío,
traduce `tipo_enum`.

**Las filas incompletas no se filtran en el SQL.** Una venta cobrada a la que nunca
se le asignó número igual no se puede emitir, pero descartarla en la consulta la
hacía desaparecer sin dejar una línea en el log. Que lleguen: el daemon las reporta
identificándolas por su `id`.

**Sin enviar** significa `enviado = false`. En SQL Server no hay booleano —`enviado`
es `BIT`— así que es `(enviado = 0 OR enviado IS NULL)`.

## `items(conn, comprobante_id)`

Una fila por línea del comprobante.

| Clave | Qué es |
|---|---|
| `descripcion` | Lo que se factura |
| `cantidad` / `dec_cantidad` | La cantidad. Se usa `dec_cantidad` si existe |
| `precio_unit` / `precio` | Precio unitario **con IGV** |
| `total` | Total de la línea |
| `codigo_producto` | Código interno. Puede ser `None` |
| `valor` | Valor unitario sin IGV. Si viene `None`, se calcula |
| `igv_venta` | IGV de la línea. Si viene `None`, se calcula |
| `medida` | Unidad del catálogo 03 de SUNAT. Si falta, se asume `ZZ` (servicio) |

El desglose de IGV se calcula cuando no viene: la mayoría de las aplicaciones guardan
solo el precio final. Si tu base ya lo tiene calculado, se respeta el tuyo.

## `receptor(conn, comprobante_id)`

Tiene que llegar **ya normalizado**, con estas tres claves exactas:

```python
{"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "…"}
```

`tipo_documento` es el catálogo 06 de SUNAT: `6` para RUC, `1` para DNI. Si el
comprobante no tiene receptor —una boleta de mostrador— devolvé `{}`: el daemon lo
emite como consumidor final.

La traducción vive en el adaptador porque cada esquema lo guarda distinto: el de
PostgreSQL deduce el tipo mirando si hay RUC o DNI, y el de SQL Server los tiene en
columnas separadas.

## `emisor(conn)` y `reloj(conn)`

`emisor` devuelve solo la **razón social**; el RUC sale de `EMISOR_RUC` en el `.env`.

`reloj` devuelve una lista con un diccionario que tenga `con_zona`: la hora "de
pared" del servidor de base de datos. El daemon la compara con la hora local para
medir el desfase, porque SUNAT espera la fecha de emisión en hora local del emisor y
muchos servidores corren en UTC. Si tu base ya está en hora local, devolvés la misma
y el desfase da cero.

## Las cuatro escrituras

Marcan el comprobante como enviado o le guardan el motivo de un rechazo. Todas
devuelven **cuántas filas afectaron** — el daemon lo usa para saber si el comprobante
existía.

`marcar_enviados` y `guardar_error_varios` reciben una **lista**: cierran de una vez
todas las boletas de un resumen diario. En PostgreSQL eso es `= ANY(%s)`; en SQL
Server hay que armar un `IN (?,?,…)` porque no existe `ANY(lista)`.

`limpiar_error=True` borra el motivo de rechazo anterior al aceptar el comprobante:
si venía rechazado y ahora SUNAT lo aceptó, ese motivo ya no aplica. Al devolverlo a
la cola para reintentar se usa `False`, para que el motivo siga a la vista.

---

## Cuatro diferencias entre motores

Son las que hay que mirar al adaptar. Están todas resueltas en `sqlserver.py`, que
sirve de referencia:

| | PostgreSQL | SQL Server |
|---|---|---|
| Marcador de parámetro | `%s` | `?` |
| Booleano | `enviado IS NOT TRUE` | `BIT`: `= 0 OR IS NULL` |
| Lista en un `WHERE` | `= ANY(%s)` | `IN (?,?,…)`, armado a mano |
| Nulos al final | `NULLS LAST` | un `CASE WHEN … IS NULL` |

Y una que no es de dialecto sino de driver: **ODBC admite un solo statement activo
por conexión** salvo que se habilite `MARS_Connection=yes`. El daemon recorre los
pendientes y adentro del bucle pide el receptor y los ítems de cada uno, así que sin
MARS falla en el primer ciclo con comprobantes.

## Cómo se elige el adaptador

Por el esquema de `DATABASE_URL`, sin una segunda variable que pueda quedar en
desacuerdo:

```
postgresql://usuario:clave@host:5432/base?schema=public
sqlserver://usuario:clave@host:1433/base
sqlserver://host:1433/base?trusted=yes          autenticación de Windows
sqlserver://host/base?instancia=SQLEXPRESS      instancia con nombre, sin puerto
```

## Cómo probar un adaptador nuevo

`test_sqlserver.py` corre contra una base real, no contra dobles, y verifica lo que
más se rompe al adaptar: que `pendientes()` traiga las catorce claves, que el filtro
de "sin enviar" cuente lo mismo que un conteo directo, y que el cierre masivo afecte
exactamente las filas que debía.

Vale la pena copiarlo: probar contra una base real ya encontró un bug que ninguna
simulación habría mostrado —el de MARS—, y lo encontró antes de llegar a producción.
