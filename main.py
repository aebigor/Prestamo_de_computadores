
"""
Sistema de Préstamo de Equipos v2.0 — Backend FastAPI para Render
=================================================================
Variables de entorno requeridas en Render:
  DATABASE_URL  → URL de PostgreSQL de Render (se setea automáticamente si usas una DB de Render)
  SECRET_KEY    → Clave para la sesión (cualquier string largo aleatorio)

Dependencias (requirements.txt):
  fastapi
  uvicorn[standard]
  python-multipart
  sqlalchemy
  psycopg2-binary
  cloudinary          ← para almacenar fotos (archivos no persisten en Render free tier)
  python-dotenv

Correr localmente:
  uvicorn main:app --reload --port 8000
"""

import os, uuid, shutil, io
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from sqlalchemy import (
    create_engine, Column, Integer, String, Text,
    DateTime, Boolean, ForeignKey, event as sa_event
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

# ─────────────────────────────────────────────
# CONFIGURACIÓN DE BASE DE DATOS
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError(
        "❌ Falta la variable de entorno DATABASE_URL. "
        "Configúrala en Render → Environment → DATABASE_URL"
    )

# Render devuelve "postgres://..." pero SQLAlchemy necesita "postgresql://..."
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Para PostgreSQL en producción usamos pool robusto
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,       # verifica conexión antes de usarla
    pool_size=5,              # conexiones simultáneas máximas
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=300,         # recicla conexiones cada 5 min (evita timeouts)
    echo=False,               # pon True para ver SQL en consola (debug)
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# ─────────────────────────────────────────────
# ARCHIVOS ESTÁTICOS
# Render free tier: el sistema de archivos es efímero (se borra al redeploy).
# Las fotos y firmas se pierden. Solución: usa Cloudinary (ver helper abajo).
# Si prefieres guardar localmente en dev, deja las rutas como están.
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATIC   = BASE_DIR / "static"
UPLOADS  = BASE_DIR / "uploads"   # ⚠ efímero en Render Free — usa Cloudinary en prod
STATIC.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# CLOUDINARY (opcional — activa si quieres fotos persistentes)
# Configura en Render: CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name
# ─────────────────────────────────────────────
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "")
USE_CLOUDINARY = bool(CLOUDINARY_URL)

if USE_CLOUDINARY:
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(cloudinary_url=CLOUDINARY_URL)
        print("✅ Cloudinary configurado")
    except ImportError:
        USE_CLOUDINARY = False
        print("⚠ cloudinary no instalado — usando disco local (efímero)")

# ─────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────

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
    entregas   = relationship("Entrega",  back_populates="docente")


class Inventario(Base):
    """
    Tipo de ítem.
    requiere_serial=True  → activos únicos (portátiles, monitores…)
    requiere_serial=False → stock genérico (cables, mouse, RAM…)
    """
    __tablename__ = "inventario"
    id              = Column(Integer, primary_key=True, index=True)
    nombre          = Column(String(150), nullable=False)
    categoria       = Column(String(80),  nullable=False)
    requiere_serial = Column(Boolean, default=False, nullable=False)
    stock           = Column(Integer, default=0)   # solo para no-serial
    marca           = Column(String(80))
    modelo          = Column(String(120))
    descripcion     = Column(Text)
    created_at      = Column(DateTime, default=datetime.utcnow)
    activos     = relationship("Activo",     back_populates="inventario", cascade="all, delete-orphan")
    movimientos = relationship("Movimiento", back_populates="inventario", cascade="all, delete-orphan")


