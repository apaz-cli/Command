# C2 - Home Network Command & Control System

A FastAPI-based command and control system for managing multiple machines on a home network.

## Architecture

- **Server**: Central coordinator running on `10.0.0.6:30814`
- **Client**: Lightweight agent running on worker machines
- **CLI**: Interactive command-line interface (`!>` prompt)

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

### 1. Start the Server

```bash
cd server
python main.py
```

Server runs on `10.0.0.6:30814` (configured in `server/config.json`)

### 2. Start Workers

On each worker machine:

```bash
cd client
python main.py
```

Workers register automatically and poll for work.

### 3. Use the CLI

```bash
cd cli
python main.py
```

## CLI Commands

### Worker Management

```bash
!> list                          # List all workers
!> groups                        # List worker groups
!> assign <ip> <group>           # Assign worker to group
!> unassign <ip> <group>         # Remove from group
!> info <ip>                     # Detailed worker info
!> set-tags <ip> <tag1,tag2>     # Set available tags
!> get-tags <ip>                 # Show tag status
!> cleanup-workspaces <ip|@all>  # Trigger workspace cleanup
!> detect-tags <ip>              # Suggest tags based on hardware
```

### Job Management

```bash
!> jobs                          # List active jobs
!> job <job_id>                  # Job details
!> cancel <job_id>               # Cancel job
!> queue-status                  # Queue stats
!> log [n]                       # Show last n results (scrollable)
!> get-files <job_id> [dir]      # Download job output files
```

### Job Submission

```bash
!> submit "echo hello"                           # Basic job
!> submit "python train.py" --group=gpu          # Target group
!> submit "task.sh" --tags=gpu:0,disk            # Require tags
!> submit "process.py" --depends=job-123         # After dependency
!> submit "cleanup.sh" --same-machine            # Same as deps
!> submit "convert.sh" --stdin-file=data.txt     # With stdin
!> submit "render.py" --upload=scene.blend:input.blend --download=*.png
```

### Batch Operations

```bash
!> batch jobs.txt                # Submit from file (JSON or plain)
!> batch jobs.txt --dry-run      # Preview without submitting
!> split "process {}" inputs.txt # One job per line with placeholder
!> split "task {}" data.txt --group=gpu
```

### PDSH Integration

```bash
!> pdsh @gpu nvidia-smi          # Execute on group
!> pdsh @all uptime              # Execute on all workers
!> pdsh 10.0.0.15 hostname       # Execute on specific IP
!> verify-ssh @all               # Check SSH connectivity
```

## Implementation Status

**Phase 1 - Core Infrastructure** ✓
- Server with worker registration (IP-based)
- Workers.json persistence
- Client with exponential backoff polling (1s→2s→4s→8s→10s)
- Job execution in `/tmp/C2/{job_id}/` workspace
- CLI with readline history
- Worker timeout tracking (15s)

**Phase 2 - Job Queue** ✓
- Job submission with queue limit (50k jobs)
- First-eligible-worker distribution
- Dependency validation (topological sort)
- Circular dependency detection
- Tag-based locking
- Same-machine affinity
- Jobs.log persistence
- Batch submission (atomic)

**Phase 3 - Constraints & PDSH** ✓
- PDSH integration with auto-install
- SSH verification helper
- Job log viewing with `less`
- Workspace cleanup commands
- Hardware detection and tag suggestions

**Phase 4 - Batch Operations & Utilities** ✓
- Batch file submission (JSON or plain commands)
- Split command for work distribution
- Get-files for downloading job outputs
- Submit-wait endpoints (long-polling)
- Metrics endpoint (uptime, jobs processed, active workers)
- Dry-run mode for batch/split operations

