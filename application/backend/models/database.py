from sqlalchemy import Column, String, DateTime, Integer, Float, Enum as SQLEnum, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime
import enum


Base = declarative_base()


class PipelineType(str, enum.Enum):
    OPENMVG_OPENMVS = "openmvg_openmvs"
    GAUSSIAN_SPLATTING = "gaussian_splatting"
    # SphereSfM (COLMAP fork) for 360° equirectangular video + OpenMVS + GLB.
    SPHERE_COLMAP_OPENMVS = "sphere_colmap_openmvs"
    # application/scripts: COLMAP 360 GPU + OpenMVS or 3DGS
    COLMAP360_OPENMVS = "colmap360_openmvs"
    COLMAP360_3DGS = "colmap360_3dgs"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(36), primary_key=True)
    pipeline_type = Column(SQLEnum(PipelineType), nullable=False)
    status = Column(SQLEnum(TaskStatus), default=TaskStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    original_filename = Column(String(512), nullable=True)
    video_path = Column(String(1024), nullable=True)
    frames_path = Column(String(1024), nullable=True)

    frames_count = Column(Integer, nullable=True)

    openmvg_matches_path = Column(String(1024), nullable=True)
    openmvg_reconstruction_path = Column(String(1024), nullable=True)
    openmvs_scene_path = Column(String(1024), nullable=True)

    gaussian_model_path = Column(String(1024), nullable=True)

    result_path = Column(String(1024), nullable=True)

    error_message = Column(String(2048), nullable=True)
    progress = Column(Float, default=0.0)

    extra_data = Column(JSON, nullable=True)