class Activo(Base):
    """Unidad física individual con serial propio."""
    __tablename__ = "activos"
    id                 = Column(Integer, primary_key=True, index=True)
    inventario_id      = Column(Integer, ForeignKey("inventario.id"), nullable=False)
    serial             = Column(String(80),  unique=True, index=True, nullable=False)
    codigo_barras      = Column(String(120), unique=True, index=True)
    estado             = Column(String(30),  default="Disponible")
    # Estados válidos: Disponible | Prestado | Mantenimiento | Dañado | Baja
    marca              = Column(String(80))
    modelo             = Column(String(120))
    observaciones      = Column(Text)
    responsable_actual = Column(String(150))
    fecha_creacion     = Column(DateTime, default=datetime.utcnow)
    inventario = relationship("Inventario", back_populates="activos")
    prestamos  = relationship("Prestamo",   back_populates="activo",    cascade="all, delete-orphan")
    historial  = relationship("Historial",  back_populates="activo",    cascade="all, delete-orphan")


class Movimiento(Base):
    """Entrada / salida de stock genérico (sin serial)."""
    __tablename__ = "movimientos"
    id             = Column(Integer, primary_key=True, index=True)
    inventario_id  = Column(Integer, ForeignKey("inventario.id"), nullable=False)
    tipo           = Column(String(20), nullable=False)   # "entrada" | "salida"
    cantidad       = Column(Integer, nullable=False)
    responsable    = Column(String(150))
    referencia     = Column(String(150))
    observaciones  = Column(Text)
    fecha          = Column(DateTime, default=datetime.utcnow)
    inventario = relationship("Inventario", back_populates="movimientos")


class Prestamo(Base):
    """Préstamo de un activo único (con serial) a un docente."""
    __tablename__ = "prestamos"
    id                = Column(Integer, primary_key=True, index=True)
    activo_id         = Column(Integer, ForeignKey("activos.id"),  nullable=False)
    docente_id        = Column(Integer, ForeignKey("docentes.id"), nullable=False)
    fecha_entrega     = Column(DateTime, default=datetime.utcnow)
    estado_entrega    = Column(String(60), default="Bueno")
    obs_entrega       = Column(Text)
    firma_entrega     = Column(String(512))   # URL de Cloudinary o ruta local
    situacion         = Column(String(20), default="activo")   # activo | devuelto
    # Devolución (null mientras esté activo)
    fecha_devolucion  = Column(DateTime,    nullable=True)
    estado_devolucion = Column(String(60),  nullable=True)
    obs_devolucion    = Column(Text,        nullable=True)
    firma_devolucion  = Column(String(512), nullable=True)
    activo  = relationship("Activo",       back_populates="prestamos")
    docente = relationship("Docente",      back_populates="prestamos")
    fotos   = relationship("FotoPrestamo", back_populates="prestamo", cascade="all, delete-orphan")


class FotoPrestamo(Base):
    __tablename__ = "fotos_prestamo"
    id          = Column(Integer, primary_key=True, index=True)
    prestamo_id = Column(Integer, ForeignKey("prestamos.id"), nullable=False)
    url         = Column(String(512), nullable=False)   # URL o ruta
    tipo        = Column(String(20), default="entrega") # entrega | devolucion
    created_at  = Column(DateTime, default=datetime.utcnow)
    prestamo    = relationship("Prestamo", back_populates="fotos")


class Historial(Base):
    """Log de cada acción sobre un activo único."""
    __tablename__ = "historial"
    id          = Column(Integer, primary_key=True, index=True)
    activo_id   = Column(Integer, ForeignKey("activos.id"), nullable=False)
    fecha       = Column(DateTime, default=datetime.utcnow)
    accion      = Column(String(60), nullable=False)  # Prestado|Devuelto|Mantenimiento|Creado|Dañado|Baja
    responsable = Column(String(150))
    estado      = Column(String(30))
    observacion = Column(Text)
    activo      = relationship("Activo", back_populates="historial")


