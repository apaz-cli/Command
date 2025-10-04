#!/usr/bin/env python3
"""C2 Client - Worker agent for executing jobs."""
import json
import sys
import os
import base64
import subprocess
import shutil
import socket
import platform
import threading
import queue
from pathlib import Path
from time import sleep
from typing import Optional
import requests

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.models import WorkerRegistration, WorkerStatusUpdate, JobStatus

# Load configuration
config_path = Path(__file__).parent / "config.json"
with open(config_path) as f:
    config = json.load(f)

SERVER_URL = f"http://{config['server_host']}:{config['server_port']}"
WORKSPACE_BASE = Path("/tmp/C2")
BACKOFF_SEQUENCE = [1, 2, 4, 8, 10]  # Exponential backoff to 10s max


def detect_hardware() -> dict:
    """Detect hardware capabilities."""
    tags = []

    # GPU detection (NVIDIA)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--list-gpus"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            gpu_lines = [line for line in result.stdout.strip().split('\n') if line]
            for i, _ in enumerate(gpu_lines):
                tags.append(f"gpu:{i}")
    except:
        pass

    # CPU cores
    try:
        cpu_count = os.cpu_count() or 0
        if cpu_count > 0:
            tags.append(f"cpu:{cpu_count}")
    except:
        pass

    # Memory (in GB)
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_kb = int(line.split()[1])
                        mem_gb = mem_kb // (1024 * 1024)
                        tags.append(f"mem:{mem_gb}gb")
                        break
    except:
        pass

    return {"tags": tags}


def get_worker_info() -> dict:
    """Get worker system information."""
    hostname = socket.gethostname()
    ip = socket.gethostbyname(hostname)

    # Get OS and architecture
    os_name = platform.system()
    arch = platform.machine()

    # Get available disk space
    stat = shutil.disk_usage("/")
    disk_available_gb = stat.free / (1024 ** 3)

    return {
        "hostname": hostname,
        "ip": ip,
        "os": os_name,
        "arch": arch,
        "disk_available_gb": round(disk_available_gb, 2)
    }


def cleanup_workspaces():
    """Clean up all job workspaces."""
    try:
        if WORKSPACE_BASE.exists():
            shutil.rmtree(WORKSPACE_BASE)
            WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)
            print("Workspaces cleaned up")
    except Exception as e:
        print(f"Error cleaning workspaces: {e}", file=sys.stderr)


def register_worker() -> str:
    """Register with server and return worker IP."""
    info = get_worker_info()
    registration = WorkerRegistration(**info)

    response = requests.post(
        f"{SERVER_URL}/api/workers/register",
        json=registration.model_dump()
    )
    response.raise_for_status()

    result = response.json()
    return result["ip"]


def execute_job(job: dict) -> dict:
    """Execute a job and return results."""
    job_id = job["id"]
    command = job["command"]
    stdin_data = job.get("stdin")
    input_files = job.get("input_files", {})
    output_patterns = job.get("output_file_patterns", [])

    # Create workspace
    workspace = WORKSPACE_BASE / job_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Write input files
    for filename, b64_content in input_files.items():
        file_path = workspace / filename
        file_path.write_bytes(base64.b64decode(b64_content))

    # Execute command
    try:
        process = subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workspace)
        )

        stdout, stderr = process.communicate(
            input=stdin_data.encode() if stdin_data else None,
            timeout=None
        )
        exit_code = process.returncode

        # Save stdout
        stdout_file = workspace / "stdout"
        stdout_file.write_bytes(stdout)

        # Collect output files
        output_files = {}
        files_dir = workspace / "files"
        files_dir.mkdir(exist_ok=True)

        for pattern in output_patterns:
            for matched_file in workspace.glob(pattern):
                if matched_file.is_file() and matched_file != stdout_file:
                    # Copy to files directory
                    dest = files_dir / matched_file.name
                    shutil.copy2(matched_file, dest)
                    # Add to output files
                    output_files[matched_file.name] = base64.b64encode(
                        matched_file.read_bytes()
                    ).decode()

        return {
            "job_id": job_id,
            "status": JobStatus.COMPLETED if exit_code == 0 else JobStatus.FAILED,
            "exit_code": exit_code,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "output_files": output_files
        }

    except Exception as e:
        return {
            "job_id": job_id,
            "status": JobStatus.FAILED,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Job execution error: {str(e)}",
            "output_files": {}
        }


def report_status(worker_ip: str, status_update: dict):
    """Report job status to server."""
    try:
        requests.post(
            f"{SERVER_URL}/api/workers/get-work/{worker_ip}",
            json=status_update
        )
    except Exception as e:
        print(f"Failed to report status: {e}", file=sys.stderr)


def job_executor(job_queue: queue.Queue, result_queue: queue.Queue, worker_ip: str):
    """Worker thread that executes jobs concurrently."""
    while True:
        try:
            job = job_queue.get()
            if job is None:  # Shutdown signal
                break

            print(f"Received job {job['id']}: {job['command']}")

            # Report job as running
            try:
                running_update = WorkerStatusUpdate(
                    job_id=job["id"],
                    status=JobStatus.RUNNING
                )
                requests.post(
                    f"{SERVER_URL}/api/workers/get-work/{worker_ip}",
                    json=running_update.model_dump(),
                    timeout=5
                )
            except Exception as e:
                print(f"Failed to report job as running: {e}", file=sys.stderr)

            # Execute job
            result = execute_job(job)
            print(f"Job {job['id']} completed with exit code {result['exit_code']}")

            # Put result in queue
            result_queue.put(result)

        except Exception as e:
            print(f"Error in job executor: {e}", file=sys.stderr)


def main():
    """Main client loop with concurrent job execution."""
    # Ensure workspace base exists
    WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)

    # Register with server
    print("Registering with server...")
    worker_ip = register_worker()
    print(f"Registered as {worker_ip}")

    # Print detected hardware
    hw_info = detect_hardware()
    if hw_info["tags"]:
        print(f"Detected hardware tags: {', '.join(hw_info['tags'])}")

    # Create job and result queues
    job_queue_obj = queue.Queue()
    result_queue_obj = queue.Queue()

    # Start worker threads (up to 4 concurrent jobs)
    num_workers = 4
    workers = []
    for _ in range(num_workers):
        t = threading.Thread(
            target=job_executor,
            args=(job_queue_obj, result_queue_obj, worker_ip),
            daemon=True
        )
        t.start()
        workers.append(t)

    # Polling loop
    backoff_index = 0
    pending_results = []

    while True:
        try:
            # Collect completed job results
            while not result_queue_obj.empty():
                result = result_queue_obj.get_nowait()
                pending_results.append(result)

            # Build request with one pending result if available
            status_update = None
            if pending_results:
                status_update = pending_results.pop(0)

            # Poll for work
            response = requests.post(
                f"{SERVER_URL}/api/workers/get-work/{worker_ip}",
                json=status_update,
                timeout=10
            )
            response.raise_for_status()
            result = response.json()

            # Reset backoff on successful contact
            backoff_index = 0

            if "job" in result:
                # Got a job - put it in the queue for workers
                job_queue_obj.put(result["job"])
            else:
                # No job available - apply exponential backoff
                if backoff_index < len(BACKOFF_SEQUENCE) - 1:
                    backoff_index += 1

                sleep(BACKOFF_SEQUENCE[backoff_index])

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            # Exponential backoff on error
            if backoff_index < len(BACKOFF_SEQUENCE) - 1:
                backoff_index += 1
            sleep(BACKOFF_SEQUENCE[backoff_index])


if __name__ == "__main__":
    main()
