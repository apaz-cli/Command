"""FastAPI server for C2 system."""
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from uuid import uuid4
from collections import deque

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.models import (
    Worker, Job, WorkerRegistration, WorkerStatusUpdate,
    JobSubmission, QueueStatus, JobLogEntry, WorkerStatus, JobStatus
)

# Load configuration
config_path = Path(__file__).parent / "config.json"
with open(config_path) as f:
    config = json.load(f)

app = FastAPI(title="C2 Server")

# Server state
workers: Dict[str, Worker] = {}
jobs: Dict[str, Job] = {}
job_queue: deque = deque()  # Pending jobs
workers_file = Path(__file__).parent / config["workers_file"]
jobs_log_file = Path(__file__).parent / config["jobs_log_file"]

WORKER_TIMEOUT = timedelta(seconds=15)
MAX_QUEUE_SIZE = config["max_queue_size"]


def load_workers():
    """Load workers from disk."""
    global workers
    if workers_file.exists():
        with open(workers_file) as f:
            data = json.load(f)
            for ip, worker_data in data.get("workers", {}).items():
                worker_data["last_seen"] = datetime.fromisoformat(worker_data.get("last_seen", datetime.now().isoformat()))
                worker_data["registered_at"] = datetime.fromisoformat(worker_data["registered_at"])
                worker_data["status"] = WorkerStatus.DISCONNECTED
                worker_data["current_jobs"] = []
                worker_data["tag_locks"] = {tag: None for tag in worker_data.get("available_tags", [])}
                workers[ip] = Worker(**worker_data)


def save_workers():
    """Save workers to disk."""
    data = {
        "workers": {
            ip: {
                "hostname": w.hostname,
                "ip": w.ip,
                "os": w.os,
                "arch": w.arch,
                "disk_available_gb": w.disk_available_gb,
                "groups": w.groups,
                "available_tags": w.available_tags,
                "registered_at": w.registered_at.isoformat(),
                "last_seen": w.last_seen.isoformat()
            }
            for ip, w in workers.items()
        },
        "last_updated": datetime.now().isoformat()
    }
    with open(workers_file, "w") as f:
        json.dump(data, f, indent=2)


def log_job(job: Job, worker: Worker):
    """Append job result to log file."""
    entry = JobLogEntry(
        job_id=job.id,
        command=job.command,
        status=job.status.value,
        worker_ip=worker.ip,
        worker_hostname=worker.hostname,
        group=job.group,
        tags=job.tags,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at or datetime.now(),
        exit_code=job.exit_code,
        stdout=job.stdout,
        stderr=job.stderr,
        output_files=list(job.output_files.keys()),
        workspace=f"/tmp/C2/{job.id}"
    )
    with open(jobs_log_file, "a") as f:
        f.write(entry.model_dump_json() + "\n")


def check_worker_timeouts():
    """Mark workers as disconnected if they haven't checked in."""
    now = datetime.now()
    for worker in workers.values():
        if worker.status != WorkerStatus.DISCONNECTED:
            if now - worker.last_seen > WORKER_TIMEOUT:
                worker.status = WorkerStatus.DISCONNECTED
                print(f"Worker {worker.ip} timed out (last seen: {worker.last_seen})")
                # Release tags and return jobs to queue
                for job in worker.current_jobs[:]:
                    print(f"Returning job {job.id} to queue due to worker timeout")
                    job.status = JobStatus.PENDING
                    job.assigned_worker = None
                    job_queue.append(job)
                    # Release tags
                    for tag in job.tags:
                        if tag in worker.tag_locks:
                            worker.tag_locks[tag] = None
                worker.current_jobs.clear()


def validate_dependencies(job_ids: List[str]) -> Optional[str]:
    """Validate job dependencies using iterative topological sort. Returns error message or None."""
    if not job_ids:
        return None

    # Check all dependencies exist
    for dep_id in job_ids:
        if dep_id not in jobs:
            return f"Dependency job {dep_id} does not exist"

    return None