# ── Tablas legacy (compatibilidad con el frontend de entregas directas) ───────
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
    id                = Column(Integer, primary_key=True, index=True)
    equipo_id         = Column(Integer, ForeignKey("equipos.id"),  nullable=False)
    docente_id        = Column(Integer, ForeignKey("docentes.id"), nullable=False)
    fecha_entrega     = Column(DateTime, default=datetime.utcnow)
    estado_entrega    = Column(String(60), default="Bueno")
    obs_entrega       = Column(Text)
    firma_entrega     = Column(String(512))
    situacion         = Column(String(20), default="activo")
    fecha_devolucion  = Column(DateTime,    nullable=True)
    estado_devolucion = Column(String(60),  nullable=True)
    obs_devolucion    = Column(Text,        nullable=True)
    firma_devolucion  = Column(String(512), nullable=True)
    equipo  = relationship("Equipo",      back_populates="entregas")
    docente = relationship("Docente",     back_populates="entregas")
    fotos   = relationship("FotoEntrega", back_populates="entrega", cascade="all, delete-orphan")


class FotoEntrega(Base):
    __tablename__ = "fotos_entrega"
    id         = Column(Integer, primary_key=True, index=True)
    entrega_id = Column(Integer, ForeignKey("entregas.id"), nullable=False)
    url        = Column(String(512), nullable=False)
    tipo       = Column(String(20), default="entrega")
    created_at = Column(DateTime, default=datetime.utcnow)
    entrega    = relationship("Entrega", back_populates="fotos")


# Crear todas las tablas
Base.metadata.create_all(bind=engine)

# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────
app = FastAPI(
    title="Sistema de Préstamo de Equipos",
    version="2.0.0",
    description="API para gestión de activos tecnológicos en Render + PostgreSQL",
)

# CORS — permite peticiones desde cualquier origen
# En producción, reemplaza "*" por la URL exacta de tu frontend si quieres más seguridad
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Archivos estáticos (el HTML va en static/)
app.mount("/static",  StaticFiles(directory=STATIC),  name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")


# ─────────────────────────────────────────────
# DEPENDENCIA DB
# ─────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def subir_archivo(upload: UploadFile, carpeta: str = "misc") -> str:
    """
    Guarda el archivo.
    - Si USE_CLOUDINARY=True → sube a Cloudinary y retorna la URL pública.
    - Si no → guarda en disco local y retorna la ruta relativa.
    """
    if USE_CLOUDINARY:
        result = cloudinary.uploader.upload(
            upload.file,
            folder=f"equipos/{carpeta}",
            resource_type="auto",
        )
        return result["secure_url"]
    else:
        dest = UPLOADS / carpeta
        dest.mkdir(parents=True, exist_ok=True)
        ext    = Path(upload.filename or "file").suffix or ".jpg"
        nombre = f"{uuid.uuid4().hex}{ext}"
        with open(dest / nombre, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        # Retorna URL local relativa al servidor
        return f"/uploads/{carpeta}/{nombre}"


def gen_codigo_barras(prefijo: str = "EQ") -> str:
    pre = (prefijo or "EQ")[:4].upper().replace(" ", "")
    return f"{pre}-{uuid.uuid4().hex[:10].upper()}"


def log_historial(db: Session, activo_id: int, accion: str,
                  responsable: str = None, estado: str = None, obs: str = None):
    db.add(Historial(
        activo_id=activo_id, accion=accion,
        responsable=responsable, estado=estado, observacion=obs,
    ))


def _serial_activo(a: Activo) -> dict:
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
            "id":     prestamo_activo.id,
            "docente":prestamo_activo.docente.nombre,
            "cedula": prestamo_activo.docente.cedula,
            "fecha":  prestamo_activo.fecha_entrega.isoformat(),
            "estado": prestamo_activo.estado_entrega,
        } if prestamo_activo else None,
    }


def _serial_prestamo(p: Prestamo) -> dict:
    fotos_e = [f.url for f in p.fotos if f.tipo == "entrega"]
    fotos_d = [f.url for f in p.fotos if f.tipo == "devolucion"]
    return {
        "id":                p.id,
        "activo_id":         p.activo_id,
        "serial":            p.activo.serial,
        "codigo_barras":     p.activo.codigo_barras,
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
        "firma_entrega":     p.firma_entrega,
        "fotos_entrega":     fotos_e,
        "situacion":         p.situacion,
        "fecha_devolucion":  p.fecha_devolucion.isoformat() if p.fecha_devolucion else None,
        "estado_devolucion": p.estado_devolucion,
        "obs_devolucion":    p.obs_devolucion,
        "firma_devolucion":  p.firma_devolucion,
        "fotos_devolucion":  fotos_d,
    }


