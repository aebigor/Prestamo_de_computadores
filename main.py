"""
Sistema de Préstamo de Equipos v2.0 - Backend Python (FastAPI)
==============================================================
Instalación:
    pip install fastapi uvicorn python-multipart sqlalchemy pillow fpdf2 python-barcode qrcode[pil]

Correr:
    uvicorn main:app --reload --port 8000

Documentación automática:
    http://localhost:8000/docs
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import (
    create_engine, Column, Integer, String, Text,
    DateTime, Boolean, ForeignKey, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from pathlib import Path
import shutil, uuid, os, io

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
UPLOADS  = BASE_DIR / "uploads"
STATIC   = BASE_DIR / "static"

UPLOADS.mkdir(exist_ok=True)
STATIC.mkdir(exist_ok=True)

DATABASE_URL = os.environ["DATABASE_URL"]

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )
print("Conectado a:", DATABASE_URL)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base         = declarative_base()

# ─────────────────────────────────────────────
# MODELOS DE BASE DE DATOS
# ─────────────────────────────────────────────

# ── Docentes ──────────────────────────────────
class Docente(Base):
    __tablename__ = "docentes"
    id         = Column(Integer, primary_key=True, index=True)
    cedula     = Column(String(30), unique=True, index=True, nullable=False)
    nombre     = Column(String(150), nullable=False)
    asignatura = Column(String(100))
    telefono   = Column(String(30))
    email      = Column(String(120))
    created_at = Column(DateTime, default=datetime.utcnow)
    prestamos  = relationship("Prestamo", back_populates="docente")

# ── Inventario (tipo de ítem) ──────────────────
class Inventario(Base):
    """
    Representa un tipo de ítem (ej: "Portátil HP", "Cable HDMI").
    requiere_serial=True  → activos únicos individuales (portátiles, monitores…)
    requiere_serial=False → stock genérico (cables, mouse, RAM…)
    """
    __tablename__ = "inventario"
    id             = Column(Integer, primary_key=True, index=True)
    nombre         = Column(String(150), nullable=False)
    categoria      = Column(String(80),  nullable=False)
    requiere_serial= Column(Boolean, default=False, nullable=False)
    stock          = Column(Integer, default=0)   # solo para no-serial
    marca          = Column(String(80))
    modelo         = Column(String(120))
    descripcion    = Column(Text)
    icono          = Column(String(10), default='📦')
    created_at     = Column(DateTime, default=datetime.utcnow)
    activos        = relationship("Activo",    back_populates="inventario", cascade="all, delete-orphan")
    movimientos    = relationship("Movimiento",back_populates="inventario", cascade="all, delete-orphan")

# ── Activo (unidad individual con serial) ──────
class Activo(Base):
    """Cada unidad física con serial propio."""
    __tablename__ = "activos"
    id            = Column(Integer, primary_key=True, index=True)
    inventario_id = Column(Integer, ForeignKey("inventario.id"), nullable=False)
    serial        = Column(String(80), unique=True, index=True, nullable=False)
    codigo_barras = Column(String(120), unique=True, index=True)
    estado        = Column(String(30), default="Disponible")
    # estados: Disponible | Prestado | Mantenimiento | Dañado | Baja
    marca         = Column(String(80))
    modelo        = Column(String(120))
    observaciones = Column(Text)
    responsable_actual = Column(String(150))
    fecha_creacion= Column(DateTime, default=datetime.utcnow)
    inventario    = relationship("Inventario", back_populates="activos")
    prestamos     = relationship("Prestamo",   back_populates="activo", cascade="all, delete-orphan")
    historial     = relationship("Historial",  back_populates="activo", cascade="all, delete-orphan")

# ── Movimiento (entradas/salidas stock genérico) ─
class Movimiento(Base):
    __tablename__ = "movimientos"
    id            = Column(Integer, primary_key=True, index=True)
    inventario_id = Column(Integer, ForeignKey("inventario.id"), nullable=False)
    tipo          = Column(String(20), nullable=False)  # "entrada" | "salida"
    cantidad      = Column(Integer, nullable=False)
    responsable   = Column(String(150))
    referencia    = Column(String(150))
    observaciones = Column(Text)
    fecha         = Column(DateTime, default=datetime.utcnow)
    inventario    = relationship("Inventario", back_populates="movimientos")

# ── Préstamo ───────────────────────────────────
class Prestamo(Base):
    __tablename__ = "prestamos"
    id              = Column(Integer, primary_key=True, index=True)
    activo_id       = Column(Integer, ForeignKey("activos.id"), nullable=False)
    docente_id      = Column(Integer, ForeignKey("docentes.id"), nullable=False)
    fecha_entrega   = Column(DateTime, default=datetime.utcnow)
    estado_entrega  = Column(String(60), default="Bueno")
    obs_entrega     = Column(Text)
    firma_entrega   = Column(String(255))
    situacion       = Column(String(20), default="activo")  # activo | devuelto
    # Devolución
    fecha_devolucion  = Column(DateTime, nullable=True)
    estado_devolucion = Column(String(60), nullable=True)
    obs_devolucion    = Column(Text, nullable=True)
    firma_devolucion  = Column(String(255), nullable=True)
    activo  = relationship("Activo",  back_populates="prestamos")
    docente = relationship("Docente", back_populates="prestamos")
    fotos   = relationship("FotoPrestamo", back_populates="prestamo", cascade="all, delete-orphan")

# ── Fotos de préstamo ──────────────────────────
class FotoPrestamo(Base):
    __tablename__ = "fotos_prestamo"
    id         = Column(Integer, primary_key=True, index=True)
    prestamo_id= Column(Integer, ForeignKey("prestamos.id"), nullable=False)
    ruta       = Column(String(255), nullable=False)
    tipo       = Column(String(20), default="entrega")   # entrega | devolucion
    created_at = Column(DateTime, default=datetime.utcnow)
    prestamo   = relationship("Prestamo", back_populates="fotos")

# ── Historial de activo ────────────────────────
class Historial(Base):
    __tablename__ = "historial"
    id          = Column(Integer, primary_key=True, index=True)
    activo_id   = Column(Integer, ForeignKey("activos.id"), nullable=False)
    fecha       = Column(DateTime, default=datetime.utcnow)
    accion      = Column(String(60), nullable=False)  # Prestado|Devuelto|Mantenimiento|Creado|…
    responsable = Column(String(150))
    estado      = Column(String(30))
    observacion = Column(Text)
    activo      = relationship("Activo", back_populates="historial")

# ── Tablas legacy (compatibilidad v1) ──────────
class Equipo(Base):
    __tablename__ = "equipos"
    id         = Column(Integer, primary_key=True, index=True)
    serial     = Column(String(60), unique=True, index=True, nullable=False)
    modelo     = Column(String(120))
    marca      = Column(String(80))
    created_at = Column(DateTime, default=datetime.utcnow)
    entregas   = relationship("Entrega", back_populates="equipo", cascade="all, delete-orphan")

class Entrega(Base):
    __tablename__ = "entregas"
    id               = Column(Integer, primary_key=True, index=True)
    equipo_id        = Column(Integer, ForeignKey("equipos.id"), nullable=False)
    docente_id       = Column(Integer, ForeignKey("docentes.id"), nullable=False)
    fecha_entrega    = Column(DateTime, default=datetime.utcnow)
    estado_entrega   = Column(String(60), default="Bueno")
    obs_entrega      = Column(Text)
    firma_entrega    = Column(String(255))
    situacion        = Column(String(20), default="activo")
    fecha_devolucion  = Column(DateTime, nullable=True)
    estado_devolucion = Column(String(60), nullable=True)
    obs_devolucion    = Column(Text, nullable=True)
    firma_devolucion  = Column(String(255), nullable=True)
    equipo  = relationship("Equipo",  back_populates="entregas")
    docente = relationship("Docente")
    fotos   = relationship("FotoEntrega", back_populates="entrega", cascade="all, delete-orphan")

class FotoEntrega(Base):
    __tablename__ = "fotos_entrega"
    id         = Column(Integer, primary_key=True, index=True)
    entrega_id = Column(Integer, ForeignKey("entregas.id"), nullable=False)
    ruta       = Column(String(255), nullable=False)
    tipo       = Column(String(20), default="entrega")
    created_at = Column(DateTime, default=datetime.utcnow)
    entrega    = relationship("Entrega", back_populates="fotos")

Base.metadata.create_all(bind=engine)

# ─────────────────────────────────────────────
# APP + MIDDLEWARE
# ─────────────────────────────────────────────
app = FastAPI(
    title="Sistema de Préstamo de Equipos v2",
    description="Gestión de activos únicos (con serial) e inventario general, préstamos y devoluciones.",
    version="2.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static",  StaticFiles(directory=STATIC),  name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def guardar_archivo(upload: UploadFile, carpeta: str = "") -> str:
    dest = UPLOADS / carpeta
    dest.mkdir(parents=True, exist_ok=True)
    ext    = Path(upload.filename).suffix or ".jpg"
    nombre = f"{uuid.uuid4().hex}{ext}"
    ruta   = dest / nombre
    with open(ruta, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return str(Path(carpeta) / nombre)

def gen_codigo_barras(prefijo: str = "EQ") -> str:
    return f"{prefijo}-{uuid.uuid4().hex[:10].upper()}"

def registrar_historial(db: Session, activo_id: int, accion: str,
                        responsable: str = None, estado: str = None, obs: str = None):
    db.add(Historial(activo_id=activo_id, accion=accion,
                     responsable=responsable, estado=estado, observacion=obs))

def serializar_activo(a: Activo) -> dict:
    prestamo_activo = next((p for p in a.prestamos if p.situacion == "activo"), None)
    return {
        "id":            a.id,
        "inventario_id": a.inventario_id,
        "nombre":        a.inventario.nombre,
        "categoria":     a.inventario.categoria,
        "serial":        a.serial,
        "codigo_barras": a.codigo_barras,
        "estado":        a.estado,
        "marca":         a.marca or a.inventario.marca,
        "modelo":        a.modelo or a.inventario.modelo,
        "observaciones": a.observaciones,
        "responsable_actual": a.responsable_actual,
        "fecha_creacion": a.fecha_creacion.isoformat(),
        "prestamo_activo": {
            "id":           prestamo_activo.id,
            "docente":      prestamo_activo.docente.nombre,
            "cedula":       prestamo_activo.docente.cedula,
            "fecha":        prestamo_activo.fecha_entrega.isoformat(),
            "estado":       prestamo_activo.estado_entrega,
        } if prestamo_activo else None,
    }

def serializar_prestamo(p: Prestamo) -> dict:
    fotos_e = [f"/uploads/{f.ruta}" for f in p.fotos if f.tipo == "entrega"]
    fotos_d = [f"/uploads/{f.ruta}" for f in p.fotos if f.tipo == "devolucion"]
    return {
        "id":                p.id,
        "activo_id":         p.activo_id,
        "serial":            p.activo.serial,
        "nombre_equipo":     p.activo.inventario.nombre,
        "categoria":         p.activo.inventario.categoria,
        "marca":             p.activo.marca or p.activo.inventario.marca,
        "modelo":            p.activo.modelo or p.activo.inventario.modelo,
        "nombre_docente":    p.docente.nombre,
        "cedula_docente":    p.docente.cedula,
        "asignatura":        p.docente.asignatura,
        "telefono":          p.docente.telefono,
        "fecha_entrega":     p.fecha_entrega.isoformat(),
        "estado_entrega":    p.estado_entrega,
        "obs_entrega":       p.obs_entrega,
        "firma_entrega":     f"/uploads/{p.firma_entrega}" if p.firma_entrega else None,
        "fotos_entrega":     fotos_e,
        "situacion":         p.situacion,
        "fecha_devolucion":  p.fecha_devolucion.isoformat() if p.fecha_devolucion else None,
        "estado_devolucion": p.estado_devolucion,
        "obs_devolucion":    p.obs_devolucion,
        "firma_devolucion":  f"/uploads/{p.firma_devolucion}" if p.firma_devolucion else None,
        "fotos_devolucion":  fotos_d,
    }

def serializar_entrega_legacy(e: Entrega) -> dict:
    fotos_e = [f"/uploads/{f.ruta}" for f in e.fotos if f.tipo == "entrega"]
    fotos_d = [f"/uploads/{f.ruta}" for f in e.fotos if f.tipo == "devolucion"]
    return {
        "id": e.id, "serial": e.equipo.serial, "modelo": e.equipo.modelo,
        "marca": e.equipo.marca, "nombre_docente": e.docente.nombre,
        "cedula_docente": e.docente.cedula, "asignatura": e.docente.asignatura,
        "telefono": e.docente.telefono,
        "fecha_entrega": e.fecha_entrega.isoformat(),
        "estado_entrega": e.estado_entrega, "obs_entrega": e.obs_entrega,
        "firma_entrega": f"/uploads/{e.firma_entrega}" if e.firma_entrega else None,
        "fotos_entrega": fotos_e, "situacion": e.situacion,
        "fecha_devolucion": e.fecha_devolucion.isoformat() if e.fecha_devolucion else None,
        "estado_devolucion": e.estado_devolucion, "obs_devolucion": e.obs_devolucion,
        "firma_devolucion": f"/uploads/{e.firma_devolucion}" if e.firma_devolucion else None,
        "fotos_devolucion": fotos_d,
    }
@app.get("/debug-db")
def debug_db(db: Session = Depends(get_db)):
    return {
        "inventario": db.query(Inventario).count(),
        "docentes": db.query(Docente).count(),
        "activos": db.query(Activo).count()
    }
# ═══════════════════════════════════════════════
# DOCENTES
# ═══════════════════════════════════════════════
@app.get("/docentes", tags=["Docentes"])
def listar_docentes(db: Session = Depends(get_db)):
    return db.query(Docente).order_by(Docente.nombre).all()

@app.post("/docentes", tags=["Docentes"], status_code=201)
def crear_o_actualizar_docente(
    cedula: str = Form(...), nombre: str = Form(...),
    asignatura: Optional[str] = Form(None), telefono: Optional[str] = Form(None),
    email: Optional[str] = Form(None), db: Session = Depends(get_db)
):
    doc = db.query(Docente).filter_by(cedula=cedula).first()
    if doc:
        doc.nombre = nombre; doc.asignatura = asignatura
        doc.telefono = telefono; doc.email = email
    else:
        doc = Docente(cedula=cedula, nombre=nombre, asignatura=asignatura,
                      telefono=telefono, email=email)
        db.add(doc)
    db.commit(); db.refresh(doc)
    return doc

# ═══════════════════════════════════════════════
# INVENTARIO
# ═══════════════════════════════════════════════
@app.get("/inventario", tags=["Inventario"])
def listar_inventario(
    categoria: Optional[str] = None,
    requiere_serial: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Inventario)
    if categoria:        q = q.filter(Inventario.categoria == categoria)
    if requiere_serial is not None: q = q.filter(Inventario.requiere_serial == requiere_serial)
    items = q.all()
    resultado = []
    for inv in items:
        d = {
            "id": inv.id, "nombre": inv.nombre, "categoria": inv.categoria,
            "requiere_serial": inv.requiere_serial, "marca": inv.marca,
            "modelo": inv.modelo, "descripcion": inv.descripcion,
            "icono": inv.icono or '📦',
            "created_at": inv.created_at.isoformat(),
        }
        if inv.requiere_serial:
            d["total_activos"]       = len(inv.activos)
            d["disponibles"]         = sum(1 for a in inv.activos if a.estado == "Disponible")
            d["prestados"]           = sum(1 for a in inv.activos if a.estado == "Prestado")
            d["en_mantenimiento"]    = sum(1 for a in inv.activos if a.estado == "Mantenimiento")
        else:
            d["stock"] = inv.stock
        resultado.append(d)
    return resultado

@app.post("/inventario", tags=["Inventario"], status_code=201)
def crear_inventario(
    nombre:          str           = Form(...),
    categoria:       str           = Form(...),
    requiere_serial: bool          = Form(False),
    stock:           int           = Form(0),
    marca:           Optional[str] = Form(None),
    modelo:          Optional[str] = Form(None),
    descripcion:     Optional[str] = Form(None),
    icono:           Optional[str] = Form('📦'),
    db: Session = Depends(get_db)
):
    inv = Inventario(
        nombre=nombre, categoria=categoria, requiere_serial=requiere_serial,
        stock=stock if not requiere_serial else 0,
        marca=marca, modelo=modelo, descripcion=descripcion, icono=icono
    )
    db.add(inv); db.commit(); db.refresh(inv)
    return inv

@app.get("/inventario/{id}", tags=["Inventario"])
def obtener_inventario(id: int, db: Session = Depends(get_db)):
    inv = db.query(Inventario).filter_by(id=id).first()
    if not inv: raise HTTPException(404, "Ítem no encontrado")
    return inv

@app.patch("/inventario/{id}", tags=["Inventario"])
def actualizar_inventario(
    id: int,
    nombre:      Optional[str] = Form(None),
    categoria:   Optional[str] = Form(None),
    marca:       Optional[str] = Form(None),
    modelo:      Optional[str] = Form(None),
    descripcion: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    inv = db.query(Inventario).filter_by(id=id).first()
    if not inv: raise HTTPException(404, "Ítem no encontrado")
    if nombre:      inv.nombre      = nombre
    if categoria:   inv.categoria   = categoria
    if marca:       inv.marca       = marca
    if modelo:      inv.modelo      = modelo
    if descripcion: inv.descripcion = descripcion
    db.commit(); db.refresh(inv)
    return inv

@app.delete("/inventario/{id}", tags=["Inventario"])
def eliminar_inventario(id: int, db: Session = Depends(get_db)):
    inv = db.query(Inventario).filter_by(id=id).first()
    if not inv: raise HTTPException(404, "Ítem no encontrado")
    db.delete(inv); db.commit()
    return {"ok": True}

# ═══════════════════════════════════════════════
# ACTIVOS ÚNICOS
# ═══════════════════════════════════════════════
@app.get("/activos", tags=["Activos"])
def listar_activos(
    inventario_id: Optional[int] = None,
    estado: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Activo)
    if inventario_id: q = q.filter(Activo.inventario_id == inventario_id)
    if estado:        q = q.filter(Activo.estado == estado)
    return [serializar_activo(a) for a in q.all()]

@app.get("/activos/buscar", tags=["Activos"])
def buscar_activo(
    codigo: str = Query(..., description="Serial o código de barras"),
    db: Session = Depends(get_db)
):
    """Buscar activo por serial o código de barras (usado por el escáner)."""
    a = db.query(Activo).filter(
        (Activo.serial == codigo) | (Activo.codigo_barras == codigo)
    ).first()
    if not a: raise HTTPException(404, f"No se encontró activo con código: {codigo}")
    return serializar_activo(a)

@app.post("/activos", tags=["Activos"], status_code=201)
def crear_activos(
    inventario_id:  int            = Form(...),
    seriales:       str            = Form(..., description="Seriales separados por coma o salto de línea"),
    marca:          Optional[str]  = Form(None),
    modelo:         Optional[str]  = Form(None),
    observaciones:  Optional[str]  = Form(None),
    db: Session = Depends(get_db)
):
    """Crear uno o varios activos para un ítem de inventario con serial."""
    inv = db.query(Inventario).filter_by(id=inventario_id).first()
    if not inv:            raise HTTPException(404, "Ítem de inventario no encontrado")
    if not inv.requiere_serial: raise HTTPException(400, "Este ítem no requiere serial individual")

    lista = [s.strip() for s in seriales.replace("\n", ",").split(",") if s.strip()]
    if not lista: raise HTTPException(400, "Debes ingresar al menos un serial")

    creados = []
    for serial in lista:
        if db.query(Activo).filter_by(serial=serial).first():
            raise HTTPException(409, f"El serial '{serial}' ya existe")
        cod  = gen_codigo_barras(inv.categoria[:3].upper())
        activo = Activo(
            inventario_id=inventario_id, serial=serial,
            codigo_barras=cod, estado="Disponible",
            marca=marca, modelo=modelo, observaciones=observaciones
        )
        db.add(activo); db.flush()
        registrar_historial(db, activo.id, "Creado", estado="Disponible",
                            obs=f"Registro inicial. Serial: {serial}")
        creados.append(activo)

    db.commit()
    return [serializar_activo(a) for a in creados]

@app.patch("/activos/{id}/estado", tags=["Activos"])
def cambiar_estado_activo(
    id:     int,
    estado: str           = Form(...),
    obs:    Optional[str] = Form(None),
    responsable: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Cambiar estado manualmente: Disponible|Mantenimiento|Dañado|Baja."""
    estados_validos = {"Disponible", "Mantenimiento", "Dañado", "Baja"}
    if estado not in estados_validos:
        raise HTTPException(400, f"Estado inválido. Opciones: {estados_validos}")
    a = db.query(Activo).filter_by(id=id).first()
    if not a: raise HTTPException(404, "Activo no encontrado")
    a.estado = estado
    registrar_historial(db, id, estado, responsable=responsable, estado=estado, obs=obs)
    db.commit()
    return serializar_activo(a)