def check_circular_dependencies(new_job_deps: List[str], all_jobs: Dict[str, Job]) -> bool:
    """Check for circular dependencies using iterative topological sort. Returns True if circular."""
    # Build dependency graph
    graph = {job_id: job.dependencies for job_id, job in all_jobs.items()}
    graph["_new_job"] = new_job_deps

    # Count incoming edges
    in_degree = {job_id: 0 for job_id in graph}
    for job_id in graph:
        for dep in graph[job_id]:
            if dep in in_degree:
                in_degree[dep] += 1

    # Process nodes with no incoming edges
    queue = deque([job_id for job_id, degree in in_degree.items() if degree == 0])
    processed = 0

    while queue:
        job_id = queue.popleft()
        processed += 1
        for dep in graph[job_id]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # If we processed all nodes, no cycle
    return processed != len(graph)


def can_assign_job_to_worker(job: Job, worker: Worker) -> bool:
    """Check if job can be assigned to worker."""
    # Check group affinity
    if job.group and job.group not in worker.groups:
        return False

    # Check dependencies are satisfied
    for dep_id in job.dependencies:
        dep_job = jobs.get(dep_id)
        if not dep_job or dep_job.status != JobStatus.COMPLETED:
            return False

    # Check same-machine affinity
    if job.same_machine and job.dependencies:
        dep_workers = set()
        for dep_id in job.dependencies:
            dep_job = jobs.get(dep_id)
            if dep_job and dep_job.assigned_worker:
                dep_workers.add(dep_job.assigned_worker)
        if dep_workers and worker.ip not in dep_workers:
            return False
        if len(dep_workers) > 1:
            # Dependencies ran on different workers - cannot satisfy
            return False

    # Check tags are available
    for tag in job.tags:
        if tag not in worker.available_tags:
            return False
        if worker.tag_locks.get(tag) is not None:
            return False

    return True


def acquire_tags(job: Job, worker: Worker):
    """Atomically acquire all job tags."""
    for tag in job.tags:
        worker.tag_locks[tag] = job.id


def release_tags(job: Job, worker: Worker):
    """Release all job tags."""
    for tag in job.tags:
        if worker.tag_locks.get(tag) == job.id:
            worker.tag_locks[tag] = None


def try_assign_job(worker: Worker) -> Optional[Job]:
    """Try to assign a pending job to worker."""
    for job in job_queue:
        if can_assign_job_to_worker(job, worker):
            job_queue.remove(job)
            job.status = JobStatus.ASSIGNED
            job.assigned_worker = worker.ip
            acquire_tags(job, worker)
            worker.current_jobs.append(job)
            return job
    return None


@app.on_event("startup")
async def startup():
    """Load workers on startup and recover from crash."""
    import time
    app.state.start_time = time.time()
    load_workers()

    # Mark any in-progress jobs as failed (server restart recovery)
    if jobs_log_file.exists():
        # Get all job IDs from log (these are completed)
        completed_job_ids = set()
        with open(jobs_log_file) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    completed_job_ids.add(entry["job_id"])
                except:
                    pass

        # Any jobs not in log were in-progress - mark as failed
        # Note: jobs dict is empty on startup, so we'd need persistence
        # For now, this is a placeholder - in-progress jobs are lost on restart
        # which effectively fails them (they never complete)


@app.post("/api/workers/register")
async def register_worker(registration: WorkerRegistration):
    """Register or update worker."""
    if registration.ip in workers:
        # Update existing worker (reconnect scenario)
        worker = workers[registration.ip]
        worker.hostname = registration.hostname
        worker.os = registration.os
        worker.arch = registration.arch
        worker.disk_available_gb = registration.disk_available_gb
        worker.last_seen = datetime.now()
        worker.status = WorkerStatus.IDLE
    else:
        # Create new worker
        worker = Worker(
            ip=registration.ip,
            hostname=registration.hostname,
            os=registration.os,
            arch=registration.arch,
            disk_available_gb=registration.disk_available_gb,
            groups=registration.groups
        )
        workers[registration.ip] = worker

    save_workers()
    return {"ip": registration.ip}