def _serial_entrega(e: Entrega) -> dict:
    fotos_e = [f.url for f in e.fotos if f.tipo == "entrega"]
    fotos_d = [f.url for f in e.fotos if f.tipo == "devolucion"]
    return {
        "id":                e.id,
        "serial":            e.equipo.serial,
        "modelo":            e.equipo.modelo,
        "marca":             e.equipo.marca,
        "nombre_docente":    e.docente.nombre,
        "cedula_docente":    e.docente.cedula,
        "asignatura":        e.docente.asignatura,
        "telefono":          e.docente.telefono,
        "fecha_entrega":     e.fecha_entrega.isoformat(),
        "estado_entrega":    e.estado_entrega,
        "obs_entrega":       e.obs_entrega,
        "firma_entrega":     e.firma_entrega,
        "fotos_entrega":     fotos_e,
        "situacion":         e.situacion,
        "fecha_devolucion":  e.fecha_devolucion.isoformat() if e.fecha_devolucion else None,
        "estado_devolucion": e.estado_devolucion,
        "obs_devolucion":    e.obs_devolucion,
        "firma_devolucion":  e.firma_devolucion,
        "fotos_devolucion":  fotos_d,
    }


# ═══════════════════════════════════════════════
# SALUD DEL SERVICIO
# ═══════════════════════════════════════════════
@app.get("/health", tags=["Sistema"])
def health(db: Session = Depends(get_db)):
    """Verifica que la API y la base de datos respondan."""
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ok", "db": "conectada", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(500, f"Error de base de datos: {e}")


# ═══════════════════════════════════════════════
# DOCENTES
# ═══════════════════════════════════════════════
@app.get("/docentes", tags=["Docentes"])
def listar_docentes(db: Session = Depends(get_db)):
    return db.query(Docente).order_by(Docente.nombre).all()


@app.post("/docentes", tags=["Docentes"], status_code=201)
def crear_o_actualizar_docente(
    cedula:     str           = Form(...),
    nombre:     str           = Form(...),
    asignatura: Optional[str] = Form(None),
    telefono:   Optional[str] = Form(None),
    email:      Optional[str] = Form(None),
    db: Session = Depends(get_db),
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
    categoria:       Optional[str]  = None,
    requiere_serial: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Inventario)
    if categoria:             q = q.filter(Inventario.categoria == categoria)
    if requiere_serial is not None: q = q.filter(Inventario.requiere_serial == requiere_serial)
    result = []
    for inv in q.all():
        d = {
            "id": inv.id, "nombre": inv.nombre, "categoria": inv.categoria,
            "requiere_serial": inv.requiere_serial, "marca": inv.marca,
            "modelo": inv.modelo, "descripcion": inv.descripcion,
            "created_at": inv.created_at.isoformat(),
        }
        if inv.requiere_serial:
            d["total_activos"]    = len(inv.activos)
            d["disponibles"]      = sum(1 for a in inv.activos if a.estado == "Disponible")
            d["prestados"]        = sum(1 for a in inv.activos if a.estado == "Prestado")
            d["en_mantenimiento"] = sum(1 for a in inv.activos if a.estado == "Mantenimiento")
        else:
            d["stock"] = inv.stock
        result.append(d)
    return result