@app.delete("/activos/{id}", tags=["Activos"])
def eliminar_activo(id: int, db: Session = Depends(get_db)):
    a = db.query(Activo).filter_by(id=id).first()
    if not a: raise HTTPException(404, "Activo no encontrado")
    db.delete(a); db.commit()
    return {"ok": True}

@app.get("/activos/{id}/historial", tags=["Activos"])
def historial_activo(id: int, db: Session = Depends(get_db)):
    a = db.query(Activo).filter_by(id=id).first()
    if not a: raise HTTPException(404, "Activo no encontrado")
    return sorted([{
        "id":          h.id,
        "fecha":       h.fecha.isoformat(),
        "accion":      h.accion,
        "responsable": h.responsable,
        "estado":      h.estado,
        "observacion": h.observacion,
    } for h in a.historial], key=lambda x: x["fecha"], reverse=True)

# ═══════════════════════════════════════════════
# MOVIMIENTOS DE STOCK GENÉRICO
# ═══════════════════════════════════════════════
@app.post("/inventario/{id}/movimiento", tags=["Inventario"])
def registrar_movimiento(
    id:           int,
    tipo:         str           = Form(...),   # entrada | salida
    cantidad:     int           = Form(...),
    responsable:  Optional[str] = Form(None),
    referencia:   Optional[str] = Form(None),
    observaciones:Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    inv = db.query(Inventario).filter_by(id=id).first()
    if not inv: raise HTTPException(404, "Ítem no encontrado")
    if inv.requiere_serial: raise HTTPException(400, "Este ítem usa activos individuales, no stock genérico")
    if tipo not in ("entrada", "salida"): raise HTTPException(400, "Tipo debe ser 'entrada' o 'salida'")
    if tipo == "salida" and inv.stock < cantidad:
        raise HTTPException(409, f"Stock insuficiente ({inv.stock} disponibles)")
    inv.stock += cantidad if tipo == "entrada" else -cantidad
    db.add(Movimiento(inventario_id=id, tipo=tipo, cantidad=cantidad,
                      responsable=responsable, referencia=referencia,
                      observaciones=observaciones))
    db.commit()
    return {"id": inv.id, "nombre": inv.nombre, "stock": inv.stock}

@app.get("/inventario/{id}/movimientos", tags=["Inventario"])
def listar_movimientos(id: int, db: Session = Depends(get_db)):
    movs = db.query(Movimiento).filter_by(inventario_id=id).order_by(Movimiento.fecha.desc()).all()
    return [{
        "id": m.id, "tipo": m.tipo, "cantidad": m.cantidad,
        "responsable": m.responsable, "referencia": m.referencia,
        "observaciones": m.observaciones, "fecha": m.fecha.isoformat()
    } for m in movs]

# ═══════════════════════════════════════════════
# PRÉSTAMOS (activos con serial)
# ═══════════════════════════════════════════════
@app.get("/prestamos", tags=["Préstamos"])
def listar_prestamos(situacion: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Prestamo)
    if situacion: q = q.filter(Prestamo.situacion == situacion)
    return [serializar_prestamo(p) for p in q.all()]

@app.get("/prestamos/{id}", tags=["Préstamos"])
def obtener_prestamo(id: int, db: Session = Depends(get_db)):
    p = db.query(Prestamo).filter_by(id=id).first()
    if not p: raise HTTPException(404, "Préstamo no encontrado")
    return serializar_prestamo(p)

@app.post("/prestamos", tags=["Préstamos"], status_code=201)
async def crear_prestamo(
    serial_o_barras: str           = Form(..., description="Serial o código de barras del activo"),
    cedula_docente:  str           = Form(...),
    nombre_docente:  str           = Form(...),
    asignatura:      Optional[str] = Form(None),
    telefono:        Optional[str] = Form(None),
    estado_entrega:  Optional[str] = Form("Bueno"),
    obs_entrega:     Optional[str] = Form(None),
    fecha_entrega:   Optional[str] = Form(None),
    firma:           Optional[UploadFile] = File(None),
    fotos:           List[UploadFile]     = File(default=[]),
    db: Session = Depends(get_db)
):
    # Buscar activo por serial o código de barras
    activo = db.query(Activo).filter(
        (Activo.serial == serial_o_barras) | (Activo.codigo_barras == serial_o_barras)
    ).first()
    if not activo: raise HTTPException(404, f"No se encontró el activo: {serial_o_barras}")
    if activo.estado != "Disponible":
        raise HTTPException(409, f"El activo no está disponible. Estado actual: {activo.estado}")

    # Docente (upsert)
    docente = db.query(Docente).filter_by(cedula=cedula_docente).first()
    if not docente:
        docente = Docente(cedula=cedula_docente, nombre=nombre_docente,
                          asignatura=asignatura, telefono=telefono)
        db.add(docente); db.flush()

    ruta_firma = None
    if firma and firma.filename:
        ruta_firma = guardar_archivo(firma, "firmas")

    fecha = datetime.fromisoformat(fecha_entrega) if fecha_entrega else datetime.utcnow()
    prestamo = Prestamo(
        activo_id=activo.id, docente_id=docente.id,
        estado_entrega=estado_entrega, obs_entrega=obs_entrega,
        fecha_entrega=fecha, firma_entrega=ruta_firma, situacion="activo"
    )
    db.add(prestamo); db.flush()

    for foto in fotos:
        if foto.filename:
            ruta = guardar_archivo(foto, f"prestamos/{prestamo.id}")
            db.add(FotoPrestamo(prestamo_id=prestamo.id, ruta=ruta, tipo="entrega"))

    # Actualizar estado del activo
    activo.estado = "Prestado"
    activo.responsable_actual = nombre_docente
    registrar_historial(db, activo.id, "Prestado",
                        responsable=nombre_docente, estado="Prestado",
                        obs=f"Entregado a {nombre_docente} (CC {cedula_docente}). Estado: {estado_entrega}")

    db.commit(); db.refresh(prestamo)
    return serializar_prestamo(prestamo)

@app.patch("/prestamos/{id}/devolucion", tags=["Préstamos"])
async def registrar_devolucion(
    id: int,
    estado_devolucion: Optional[str] = Form("Disponible"),
    obs_devolucion:    Optional[str] = Form(None),
    fecha_devolucion:  Optional[str] = Form(None),
    firma:             Optional[UploadFile] = File(None),
    fotos:             List[UploadFile]     = File(default=[]),
    db: Session = Depends(get_db)
):
    """
    Registra devolución. estado_devolucion puede ser:
    Disponible | Mantenimiento | Dañado
    """
    p = db.query(Prestamo).filter_by(id=id).first()
    if not p: raise HTTPException(404, "Préstamo no encontrado")
    if p.situacion == "devuelto": raise HTTPException(409, "Este préstamo ya fue cerrado")

    if firma and firma.filename:
        p.firma_devolucion = guardar_archivo(firma, "firmas")

    for foto in fotos:
        if foto.filename:
            ruta = guardar_archivo(foto, f"devoluciones/{id}")
            db.add(FotoPrestamo(prestamo_id=id, ruta=ruta, tipo="devolucion"))

    fecha = datetime.fromisoformat(fecha_devolucion) if fecha_devolucion else datetime.utcnow()
    p.situacion = "devuelto"
    p.fecha_devolucion  = fecha
    p.estado_devolucion = estado_devolucion
    p.obs_devolucion    = obs_devolucion

    # Actualizar estado del activo
    nuevo_estado = estado_devolucion if estado_devolucion in ("Mantenimiento", "Dañado") else "Disponible"
    p.activo.estado = nuevo_estado
    p.activo.responsable_actual = None
    registrar_historial(db, p.activo_id, "Devuelto",
                        responsable=p.docente.nombre, estado=nuevo_estado,
                        obs=obs_devolucion)

    db.commit(); db.refresh(p)
    return serializar_prestamo(p)

@app.get("/prestamos/por-serial/{serial}", tags=["Préstamos"])
def prestamo_activo_por_serial(serial: str, db: Session = Depends(get_db)):
    """Obtiene el préstamo activo de un equipo por serial o código de barras."""
    activo = db.query(Activo).filter(
        (Activo.serial == serial) | (Activo.codigo_barras == serial)
    ).first()
    if not activo: raise HTTPException(404, "Activo no encontrado")
    prestamo = db.query(Prestamo).filter_by(activo_id=activo.id, situacion="activo").first()
    if not prestamo: raise HTTPException(404, "No hay préstamo activo para este equipo")
    return serializar_prestamo(prestamo)

# ═══════════════════════════════════════════════
# ENDPOINTS LEGACY (compatibilidad v1)
# ═══════════════════════════════════════════════
@app.get("/equipos", tags=["Legacy v1"])
def listar_equipos(db: Session = Depends(get_db)):
    return db.query(Equipo).all()

@app.post("/equipos", tags=["Legacy v1"], status_code=201)
def crear_equipo_legacy(
    serial: str = Form(...), modelo: Optional[str] = Form(None),
    marca: Optional[str] = Form(None), db: Session = Depends(get_db)
):
    if db.query(Equipo).filter_by(serial=serial).first():
        raise HTTPException(409, "Ya existe un equipo con ese serial")
    eq = Equipo(serial=serial, modelo=modelo, marca=marca)
    db.add(eq); db.commit(); db.refresh(eq)
    return eq

@app.get("/entregas", tags=["Legacy v1"])
def listar_entregas(situacion: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Entrega)
    if situacion: q = q.filter(Entrega.situacion == situacion)
    return [serializar_entrega_legacy(e) for e in q.all()]

@app.post("/entregas", tags=["Legacy v1"], status_code=201)
async def crear_entrega_legacy(
    serial_equipo: str = Form(...), cedula_docente: str = Form(...),
    nombre_docente: str = Form(...), asignatura: Optional[str] = Form(None),
    telefono: Optional[str] = Form(None), modelo: Optional[str] = Form(None),
    estado_entrega: Optional[str] = Form("Bueno"), obs_entrega: Optional[str] = Form(None),
    fecha_entrega: Optional[str] = Form(None),
    firma: Optional[UploadFile] = File(None), fotos: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db)
):
    equipo = db.query(Equipo).filter_by(serial=serial_equipo).first()
    if not equipo:
        equipo = Equipo(serial=serial_equipo, modelo=modelo)
        db.add(equipo); db.flush()
    if db.query(Entrega).filter_by(equipo_id=equipo.id, situacion="activo").first():
        raise HTTPException(409, f"El equipo {serial_equipo} ya tiene una entrega activa")
    docente = db.query(Docente).filter_by(cedula=cedula_docente).first()
    if not docente:
        docente = Docente(cedula=cedula_docente, nombre=nombre_docente,
                          asignatura=asignatura, telefono=telefono)
        db.add(docente); db.flush()
    ruta_firma = None
    if firma and firma.filename: ruta_firma = guardar_archivo(firma, "firmas")
    fecha = datetime.fromisoformat(fecha_entrega) if fecha_entrega else datetime.utcnow()
    entrega = Entrega(equipo_id=equipo.id, docente_id=docente.id,
                      estado_entrega=estado_entrega, obs_entrega=obs_entrega,
                      fecha_entrega=fecha, firma_entrega=ruta_firma, situacion="activo")
    db.add(entrega); db.flush()
    for foto in fotos:
        if foto.filename:
            ruta = guardar_archivo(foto, f"entregas/{entrega.id}")
            db.add(FotoEntrega(entrega_id=entrega.id, ruta=ruta, tipo="entrega"))
    db.commit(); db.refresh(entrega)
    return serializar_entrega_legacy(entrega)