def fail_dependent_jobs(failed_job_id: str):
    """Fail all jobs that depend on a failed job."""
    to_fail = []

    # Find all jobs that depend on this one
    for job in jobs.values():
        if failed_job_id in job.dependencies:
            to_fail.append(job)

    # Recursively fail dependent jobs
    for job in to_fail:
        if job.status not in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
            job.status = JobStatus.FAILED
            job.completed_at = datetime.now()
            job.stderr = f"Dependency {failed_job_id} failed"

            # Remove from queue if pending
            if job in job_queue:
                job_queue.remove(job)

            # Release tags and remove from worker if assigned
            if job.assigned_worker:
                worker = workers.get(job.assigned_worker)
                if worker:
                    release_tags(job, worker)
                    if job in worker.current_jobs:
                        worker.current_jobs.remove(job)
                    # Create a pseudo-worker for logging if worker not found
                    log_job(job, worker)

            # Recursively fail its dependents
            fail_dependent_jobs(job.id)

            del jobs[job.id]


def queue_ready_jobs():
    """Check for jobs whose dependencies are now satisfied and queue them."""
    to_queue = []

    for job in jobs.values():
        if job.status == JobStatus.PENDING and job not in job_queue:
            # Check if all dependencies are satisfied
            all_deps_satisfied = all(
                jobs.get(dep_id) and jobs[dep_id].status == JobStatus.COMPLETED
                for dep_id in job.dependencies
            )

            # If no dependencies or all satisfied, queue it
            if not job.dependencies or all_deps_satisfied:
                to_queue.append(job)

    for job in to_queue:
        job_queue.append(job)


@app.post("/api/workers/get-work/{ip}")
async def get_work(ip: str, status_update: Optional[WorkerStatusUpdate] = None):
    """Worker polls for work and optionally updates job status."""
    check_worker_timeouts()

    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not registered")

    worker = workers[ip]
    worker.last_seen = datetime.now()
    worker.status = WorkerStatus.ACTIVE

    # Handle status update if provided
    if status_update:
        job = jobs.get(status_update.job_id)
        if job:
            job.status = status_update.status
            if status_update.exit_code is not None:
                job.exit_code = status_update.exit_code
            if status_update.stdout is not None:
                job.stdout = status_update.stdout
            if status_update.stderr is not None:
                job.stderr = status_update.stderr
            if status_update.output_files is not None:
                job.output_files = status_update.output_files

            if job.status == JobStatus.RUNNING:
                job.started_at = datetime.now()
            elif job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
                job.completed_at = datetime.now()
                release_tags(job, worker)
                if job in worker.current_jobs:
                    worker.current_jobs.remove(job)
                log_job(job, worker)

                # If job failed, fail all dependent jobs
                if job.status == JobStatus.FAILED:
                    fail_dependent_jobs(job.id)

                # If job completed, check if any waiting jobs can now be queued
                if job.status == JobStatus.COMPLETED:
                    queue_ready_jobs()

                del jobs[job.id]

    # Update worker status
    if worker.current_jobs:
        worker.status = WorkerStatus.BUSY
    else:
        worker.status = WorkerStatus.IDLE

    # Try to assign a job
    job = try_assign_job(worker)
    if job:
        return {"job": job.model_dump(mode="json")}

    # Calculate backoff (client handles the actual backoff)
    return {"wait_seconds": 1}


@app.get("/api/workers/list")
async def list_workers():
    """List all workers."""
    check_worker_timeouts()
    return {"workers": [w.model_dump(mode="json") for w in workers.values()]}


@app.get("/api/workers/info/{ip}")
async def worker_info(ip: str):
    """Get worker details."""
    check_worker_timeouts()
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")
    return workers[ip].model_dump(mode="json")


@app.post("/api/workers/groups/{ip}")
async def assign_groups(ip: str, groups: Dict[str, List[str]]):
    """Assign worker to groups."""
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker = workers[ip]
    for group in groups["groups"]:
        if group not in worker.groups:
            worker.groups.append(group)

    save_workers()
    return {"status": "ok"}


@app.delete("/api/workers/groups/{ip}/{group}")
async def unassign_group(ip: str, group: str):
    """Remove worker from group."""
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker = workers[ip]
    if group in worker.groups:
        worker.groups.remove(group)

    save_workers()
    return {"status": "ok"}