@app.post("/inventario", tags=["Inventario"], status_code=201)
def crear_inventario(
    nombre:          str           = Form(...),
    categoria:       str           = Form(...),
    requiere_serial: bool          = Form(False),
    stock:           int           = Form(0),
    marca:           Optional[str] = Form(None),
    modelo:          Optional[str] = Form(None),
    descripcion:     Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    inv = Inventario(
        nombre=nombre, categoria=categoria, requiere_serial=requiere_serial,
        stock=0 if requiere_serial else stock,
        marca=marca, modelo=modelo, descripcion=descripcion,
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
    id:          int,
    nombre:      Optional[str] = Form(None),
    categoria:   Optional[str] = Form(None),
    marca:       Optional[str] = Form(None),
    modelo:      Optional[str] = Form(None),
    descripcion: Optional[str] = Form(None),
    db: Session = Depends(get_db),
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


# ── Movimientos de stock genérico ────────────
@app.post("/inventario/{id}/movimiento", tags=["Inventario"])
def registrar_movimiento(
    id:            int,
    tipo:          str           = Form(...),
    cantidad:      int           = Form(...),
    responsable:   Optional[str] = Form(None),
    referencia:    Optional[str] = Form(None),
    observaciones: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    inv = db.query(Inventario).filter_by(id=id).first()
    if not inv: raise HTTPException(404, "Ítem no encontrado")
    if inv.requiere_serial:
        raise HTTPException(400, "Este ítem usa activos individuales, no stock genérico")
    if tipo not in ("entrada", "salida"):
        raise HTTPException(400, "Tipo debe ser 'entrada' o 'salida'")
    if tipo == "salida" and inv.stock < cantidad:
        raise HTTPException(409, f"Stock insuficiente ({inv.stock} disponibles)")
    inv.stock += cantidad if tipo == "entrada" else -cantidad
    db.add(Movimiento(
        inventario_id=id, tipo=tipo, cantidad=cantidad,
        responsable=responsable, referencia=referencia, observaciones=observaciones,
    ))
    db.commit()
    return {"id": inv.id, "nombre": inv.nombre, "stock": inv.stock}


@app.get("/inventario/{id}/movimientos", tags=["Inventario"])
def listar_movimientos(id: int, db: Session = Depends(get_db)):
    movs = db.query(Movimiento).filter_by(inventario_id=id).order_by(Movimiento.fecha.desc()).all()
    return [{
        "id": m.id, "inventario_id": m.inventario_id,
        "tipo": m.tipo, "cantidad": m.cantidad,
        "responsable": m.responsable, "referencia": m.referencia,
        "observaciones": m.observaciones, "fecha": m.fecha.isoformat(),
    } for m in movs]


# ═══════════════════════════════════════════════
# ACTIVOS ÚNICOS (con serial)
# ═══════════════════════════════════════════════
@app.get("/activos", tags=["Activos"])
def listar_activos(
    inventario_id: Optional[int] = None,
    estado:        Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Activo)
    if inventario_id: q = q.filter(Activo.inventario_id == inventario_id)
    if estado:        q = q.filter(Activo.estado == estado)
    return [_serial_activo(a) for a in q.all()]


@app.get("/activos/buscar", tags=["Activos"])
def buscar_activo(
    codigo: str = Query(..., description="Serial o código de barras"),
    db: Session = Depends(get_db),
):
    """Busca por serial o código de barras (usado por el escáner de pistola)."""
    a = db.query(Activo).filter(
        (Activo.serial == codigo) | (Activo.codigo_barras == codigo)
    ).first()
    if not a: raise HTTPException(404, f"No se encontró activo con código: {codigo}")
    return _serial_activo(a)


@app.post("/activos", tags=["Activos"], status_code=201)
def crear_activos(
    inventario_id: int           = Form(...),
    seriales:      str           = Form(..., description="Separados por coma o salto de línea"),
    marca:         Optional[str] = Form(None),
    modelo:        Optional[str] = Form(None),
    observaciones: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    inv = db.query(Inventario).filter_by(id=inventario_id).first()
    if not inv:              raise HTTPException(404, "Ítem de inventario no encontrado")
    if not inv.requiere_serial: raise HTTPException(400, "Este ítem no requiere serial individual")

    lista = [s.strip() for s in seriales.replace("\n", ",").split(",") if s.strip()]
    if not lista: raise HTTPException(400, "Debes ingresar al menos un serial")

    creados = []
    for serial in lista:
        if db.query(Activo).filter_by(serial=serial).first():
            raise HTTPException(409, f"El serial '{serial}' ya existe")
        cod    = gen_codigo_barras(inv.categoria[:4])
        activo = Activo(
            inventario_id=inventario_id, serial=serial,
            codigo_barras=cod, estado="Disponible",
            marca=marca, modelo=modelo, observaciones=observaciones,
        )
        db.add(activo); db.flush()
        log_historial(db, activo.id, "Creado", estado="Disponible",
                      obs=f"Registro inicial. Serial: {serial}")
        creados.append(activo)

    db.commit()
    return [_serial_activo(a) for a in creados]


@app.patch("/activos/{id}/estado", tags=["Activos"])
def cambiar_estado_activo(
    id:          int,
    estado:      str           = Form(...),
    obs:         Optional[str] = Form(None),
    responsable: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    estados_validos = {"Disponible", "Mantenimiento", "Dañado", "Baja"}
    if estado not in estados_validos:
        raise HTTPException(400, f"Estado inválido. Opciones: {estados_validos}")
    a = db.query(Activo).filter_by(id=id).first()
    if not a: raise HTTPException(404, "Activo no encontrado")
    a.estado = estado
    log_historial(db, id, estado, responsable=responsable, estado=estado, obs=obs)
    db.commit()
    return _serial_activo(a)


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
# PRÉSTAMOS (activos con serial)
# ═══════════════════════════════════════════════
@app.get("/prestamos", tags=["Préstamos"])
def listar_prestamos(
    situacion: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Prestamo)
    if situacion: q = q.filter(Prestamo.situacion == situacion)
    return [_serial_prestamo(p) for p in q.all()]


@app.get("/prestamos/{id}", tags=["Préstamos"])
def obtener_prestamo(id: int, db: Session = Depends(get_db)):
    p = db.query(Prestamo).filter_by(id=id).first()
    if not p: raise HTTPException(404, "Préstamo no encontrado")
    return _serial_prestamo(p)


@app.post("/prestamos", tags=["Préstamos"], status_code=201)
async def crear_prestamo(
    serial_o_barras: str           = Form(...),
    cedula_docente:  str           = Form(...),
    nombre_docente:  str           = Form(...),
    asignatura:      Optional[str] = Form(None),
    telefono:        Optional[str] = Form(None),
    estado_entrega:  Optional[str] = Form("Bueno"),
    obs_entrega:     Optional[str] = Form(None),
    fecha_entrega:   Optional[str] = Form(None),
    firma:           Optional[UploadFile] = File(None),
    fotos:           List[UploadFile]     = File(default=[]),
    db: Session = Depends(get_db),
):
    activo = db.query(Activo).filter(
        (Activo.serial == serial_o_barras) | (Activo.codigo_barras == serial_o_barras)
    ).first()
    if not activo:
        raise HTTPException(404, f"No se encontró el activo: {serial_o_barras}")
    if activo.estado != "Disponible":
        raise HTTPException(409, f"El activo no está disponible. Estado actual: {activo.estado}")

    docente = db.query(Docente).filter_by(cedula=cedula_docente).first()
    if not docente:
        docente = Docente(cedula=cedula_docente, nombre=nombre_docente,
                          asignatura=asignatura, telefono=telefono)
        db.add(docente); db.flush()

    url_firma = None
    if firma and firma.filename:
        url_firma = subir_archivo(firma, "firmas")

    fecha = datetime.fromisoformat(fecha_entrega) if fecha_entrega else datetime.utcnow()
    prestamo = Prestamo(
        activo_id=activo.id, docente_id=docente.id,
        estado_entrega=estado_entrega, obs_entrega=obs_entrega,
        fecha_entrega=fecha, firma_entrega=url_firma, situacion="activo",
    )
    db.add(prestamo); db.flush()

    for foto in fotos:
        if foto.filename:
            url = subir_archivo(foto, f"prestamos/{prestamo.id}")
            db.add(FotoPrestamo(prestamo_id=prestamo.id, url=url, tipo="entrega"))

    activo.estado = "Prestado"
    activo.responsable_actual = nombre_docente
    log_historial(db, activo.id, "Prestado", responsable=nombre_docente, estado="Prestado",
                  obs=f"Entregado a {nombre_docente} (CC {cedula_docente}). Estado: {estado_entrega}")

    db.commit(); db.refresh(prestamo)
    return _serial_prestamo(prestamo)


@app.patch("/prestamos/{id}/devolucion", tags=["Préstamos"])
async def registrar_devolucion_prestamo(
    id: int,
    estado_devolucion: Optional[str] = Form("Disponible"),
    obs_devolucion:    Optional[str] = Form(None),
    fecha_devolucion:  Optional[str] = Form(None),
    firma:             Optional[UploadFile] = File(None),
    fotos:             List[UploadFile]     = File(default=[]),
    db: Session = Depends(get_db),
):
    p = db.query(Prestamo).filter_by(id=id).first()
    if not p:              raise HTTPException(404, "Préstamo no encontrado")
    if p.situacion == "devuelto": raise HTTPException(409, "Este préstamo ya fue cerrado")

    if firma and firma.filename:
        p.firma_devolucion = subir_archivo(firma, "firmas")

    for foto in fotos:
        if foto.filename:
            url = subir_archivo(foto, f"devoluciones/{id}")
            db.add(FotoPrestamo(prestamo_id=id, url=url, tipo="devolucion"))

    fecha = datetime.fromisoformat(fecha_devolucion) if fecha_devolucion else datetime.utcnow()
    p.situacion        = "devuelto"
    p.fecha_devolucion = fecha
    p.estado_devolucion = estado_devolucion
    p.obs_devolucion   = obs_devolucion

    nuevo_estado = estado_devolucion if estado_devolucion in ("Mantenimiento", "Dañado") else "Disponible"
    p.activo.estado = nuevo_estado
    p.activo.responsable_actual = None
    log_historial(db, p.activo_id, "Devuelto",
                  responsable=p.docente.nombre, estado=nuevo_estado, obs=obs_devolucion)

    db.commit(); db.refresh(p)
    return _serial_prestamo(p)


@app.get("/prestamos/por-serial/{serial}", tags=["Préstamos"])
def prestamo_activo_por_serial(serial: str, db: Session = Depends(get_db)):
    activo = db.query(Activo).filter(
        (Activo.serial == serial) | (Activo.codigo_barras == serial)
    ).first()
    if not activo: raise HTTPException(404, "Activo no encontrado")
    p = db.query(Prestamo).filter_by(activo_id=activo.id, situacion="activo").first()
    if not p: raise HTTPException(404, "No hay préstamo activo para este equipo")
    return _serial_prestamo(p)


# ═══════════════════════════════════════════════
# ENDPOINTS LEGACY — /entregas (compatibilidad con el frontend original)
# ═══════════════════════════════════════════════
@app.get("/equipos", tags=["Legacy"])
def listar_equipos(db: Session = Depends(get_db)):
    return db.query(Equipo).all()


@app.post("/equipos", tags=["Legacy"], status_code=201)
def crear_equipo_legacy(
    serial: str           = Form(...),
    modelo: Optional[str] = Form(None),
    marca:  Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if db.query(Equipo).filter_by(serial=serial).first():
        raise HTTPException(409, "Ya existe un equipo con ese serial")
    eq = Equipo(serial=serial, modelo=modelo, marca=marca)
    db.add(eq); db.commit(); db.refresh(eq)
    return eq


@app.get("/entregas", tags=["Legacy"])
def listar_entregas(
    situacion: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Entrega)
    if situacion: q = q.filter(Entrega.situacion == situacion)
    return [_serial_entrega(e) for e in q.all()]


@app.post("/entregas", tags=["Legacy"], status_code=201)
async def crear_entrega_legacy(
    serial_equipo:  str           = Form(...),
    cedula_docente: str           = Form(...),
    nombre_docente: str           = Form(...),
    asignatura:     Optional[str] = Form(None),
    telefono:       Optional[str] = Form(None),
    modelo:         Optional[str] = Form(None),
    estado_entrega: Optional[str] = Form("Bueno"),
    obs_entrega:    Optional[str] = Form(None),
    fecha_entrega:  Optional[str] = Form(None),
    firma:          Optional[UploadFile] = File(None),
    fotos:          List[UploadFile]     = File(default=[]),
    db: Session = Depends(get_db),
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
    url_firma = None
    if firma and firma.filename: url_firma = subir_archivo(firma, "firmas")
    fecha = datetime.fromisoformat(fecha_entrega) if fecha_entrega else datetime.utcnow()
    entrega = Entrega(
        equipo_id=equipo.id, docente_id=docente.id,
        estado_entrega=estado_entrega, obs_entrega=obs_entrega,
        fecha_entrega=fecha, firma_entrega=url_firma, situacion="activo",
    )
    db.add(entrega); db.flush()
    for foto in fotos:
        if foto.filename:
            url = subir_archivo(foto, f"entregas/{entrega.id}")
            db.add(FotoEntrega(entrega_id=entrega.id, url=url, tipo="entrega"))
    db.commit(); db.refresh(entrega)
    return _serial_entrega(entrega)


@app.patch("/entregas/{id}/devolucion", tags=["Legacy"])
async def devolucion_legacy(
    id:                int,
    estado_devolucion: Optional[str] = Form("Bueno"),
    obs_devolucion:    Optional[str] = Form(None),
    fecha_devolucion:  Optional[str] = Form(None),
    firma:             Optional[UploadFile] = File(None),
    fotos:             List[UploadFile]     = File(default=[]),
    db: Session = Depends(get_db),
):
    e = db.query(Entrega).filter_by(id=id).first()
    if not e:                     raise HTTPException(404, "Entrega no encontrada")
    if e.situacion == "devuelto": raise HTTPException(409, "Ya devuelto")
    if firma and firma.filename:  e.firma_devolucion = subir_archivo(firma, "firmas")
    for foto in fotos:
        if foto.filename:
            url = subir_archivo(foto, f"devoluciones/{id}")
            db.add(FotoEntrega(entrega_id=id, url=url, tipo="devolucion"))
    fecha = datetime.fromisoformat(fecha_devolucion) if fecha_devolucion else datetime.utcnow()
    e.situacion        = "devuelto"
    e.fecha_devolucion = fecha
    e.estado_devolucion = estado_devolucion
    e.obs_devolucion   = obs_devolucion
    db.commit(); db.refresh(e)
    return _serial_entrega(e)


# ═══════════════════════════════════════════════
# INFORME / DASHBOARD
# ═══════════════════════════════════════════════
@app.get("/informe", tags=["Informe"])
def obtener_informe(db: Session = Depends(get_db)):
    return {
        "prestamos_activos":     db.query(Prestamo).filter_by(situacion="activo").count(),
        "prestamos_devueltos":   db.query(Prestamo).filter_by(situacion="devuelto").count(),
        "total_activos":         db.query(Activo).count(),
        "activos_disponibles":   db.query(Activo).filter_by(estado="Disponible").count(),
        "activos_prestados":     db.query(Activo).filter_by(estado="Prestado").count(),
        "activos_mantenimiento": db.query(Activo).filter_by(estado="Mantenimiento").count(),
        "activos_daniados":      db.query(Activo).filter_by(estado="Dañado").count(),
        "total_inventario":      db.query(Inventario).count(),
        "total_docentes":        db.query(Docente).count(),
        "entregas_activas":      db.query(Entrega).filter_by(situacion="activo").count(),
        "entregas_devueltas":    db.query(Entrega).filter_by(situacion="devuelto").count(),
    }


# ═══════════════════════════════════════════════
# RAÍZ — sirve el frontend desde static/index.html
# ═══════════════════════════════════════════════
@app.get("/", include_in_schema=False)
def root():
    index = STATIC / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"status": "API ok", "docs": "/docs"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)





