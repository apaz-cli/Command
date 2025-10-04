"""Shared data models for the C2 system."""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum


class WorkerStatus(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    BUSY = "busy"
    DISCONNECTED = "disconnected"


class JobStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Worker(BaseModel):
    ip: str
    hostname: str
    os: str
    arch: str
    disk_available_gb: float
    groups: List[str] = Field(default_factory=list)
    available_tags: List[str] = Field(default_factory=list)
    tag_locks: Dict[str, Optional[str]] = Field(default_factory=dict)
    status: WorkerStatus = WorkerStatus.IDLE
    last_seen: datetime = Field(default_factory=datetime.now)
    current_jobs: List["Job"] = Field(default_factory=list)
    registered_at: datetime = Field(default_factory=datetime.now)


class Job(BaseModel):
    id: str
    command: str
    status: JobStatus = JobStatus.PENDING
    group: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    same_machine: bool = False
    tags: List[str] = Field(default_factory=list)
    stdin: Optional[str] = None
    input_files: Dict[str, str] = Field(default_factory=dict)  # filename -> base64 content
    output_file_patterns: List[str] = Field(default_factory=list)
    assigned_worker: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    output_files: Dict[str, str] = Field(default_factory=dict)  # filename -> base64 content


class WorkerRegistration(BaseModel):
    hostname: str
    ip: str
    os: str
    arch: str
    disk_available_gb: float
    groups: List[str] = Field(default_factory=list)


class WorkerStatusUpdate(BaseModel):
    job_id: str
    status: JobStatus
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    output_files: Optional[Dict[str, str]] = None


class JobSubmission(BaseModel):
    command: str
    group: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    same_machine: bool = False
    tags: List[str] = Field(default_factory=list)
    stdin: Optional[str] = None
    input_files: Dict[str, str] = Field(default_factory=dict)
    output_file_patterns: List[str] = Field(default_factory=list)


class QueueStatus(BaseModel):
    pending: int
    running: int
    capacity: int
    available: int


class JobLogEntry(BaseModel):
    job_id: str
    command: str
    status: str
    worker_ip: str
    worker_hostname: str
    group: Optional[str]
    tags: List[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: datetime
    exit_code: Optional[int]
    stdout: str
    stderr: str
    output_files: List[str]
    workspace: str