@app.post("/api/workers/tags/{ip}")
async def set_tags(ip: str, tags: Dict[str, List[str]]):
    """Set available tags for worker."""
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker = workers[ip]
    worker.available_tags = tags["tags"]
    worker.tag_locks = {tag: None for tag in tags["tags"]}

    save_workers()
    return {"status": "ok"}


@app.get("/api/workers/tags/{ip}")
async def get_tags(ip: str):
    """Get tag status for worker."""
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker = workers[ip]
    return {
        "available_tags": worker.available_tags,
        "tag_locks": worker.tag_locks
    }


@app.post("/api/jobs/submit")
async def submit_job(submission: JobSubmission):
    """Submit a new job."""
    # Validate command is not empty
    if not submission.command or not submission.command.strip():
        raise HTTPException(status_code=400, detail="Command cannot be empty")

    # Check queue capacity
    if len(job_queue) >= MAX_QUEUE_SIZE:
        raise HTTPException(status_code=507, detail="Queue is full")

    # Validate dependencies
    if submission.dependencies:
        error = validate_dependencies(submission.dependencies)
        if error:
            raise HTTPException(status_code=400, detail=error)

        # Check for circular dependencies
        if check_circular_dependencies(submission.dependencies, jobs):
            raise HTTPException(status_code=400, detail="Circular dependency detected")

    # Validate tags exist on at least one worker in target group
    if submission.tags:
        eligible_workers = [
            w for w in workers.values()
            if (not submission.group or submission.group in w.groups)
            and all(tag in w.available_tags for tag in submission.tags)
        ]
        if not eligible_workers:
            raise HTTPException(
                status_code=400,
                detail=f"No workers in group '{submission.group or 'any'}' have all required tags: {submission.tags}"
            )

    # Validate group exists if specified
    if submission.group:
        group_workers = [w for w in workers.values() if submission.group in w.groups]
        if not group_workers:
            raise HTTPException(
                status_code=400,
                detail=f"No workers in group '{submission.group}'"
            )

    # Create job
    job_id = f"job-{uuid4()}"
    job = Job(
        id=job_id,
        command=submission.command,
        group=submission.group,
        dependencies=submission.dependencies,
        same_machine=submission.same_machine,
        tags=submission.tags,
        stdin=submission.stdin,
        input_files=submission.input_files,
        output_file_patterns=submission.output_file_patterns
    )

    jobs[job_id] = job

    # Check if dependencies are met - if all satisfied, add to queue immediately
    all_deps_satisfied = all(
        jobs.get(dep_id) and jobs[dep_id].status == JobStatus.COMPLETED
        for dep_id in job.dependencies
    )

    if all_deps_satisfied or not job.dependencies:
        job_queue.append(job)
    # Otherwise job stays in jobs dict but not in queue yet

    return {"job_id": job_id}


@app.post("/api/jobs/submit-batch")
async def submit_batch(batch: Dict[str, List[JobSubmission]]):
    """Submit multiple jobs atomically."""
    job_submissions = batch["jobs"]

    # Check queue capacity
    if len(job_queue) + len(job_submissions) > MAX_QUEUE_SIZE:
        raise HTTPException(status_code=507, detail="Batch would exceed queue capacity")

    # Validate all jobs first
    for submission in job_submissions:
        if submission.dependencies:
            error = validate_dependencies(submission.dependencies)
            if error:
                raise HTTPException(status_code=400, detail=error)
            if check_circular_dependencies(submission.dependencies, jobs):
                raise HTTPException(status_code=400, detail="Circular dependency detected")

    # Create all jobs
    job_ids = []
    for submission in job_submissions:
        job_id = f"job-{uuid4()}"
        job = Job(
            id=job_id,
            command=submission.command,
            group=submission.group,
            dependencies=submission.dependencies,
            same_machine=submission.same_machine,
            tags=submission.tags,
            stdin=submission.stdin,
            input_files=submission.input_files,
            output_file_patterns=submission.output_file_patterns
        )
        jobs[job_id] = job
        job_ids.append(job_id)

        # Add to queue if dependencies satisfied
        all_deps_satisfied = all(
            jobs.get(dep_id) and jobs[dep_id].status == JobStatus.COMPLETED
            for dep_id in job.dependencies
        )
        if all_deps_satisfied or not job.dependencies:
            job_queue.append(job)

    return {"job_ids": job_ids}