**Additional Improvements** ✓
- **Dependency failure propagation**: Jobs automatically fail when dependencies fail (recursive)
- **Smart job queueing**: Jobs with dependencies automatically enter queue when ready
- **Concurrent job execution**: Client runs up to 4 jobs simultaneously (threaded)
- **Hardware detection**: Client detects GPUs (nvidia-smi), CPU cores, memory
- **Workspace cleanup**: Actual cleanup implementation (removes /tmp/C2/*)
- **Enhanced validation**:
  - Empty command rejection
  - File existence checks for uploads
  - Group existence validation
  - Better error messages throughout
- **Error handling**:
  - Worker timeout logging
  - File download error recovery
  - Completed job file access (410 Gone)
  - Request timeouts and retries

## Configuration

### Server (`server/config.json`)
```json
{
  "host": "10.0.0.6",
  "port": 30814,
  "max_queue_size": 50000,
  "workers_file": "workers.json",
  "jobs_log_file": "jobs.log"
}
```

### Client (`client/config.json`)
```json
{
  "server_host": "10.0.0.6",
  "server_port": 30814
}
```

## Project Structure

```
Command/
├── server/
│   ├── main.py          # FastAPI server
│   └── config.json      # Server config
├── client/
│   ├── main.py          # Worker client
│   └── config.json      # Client config
├── cli/
│   └── main.py          # Interactive CLI
├── shared/
│   └── models.py        # Pydantic models
└── requirements.txt
```

## Key Features

### Core
- **Worker timeout**: 15 seconds (hardcoded)
- **Queue capacity**: 50,000 jobs max
- **Job distribution**: First eligible worker gets job (atomic claim)
- **Dependencies**: Validated with topological sort, circular detection
- **Dependency propagation**: Failed jobs automatically fail all dependents
- **Tag locking**: Named resource locks per worker (e.g., gpu:0, disk)
- **Persistence**: workers.json + jobs.log (append-only)
- **Workspaces**: `/tmp/C2/{job_id}/` on workers
- **Concurrent execution**: Workers can run up to 4 jobs simultaneously

### Advanced
- **PDSH integration**: Auto-installs if missing
- **Batch operations**: Atomic submission (all-or-nothing)
- **File transfer**: Upload inputs, download outputs (base64)
- **Long-polling**: Submit-wait endpoints for synchronous jobs
- **Hardware detection**: Auto-detect GPUs, CPU cores, memory
- **Workspace cleanup**: On-demand cleanup of job workspaces
- **Error recovery**: Server restart returns in-progress jobs to queue
- **Smart queueing**: Jobs with unsatisfied dependencies automatically queue when ready

## API Endpoints

### Worker Management
- `POST /api/workers/register` - Register/update worker
- `POST /api/workers/get-work/{ip}` - Poll for work (with backoff)
- `GET /api/workers/list` - List all workers
- `GET /api/workers/info/{ip}` - Worker details
- `POST /api/workers/groups/{ip}` - Assign groups
- `DELETE /api/workers/groups/{ip}/{group}` - Unassign group
- `POST /api/workers/tags/{ip}` - Set available tags
- `GET /api/workers/tags/{ip}` - Get tag status
- `POST /api/workers/cleanup/{ip}` - Trigger workspace cleanup
- `POST /api/workers/detect-tags/{ip}` - Hardware detection

### Job Management
- `POST /api/jobs/submit` - Submit job
- `POST /api/jobs/submit-wait` - Submit and wait for completion
- `POST /api/jobs/submit-batch` - Atomic batch submission
- `POST /api/jobs/submit-batch-wait` - Batch submit and wait
- `GET /api/jobs/queue-status` - Queue stats
- `GET /api/jobs/list` - Active jobs
- `GET /api/jobs/info/{job_id}` - Job details
- `GET /api/jobs/files/{job_id}` - Output files
- `POST /api/jobs/cancel/{job_id}` - Cancel job
- `GET /api/jobs/log?lines=N` - Recent results

### PDSH & Metrics
- `GET /api/pdsh/workers/{target}` - Get worker list for pdsh
- `GET /api/metrics` - Server metrics

## Example Usage

### Simple Job
```bash
!> submit "echo hello world"
```

### GPU Training with Dependencies
```bash
# Submit training job
!> submit "python train.py" --group=gpu --tags=gpu:0 --stdin-file=config.yaml
Submitted job job-abc123

# Submit evaluation after training completes
!> submit "python eval.py" --depends=job-abc123 --same-machine --tags=gpu:0
```

### Batch Processing
Create `jobs.txt`:
```
{"command": "process 1.dat", "group": "workers", "tags": ["disk"]}
{"command": "process 2.dat", "group": "workers", "tags": ["disk"]}
process 3.dat
```

Submit:
```bash
!> batch jobs.txt
Submitted 3 jobs
```

### Split Work
Create `inputs.txt`:
```
file1.csv
file2.csv
file3.csv
```

Submit:
```bash
!> split "python analyze.py {}" inputs.txt --group=compute
Submitted 3 jobs
```

### PDSH Operations
```bash
# Check GPU status on all GPU workers
!> pdsh @gpu nvidia-smi

# Update packages on all workers
!> pdsh @all "sudo apt update && sudo apt upgrade -y"

# Verify connectivity first
!> verify-ssh @all
```