@app.patch("/entregas/{id}/devolucion", tags=["Legacy v1"])
async def devolucion_legacy(
    id: int, estado_devolucion: Optional[str] = Form("Bueno"),
    obs_devolucion: Optional[str] = Form(None), fecha_devolucion: Optional[str] = Form(None),
    firma: Optional[UploadFile] = File(None), fotos: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db)
):
    e = db.query(Entrega).filter_by(id=id).first()
    if not e: raise HTTPException(404, "Entrega no encontrada")
    if e.situacion == "devuelto": raise HTTPException(409, "Ya devuelto")
    if firma and firma.filename: e.firma_devolucion = guardar_archivo(firma, "firmas")
    for foto in fotos:
        if foto.filename:
            ruta = guardar_archivo(foto, f"devoluciones/{id}")
            db.add(FotoEntrega(entrega_id=id, ruta=ruta, tipo="devolucion"))
    fecha = datetime.fromisoformat(fecha_devolucion) if fecha_devolucion else datetime.utcnow()
    e.situacion = "devuelto"; e.fecha_devolucion = fecha
    e.estado_devolucion = estado_devolucion; e.obs_devolucion = obs_devolucion
    db.commit(); db.refresh(e)
    return serializar_entrega_legacy(e)

# ═══════════════════════════════════════════════
# INFORME / DASHBOARD
# ═══════════════════════════════════════════════
@app.get("/informe", tags=["Informe"])
def obtener_informe(db: Session = Depends(get_db)):
    return {
        "prestamos_activos":    db.query(Prestamo).filter_by(situacion="activo").count(),
        "prestamos_devueltos":  db.query(Prestamo).filter_by(situacion="devuelto").count(),
        "total_activos":        db.query(Activo).count(),
        "activos_disponibles":  db.query(Activo).filter_by(estado="Disponible").count(),
        "activos_prestados":    db.query(Activo).filter_by(estado="Prestado").count(),
        "activos_mantenimiento":db.query(Activo).filter_by(estado="Mantenimiento").count(),
        "activos_daniados":     db.query(Activo).filter_by(estado="Dañado").count(),
        "total_inventario":     db.query(Inventario).count(),
        "total_docentes":       db.query(Docente).count(),
        # legacy
        "entregas_activas":     db.query(Entrega).filter_by(situacion="activo").count(),
        "entregas_devueltas":   db.query(Entrega).filter_by(situacion="devuelto").count(),
    }

# ═══════════════════════════════════════════════
# RAÍZ
# ═══════════════════════════════════════════════
@app.get("/", include_in_schema=False)
def root():
    index = STATIC / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"mensaje": "API v2 funcionando. Coloca el HTML en static/index.html"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)