@app.get("/api/jobs/queue-status")
async def queue_status():
    """Get queue size and capacity."""
    pending = len(job_queue)
    running = sum(1 for j in jobs.values() if j.status == JobStatus.RUNNING)
    return QueueStatus(
        pending=pending,
        running=running,
        capacity=MAX_QUEUE_SIZE,
        available=MAX_QUEUE_SIZE - pending
    )


@app.get("/api/jobs/list")
async def list_jobs():
    """List all active jobs."""
    return {"jobs": [j.model_dump(mode="json") for j in jobs.values()]}


@app.get("/api/jobs/info/{job_id}")
async def job_info(job_id: str):
    """Get job details."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id].model_dump(mode="json")


@app.get("/api/jobs/files/{job_id}")
async def get_job_files(job_id: str):
    """Get job output files."""
    if job_id not in jobs:
        # Check if job is in log
        if jobs_log_file.exists():
            with open(jobs_log_file) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry["job_id"] == job_id:
                            # Job completed but files not available in log
                            raise HTTPException(
                                status_code=410,
                                detail="Job completed. Files are only available for active jobs."
                            )
                    except json.JSONDecodeError:
                        pass
        raise HTTPException(status_code=404, detail="Job not found")
    return {"files": jobs[job_id].output_files}


@app.post("/api/jobs/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Job already finished")

    job.status = JobStatus.CANCELLED
    job.completed_at = datetime.now()

    # Remove from queue if pending
    if job in job_queue:
        job_queue.remove(job)

    # Release tags and remove from worker if assigned
    if job.assigned_worker:
        worker = workers.get(job.assigned_worker)
        if worker:
            release_tags(job, worker)
            if job in worker.current_jobs:
                worker.current_jobs.remove(job)
            log_job(job, worker)

    del jobs[job_id]
    return {"status": "ok"}


@app.delete("/api/jobs/completed")
async def clear_completed():
    """Clear completed jobs from memory (they're already logged)."""
    # Already handled by moving completed jobs to log
    return {"status": "ok"}


@app.get("/api/jobs/log")
async def get_job_log(lines: int = 50):
    """Get recent job results from log file."""
    if not jobs_log_file.exists():
        return {"entries": []}

    with open(jobs_log_file) as f:
        all_lines = f.readlines()

    recent_lines = all_lines[-lines:] if lines < len(all_lines) else all_lines
    entries = [json.loads(line) for line in reversed(recent_lines)]

    return {"entries": entries}


@app.get("/api/pdsh/workers/{target}")
async def get_pdsh_workers(target: str):
    """Get worker list for pdsh command."""
    check_worker_timeouts()

    worker_list = []

    if target == "all":
        worker_list = list(workers.values())
    elif target.startswith("@"):
        # Group name (strip @ prefix)
        group = target[1:]
        worker_list = [w for w in workers.values() if group in w.groups]
    else:
        # Single IP
        if target in workers:
            worker_list = [workers[target]]

    # Filter to only active workers
    active_workers = [w for w in worker_list if w.status != WorkerStatus.DISCONNECTED]

    # Build hostname list for pdsh
    hostnames = ",".join(w.hostname for w in active_workers)

    return {
        "workers": [{"ip": w.ip, "hostname": w.hostname} for w in active_workers],
        "hostnames": hostnames
    }


@app.post("/api/workers/cleanup/{ip}")
async def cleanup_worker(ip: str):
    """Trigger workspace cleanup on worker."""
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker = workers[ip]

    # Check if worker has any active jobs
    if worker.current_jobs:
        raise HTTPException(
            status_code=400,
            detail=f"Worker has {len(worker.current_jobs)} active jobs. Cannot cleanup."
        )

    # Mark worker for cleanup (worker will check this flag)
    if not hasattr(worker, "cleanup_requested"):
        worker.cleanup_requested = False
    worker.cleanup_requested = True

    return {"status": "ok"}


@app.post("/api/workers/detect-tags/{ip}")
async def detect_worker_tags(ip: str):
    """Run hardware detection and suggest tags."""
    if ip not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker = workers[ip]

    # Request hardware detection from worker
    # Mark worker for hardware detection (worker will check this flag)
    if not hasattr(worker, "detect_hardware_requested"):
        worker.detect_hardware_requested = False
    worker.detect_hardware_requested = True

    # Return basic suggestions based on current worker info
    suggested_tags = []

    # Disk-based tags
    if worker.disk_available_gb > 100:
        suggested_tags.append("disk")

    # OS-based tags
    if worker.os == "Linux":
        suggested_tags.append("linux")
    elif worker.os == "Darwin":
        suggested_tags.append("macos")

    # Architecture tags
    suggested_tags.append(f"arch:{worker.arch}")

    return {"suggested_tags": suggested_tags}


@app.post("/api/jobs/submit-wait")
async def submit_job_wait(submission: JobSubmission):
    """Submit job and wait for completion."""
    # Submit job normally
    result = await submit_job(submission)
    job_id = result["job_id"]

    # Wait for completion
    import asyncio
    while True:
        job = jobs.get(job_id)
        if not job:
            # Job completed and was removed from memory
            # Try to find it in the log
            if jobs_log_file.exists():
                with open(jobs_log_file) as f:
                    for line in f:
                        entry = json.loads(line)
                        if entry["job_id"] == job_id:
                            return {
                                "job_id": job_id,
                                "status": entry["status"],
                                "exit_code": entry.get("exit_code"),
                                "stdout": entry.get("stdout", ""),
                                "stderr": entry.get("stderr", ""),
                                "output_files": {}  # Files not in log
                            }
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
            return {
                "job_id": job_id,
                "status": job.status.value,
                "exit_code": job.exit_code,
                "stdout": job.stdout,
                "stderr": job.stderr,
                "output_files": job.output_files
            }

        await asyncio.sleep(0.5)


@app.post("/api/jobs/submit-batch-wait")
async def submit_batch_wait(batch: Dict[str, List[JobSubmission]]):
    """Submit batch and wait for all to complete."""
    # Submit batch normally
    result = await submit_batch(batch)
    job_ids = result["job_ids"]

    # Wait for all jobs to complete
    import asyncio
    results = []

    while len(results) < len(job_ids):
        for job_id in job_ids:
            if any(r["job_id"] == job_id for r in results):
                continue

            job = jobs.get(job_id)
            if not job:
                # Check log
                if jobs_log_file.exists():
                    with open(jobs_log_file) as f:
                        for line in f:
                            entry = json.loads(line)
                            if entry["job_id"] == job_id:
                                results.append({
                                    "job_id": job_id,
                                    "status": entry["status"],
                                    "exit_code": entry.get("exit_code"),
                                    "stdout": entry.get("stdout", ""),
                                    "stderr": entry.get("stderr", ""),
                                    "output_files": {}
                                })
                                break
            elif job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
                results.append({
                    "job_id": job_id,
                    "status": job.status.value,
                    "exit_code": job.exit_code,
                    "stdout": job.stdout,
                    "stderr": job.stderr,
                    "output_files": job.output_files
                })

        if len(results) < len(job_ids):
            await asyncio.sleep(0.5)

    return {"results": results}


@app.get("/api/metrics")
async def get_metrics():
    """Get server metrics."""
    import time

    # Calculate uptime
    if not hasattr(app.state, "start_time"):
        app.state.start_time = time.time()

    uptime = int(time.time() - app.state.start_time)

    # Count active workers
    check_worker_timeouts()
    active_workers = sum(1 for w in workers.values() if w.status != WorkerStatus.DISCONNECTED)

    # Get queue depth
    queue_depth = len(job_queue)

    # Total jobs processed (from log file)
    total_jobs = 0
    if jobs_log_file.exists():
        with open(jobs_log_file) as f:
            total_jobs = sum(1 for _ in f)

    return {
        "uptime_seconds": uptime,
        "total_jobs_processed": total_jobs,
        "active_workers": active_workers,
        "queue_depth": queue_depth
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config["host"],
        port=config["port"],
        log_level="info"
    )
